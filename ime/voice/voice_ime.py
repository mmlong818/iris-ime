"""语音输入状态机: PTT → 录音 → FunASR → GLM 润色 → 输出回调。

红线(来自 alpha.12 卡死教训): 所有耗时环节(录音落盘/转写/HTTP)在工作
线程跑, 主线程只收事件。同一时刻至多一个工作流; 工作中再按 PTT 忽略。

失败兜底(R8): 润色失败/断网 → 直接出 FunASR 原文; 录音 WAV 恒存本地;
历史 JSONL 恒追加——大段口述永不丢。
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from .asr_local import LocalASR
from .capture import Recorder
from .config import voice_data_dir
from .polish import Polisher

# 应用感知 profile(R2, Phase 2 完善): 前台 exe → 整理风格
PROFILE_BY_EXE = {
    # "claude.exe": "coding", "cursor.exe": "coding",
    # "outlook.exe": "email", "wechat.exe": "chat", "weixin.exe": "chat",
}


def detect_profile(exe):
    return PROFILE_BY_EXE.get((exe or "").lower(), "default")


class VoiceIme:
    """纯逻辑状态机, 不碰 Tk——回调由视图层(client)接。"""

    def __init__(self, cfg, on_state=None, on_output=None, history_dir=None):
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.recorder = Recorder(samplerate=cfg.data["record"]["samplerate"],
                                 data_dir=voice_data_dir() / "recordings",
                                 max_seconds=cfg.data["record"]["max_seconds"])
        self.history_dir = Path(history_dir) if history_dir else \
            voice_data_dir() / "history"
        self.asr = LocalASR(cfg.asr)
        self.polisher = Polisher(cfg.llm)
        self.state = "idle"        # idle/recording/transcribing/polishing/error
        self.state_ts = time.monotonic()
        self.last_raw = ""
        self._busy = False         # 工作线程在跑(转写+润色)
        self.on_state = on_state   # fn(state: str, info: dict)
        self.on_output = on_output  # fn(text: str, raw: str)

    # ---- 主线程入口(钩子事件) ----

    def ptt_down(self):
        if not self.enabled or self._busy:
            return
        self.recorder.start()
        self._set("recording")

    def ptt_up(self):
        if self.state != "recording":
            return
        wav = self.recorder.stop()
        if wav is None:            # 误触: <0.25s
            self._set("idle")
            return
        self._busy = True
        self._set("transcribing", {"wav": str(wav)})
        threading.Thread(target=self._run, args=(wav,), daemon=True,
                         name="iris-voice").start()

    def preload(self):
        """开启语音时预热 ASR 模型(首次拉取 ~1GB, 之后离线毫秒级)。"""
        self.asr.preload()

    def cancel(self):
        """语音关闭时: 丢弃录音中的内容(工作线程中的已启动流程照常走完)。"""
        if self.state == "recording":
            self.recorder.stop()
            self._set("idle")

    # ---- 工作线程 ----

    def _run(self, wav):
        raw = ""
        try:
            raw = self.asr.transcribe(wav)
        except Exception as exc:
            self._busy = False
            self._set("error", {"message": f"识别失败: {exc}"})
            return
        if not raw:
            self._busy = False
            self._set("idle")
            return
        self.last_raw = raw
        self._set("polishing", {"raw": raw})
        text = raw
        if self.cfg.llm_ready():
            try:
                profile = detect_profile(None)  # Phase 2: 前台 exe 感知
                text = self.polisher.polish(raw, profile=profile)
            except Exception:
                text = raw        # 兜底: 润色失败出原文, 绝不空手
        self._append_history(wav, raw, text)
        self._busy = False
        self._set("idle")
        if self.on_output:
            self.on_output(text, raw)

    # ---- 内部 ----

    def _set(self, state, info=None):
        self.state = state
        self.state_ts = time.monotonic()
        if self.on_state:
            self.on_state(state, info or {})

    def _append_history(self, wav, raw, text):
        try:
            self.history_dir.mkdir(parents=True, exist_ok=True)
            rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "wav": str(wav), "raw": raw, "text": text}
            with open(self.history_dir / f"{datetime.now():%Y%m}.jsonl", "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
