"""语音输入子系统 (v2.1): 按住说话 → 本地 FunASR 转写 → GLM 润色 → 注入上屏。

需求对照: 松手出稿/去口头禅/改口修订(R1), 词库自管理(R3, Phase 2),
出字快(R4), 小声可识别(R5), 直译(R6, Phase 2), 鼠标侧键(R7, Phase 2),
录音历史全本地+断网兜底(R8)。
"""

from .config import VoiceConfig, voice_data_dir
from .voice_ime import VoiceIme

__all__ = ["VoiceConfig", "VoiceIme", "voice_data_dir"]
