"""鸢尾输入法 (Iris IME): 解码引擎 + 神经重排 + 桌面客户端。"""

__version__ = "2.0.0-alpha.15"

from .engine import Candidate, DecodeResult, Engine

__all__ = ["Engine", "Candidate", "DecodeResult", "__version__"]
