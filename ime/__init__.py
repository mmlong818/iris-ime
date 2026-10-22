"""自研拼音输入法: 解码引擎 + 神经重排 + 桌面客户端。"""

__version__ = "0.9.0"

from .engine import Candidate, DecodeResult, Engine

__all__ = ["Engine", "Candidate", "DecodeResult", "__version__"]
