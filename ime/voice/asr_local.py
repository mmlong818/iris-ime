"""本地 FunASR 转写: SenseVoiceSmall + FSMN-VAD + CT 标点。

模型首次运行从 modelscope 拉取(约 1GB)后缓存; 之后完全离线。
RTX 3080 Ti 上 RTF <0.1——10 秒口述毫秒级出稿(需求 R4)。
SenseVoice 对小声/远场/口音鲁棒性好(需求 R5)。

热词: SenseVoice 本身不带 biasing, Phase 2 用 SeACo-Paraformer 或
LLM 校对双通道; hotwords 参数先行预留。
"""

import threading


class LocalASR:
    """funasr AutoModel 封装。懒加载(首个录音松手时才拉模型), 线程安全。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            from funasr import AutoModel
            device = self.cfg.get("device", "cuda")
            try:
                import torch
                if not torch.cuda.is_available():
                    device = "cpu"
            except ImportError:
                device = "cpu"
            kwargs = dict(model=self.cfg.get("model", "iic/SenseVoiceSmall"),
                          device=device, disable_update=True)
            if self.cfg.get("vad_model"):
                kwargs.update(vad_model=self.cfg["vad_model"],
                              vad_kwargs={"max_single_segment_time": 30000})
            if self.cfg.get("punc_model"):
                kwargs["punc_model"] = self.cfg["punc_model"]
            self._model = AutoModel(**kwargs)

    def transcribe(self, wav_path, hotwords=None):
        """WAV 路径 → 文本。出错抛异常, 调用方兜底。"""
        self._ensure()
        kwargs = {}
        hw = hotwords or self.cfg.get("hotwords")
        if hw:
            kwargs["hotword"] = " ".join(hw)
        res = self._model.generate(input=str(wav_path), batch_size_s=60, **kwargs)
        if not res:
            return ""
        text = res[0].get("text", "")
        # SenseVoice 输出带 <|zh|><|NEUTRAL|>... 标签, 清掉
        import re
        return re.sub(r"<\|[^|]*\|>", "", text).strip()

    def preload(self):
        """托盘开启语音时后台预热, 松手首句不用等模型下载。"""
        threading.Thread(target=self._ensure, daemon=True).start()
