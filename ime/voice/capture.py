"""按住录音: sounddevice 采集 16k mono, 松手落盘 WAV。

设计: start() 立刻开流(拖尾缓冲避免首字丢失), stop() 返回 WAV 路径。
录音过程边采边入内存 numpy 数组, 松手一次性写盘(简单可靠);
文件恒存本地(R8: 断网大段口述也不丢)。所有耗时操作在工作线程跑。
"""

import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np


class Recorder:
    """PTT 录音器。线程安全性: 单线程使用(voice_ime 工作线程)。"""

    def __init__(self, samplerate=16000, data_dir=None, max_seconds=300):
        self.samplerate = samplerate
        self.max_seconds = max_seconds
        self.dir = Path(data_dir) if data_dir else \
            Path(__import__("ime.voice.config", fromlist=["voice_data_dir"])
                 .voice_data_dir()) / "recordings"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._stream = None
        self._frames = []
        self.started_at = 0.0

    # sounddevice 回调在音频线程: 只做 append, 不碰文件系统
    def _callback(self, indata, frames, t, status):
        self._frames.append(indata.copy())

    def start(self):
        import sounddevice as sd
        self._frames = []
        self.started_at = time.monotonic()
        self._stream = sd.InputStream(samplerate=self.samplerate, channels=1,
                                      dtype="int16",
                                      blocksize=0,  # 默认, 回调频率由宿主决定
                                      callback=self._callback)
        self._stream.start()

    def duration(self):
        if self._stream is None:
            return 0.0
        return time.monotonic() - self.started_at

    def stop(self):
        """松手: 关流, 写 WAV, 返回路径。无声/出错返回 None。"""
        if self._stream is None:
            return None
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
        self._stream = None
        if not self._frames:
            return None
        audio = np.concatenate(self._frames, axis=0)
        if audio.shape[0] < self.samplerate // 4:   # <0.25s 视为误触
            return None
        audio = audio[: self.max_seconds * self.samplerate]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = self.dir / f"{ts}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.samplerate)
            w.writeframes(audio.tobytes())
        return path
