"""语音输入配置: %APPDATA%\\pinyin-ime\\voice.json。

全部 OpenAI 兼容三元组(base_url/key/model), 默认智谱 GLM;换商只改配置。
PTT 键位: vk 码十进制(右 Ctrl=163 0xA3;注意 L/R 变体分开, LL 钩子教训)。
"""

import json
import sys
from pathlib import Path

# PTT 默认键: 右 Ctrl(163)。LL 钩子报告 L/R 变体(0xA0/0xA1 教训同理),
# 两个 vk 都要监听——配置里只写"主键", 松手判定同时认 L/R 对应键。
VK_PTT_DEFAULT = [163, 162]        # RCtrl + LCtrl 对(RCtrl 主)
VK_TRANSLATE_DEFAULT = [164, 161]  # RAlt + LAlt 对(Phase 2 直译)

DEFAULTS = {
    "enabled": False,
    "ptt_vk": VK_PTT_DEFAULT,
    "translate_vk": VK_TRANSLATE_DEFAULT,
    "asr": {
        "engine": "funasr",          # Phase 1 固定本地 FunASR
        "model": "iic/SenseVoiceSmall",
        "vad_model": "fsmn-vad",
        "punc_model": "ct-punc",
        "device": "cuda",            # 无 GPU 时自动降级 cpu
        "hotwords": [],              # Phase 2: 热词库
    },
    "llm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4-flash",
        "timeout": 20,
        "temperature": 0.2,
    },
    "record": {
        "samplerate": 16000,
        "max_seconds": 300,          # 单次口述上限(防误按占满磁盘)
    },
}


def voice_data_dir():
    """录音/历史根目录: 打包态 %APPDATA%\\pinyin-ime\\voice, 开发态项目 data/。"""
    if getattr(sys, "frozen", False):
        base = Path(__import__("os").environ.get("APPDATA", ".")) / "pinyin-ime"
    else:
        base = Path(__file__).resolve().parent.parent.parent / "data"
    d = base / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class VoiceConfig:
    """语音配置读写;缺省项自动补齐, 不破坏用户自定义。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else voice_data_dir() / "voice.json"
        self.data = _deep_merge(DEFAULTS, self._load())
        self.save()  # 补齐新字段落盘

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    # ---- 便捷访问 ----

    @property
    def enabled(self):
        return bool(self.data["enabled"])

    @enabled.setter
    def enabled(self, v):
        self.data["enabled"] = bool(v)
        self.save()

    @property
    def llm(self):
        return self.data["llm"]

    @property
    def asr(self):
        return self.data["asr"]

    @property
    def ptt_vk(self):
        return list(self.data["ptt_vk"])

    def llm_ready(self):
        return bool(self.llm.get("api_key"))
