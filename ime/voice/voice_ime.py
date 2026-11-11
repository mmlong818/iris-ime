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
from .polish import PROFILE_NEWLINE, Polisher, apply_corrections


def detect_profile(exe, cfg=None):
    """前台 exe → 整理风格(应用感知, R2)。映射在 voice.json 的 profiles。"""
    if cfg is None:
        from .config import PROFILES_DEFAULT
        return PROFILES_DEFAULT.get((exe or "").lower(), "default")
    return cfg.profile_for(exe)


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
        self._mode = "dictate"     # 本次口述: dictate 听写 / translate 直译
        self._exe = None           # 按下 PTT 时的前台应用(决定整理风格)
        # 最近一次成稿的元信息, on_output 回调时可读(换行上屏策略用)
        self.last_meta = {"mode": "dictate", "profile": "default",
                          "newline": "enter", "exe": None}
        self.on_state = on_state   # fn(state: str, info: dict)
        self.on_output = on_output  # fn(text: str, raw: str)

    # ---- 主线程入口(钩子事件) ----

    def ptt_down(self, mode="dictate", exe=None):
        if not self.enabled or self._busy or self.state == "recording":
            return
        self._mode = "translate" if mode == "translate" else "dictate"
        self._exe = exe
        self.recorder.start()
        self._set("recording", {"mode": self._mode})

    def ptt_up(self):
        if self.state != "recording":
            return
        wav = self.recorder.stop()
        if wav is None:            # 误触: <0.25s
            self._set("idle")
            return
        self._busy = True
        if not self.asr.ready:
            self._set("loading_model", {"message": "语音模型加载中(每次启动首次约 10-20 秒)"})
        self._set("transcribing", {"wav": str(wav)})
        threading.Thread(target=self._run, args=(wav, self._mode, self._exe),
                         daemon=True, name="iris-voice").start()

    def preload(self):
        """开启语音时预热 ASR 模型(首次拉取 ~1GB, 之后离线毫秒级)。"""
        self.asr.preload()

    def cancel(self):
        """语音关闭时: 丢弃录音中的内容(工作线程中的已启动流程照常走完)。"""
        if self.state == "recording":
            self.recorder.stop()
            self._set("idle")

    # ---- 工作线程 ----

    def _run(self, wav, mode="dictate", exe=None):
        raw = ""
        t0 = time.monotonic()
        try:
            raw = self.asr.transcribe(wav)
        except Exception as exc:
            self._busy = False
            self._set("error", {"message": f"识别失败: {exc}"})
            return
        asr_ms = int((time.monotonic() - t0) * 1000)
        if not raw:
            self._busy = False
            self._set("idle")
            return
        # 纠错表: 本地确定性替换, 断网/没配 Key 也生效
        raw = apply_corrections(raw, self.cfg.data.get("corrections"))
        self.last_raw = raw
        profile = detect_profile(exe, self.cfg)
        hotwords = self.cfg.hotwords
        self._set("polishing", {"raw": raw, "mode": mode})
        t1 = time.monotonic()
        text, note = raw, ""
        if self.cfg.llm_ready() and self.cfg.polish_enabled():
            try:
                if mode == "translate":
                    text = self.polisher.translate(raw, hotwords=hotwords)
                else:
                    text = self.polisher.polish(raw, profile=profile,
                                                hotwords=hotwords)
            except Exception:
                text = raw        # 兜底: 润色失败出原文, 绝不空手
                if mode == "translate":
                    note = "直译失败, 已出中文原文"
        polish_ms = int((time.monotonic() - t1) * 1000)
        self.asr_ms, self.polish_ms = asr_ms, polish_ms
        if not text:
            text = raw
        self.last_meta = {
            "mode": mode, "profile": profile, "exe": exe,
            # 译文按目标应用换行; 风格规则只用于听写
            "newline": PROFILE_NEWLINE.get(profile, "enter")}
        self._append_history(wav, raw, text, mode, profile, exe,
                             asr_ms=asr_ms, polish_ms=polish_ms)
        self._busy = False
        if self.on_output:
            self.on_output(text, raw)
        if note:
            self._set("error", {"message": note})
        else:
            self._set("idle")

    # ---- 内部 ----

    def _set(self, state, info=None):
        self.state = state
        self.state_ts = time.monotonic()
        if self.on_state:
            self.on_state(state, info or {})

    def _append_history(self, wav, raw, text, mode="dictate",
                        profile="default", exe=None, asr_ms=None,
                        polish_ms=None):
        try:
            self.history_dir.mkdir(parents=True, exist_ok=True)
            rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "wav": str(wav), "raw": raw, "text": text,
                   "mode": mode, "profile": profile, "exe": exe,
                   "asr_ms": asr_ms, "polish_ms": polish_ms}
            with open(self.history_dir / f"{datetime.now():%Y%m}.jsonl", "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
