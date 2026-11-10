"""语音输入配置: %APPDATA%\\pinyin-ime\\voice.json。

全部 OpenAI 兼容三元组(base_url/key/model), 默认智谱 GLM;换商只改配置。
PTT 键位: vk 码十进制。LL 钩子报告的是左右区分键码, 列表里写哪个键
就只认哪个键: 右 Ctrl=163(0xA3), 左 Ctrl=162; 右 Alt=165(0xA5), 左 Alt=164。
"""

import json
import sys
from pathlib import Path

VK_PTT_DEFAULT = [163]         # 右 Ctrl: 按住听写
VK_TRANSLATE_DEFAULT = [165]   # 右 Alt: 按住直译(说中文出英文)

# Phase 1 的默认值写错了: [163, 162] 把左 Ctrl 也当成 PTT(开着语音时
# Ctrl+C/V 全被吞), [164, 161] 实际是左 Alt + 右 Shift。配置文件里若还是
# 这两组原样旧默认, 加载时迁移到新默认; 用户自己改过的不动。
_LEGACY_DEFAULTS = {"ptt_vk": ([163, 162], VK_PTT_DEFAULT),
                    "translate_vk": ([164, 161], VK_TRANSLATE_DEFAULT)}

# 应用感知(R2): 前台 exe(小写) → 整理风格。voice.json 的 "profiles" 可增改,
# 值写 "default" 即关闭该应用的感知。
PROFILES_DEFAULT = {
    # AI 编程助手/编辑器: 诉求整理成编号需求清单
    "claude.exe": "coding", "cursor.exe": "coding", "code.exe": "coding",
    "windsurf.exe": "coding", "zed.exe": "coding",
    # 终端(Claude Code 等 CLI): 同上, 但回车=提交, 成稿拼成一行
    "windowsterminal.exe": "terminal", "wezterm-gui.exe": "terminal",
    "alacritty.exe": "terminal", "powershell.exe": "terminal",
    "pwsh.exe": "terminal", "cmd.exe": "terminal",
    # 邮件: 称呼 + 落款
    "outlook.exe": "email", "olk.exe": "email", "thunderbird.exe": "email",
    "foxmail.exe": "email",
    # 即时聊天: 保留口语
    "wechat.exe": "chat", "weixin.exe": "chat", "qq.exe": "chat",
    "dingtalk.exe": "chat", "feishu.exe": "chat", "lark.exe": "chat",
    "wxwork.exe": "chat", "telegram.exe": "chat", "slack.exe": "chat",
}

DEFAULTS = {
    "enabled": False,
    "ptt_vk": VK_PTT_DEFAULT,
    "translate_vk": VK_TRANSLATE_DEFAULT,
    "ptt_mouse": 0,                  # 鼠标侧键 PTT: 0=关, 1=侧键后退, 2=侧键前进
    "profiles": PROFILES_DEFAULT,
    # 纠错表: ASR 稳定听错的词 → 正确写法, 转写后确定性替换(不依赖联网)
    "corrections": {},
    "asr": {
        "engine": "funasr",          # Phase 1 固定本地 FunASR
        "model": "iic/SenseVoiceSmall",
        "vad_model": "fsmn-vad",
        "punc_model": "ct-punc",
        "device": "cuda",            # 无 GPU 时自动降级 cpu
        "hotwords": [],              # 热词: 专有名词规范写法, 交给润色模型校对
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
        for key, (old, new) in _LEGACY_DEFAULTS.items():
            if self.data.get(key) == old:
                self.data[key] = list(new)
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

    @property
    def translate_vk(self):
        return list(self.data.get("translate_vk") or [])

    @property
    def ptt_mouse(self):
        try:
            v = int(self.data.get("ptt_mouse") or 0)
        except (TypeError, ValueError):
            return 0
        return v if v in (1, 2) else 0

    @property
    def hotwords(self):
        return [w for w in (self.asr.get("hotwords") or [])
                if isinstance(w, str) and w.strip()]

    def profile_for(self, exe):
        """前台 exe → 整理风格名; 未登记的应用走 default。"""
        return (self.data.get("profiles") or {}).get((exe or "").lower(),
                                                     "default")

    def llm_ready(self):
        return bool(self.llm.get("api_key"))
