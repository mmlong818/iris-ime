"""本地 ASR 双后端: funasr(torch, 开发态 GPU 优先) / funasr_onnx(打包版 CPU)。

选择逻辑: 有 torch+funasr 用 GPU 版(RTF 0.03); 否则 onnxruntime 量化
SenseVoice(CPU RTF ~0.1-0.3, 打包版内置 onnxruntime 仅几十 MB)。
模型均由 modelscope 首次运行拉取后缓存, 之后完全离线。

热词: SenseVoice 无 biasing, Phase 2 用 SeACo-Paraformer 或 LLM 校对;
hotwords 参数预留。
"""

import re
import threading

_TAG = re.compile(r"<\|[^|]*\|>")


class LocalASR:
    """懒加载(首个录音松手时才拉模型), 线程安全; backend: torch/onnx。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._model = None
        self._backend = None
        self.last_error = ""   # 各后端失败现场(_ensure 抛错时可见)
        self._lock = threading.Lock()

    @property
    def ready(self):
        return self._model is not None

    # ---- torch 后端 ----

    def _try_torch(self):
        try:
            from funasr import AutoModel  # noqa: F401  torch 依赖随包
        except Exception:
            return False   # 未安装: 走 onnx, 属正常路径
        device = self.cfg.get("device", "cuda")   # 构建期错误上抛给 _ensure 记录
        try:
            import torch
            if not torch.cuda.is_available():
                device = "cpu"
        except ImportError:
            return False
        kwargs = dict(model=self.cfg.get("model", "iic/SenseVoiceSmall"),
                      device=device, disable_update=True)
        if self.cfg.get("vad_model"):
            kwargs.update(vad_model=self.cfg["vad_model"],
                          vad_kwargs={"max_single_segment_time": 30000})
        if self.cfg.get("punc_model"):
            kwargs["punc_model"] = self.cfg["punc_model"]
        self._model = AutoModel(**kwargs)
        self._backend = "torch"
        return True

    # ---- onnx 后端(打包版) ----

    def _onnx_dir(self):
        """内置 onnx 模型目录(打包版在 _MEIPASS, 开发态在项目 models/)。"""
        import sys
        from pathlib import Path
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", ".")) / "models" / "sensevoice-onnx"
        else:
            base = (Path(__file__).resolve().parent.parent.parent /
                    "models" / "sensevoice-onnx")
        return base if base.exists() else None

    def _try_onnx(self):
        try:
            from funasr_onnx import SenseVoiceSmall
        except Exception:
            return False   # 未安装: 属正常路径(开发态通常走 torch)
        # quantize=True: int8 量化模型, CPU 上快且体积减半
        d = self._onnx_dir()
        if d:
            self._model = SenseVoiceSmall(model_dir=str(d), quantize=True,
                                      intra_op_num_threads=8)
        else:
            from modelscope import snapshot_download
            path = snapshot_download(
                self.cfg.get("onnx_model", "iic/SenseVoiceSmall_ONNX"))
            self._model = SenseVoiceSmall(model_dir=path, quantize=True)
        self._backend = "onnx"
        return True

    def _ensure(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            errors = []
            for name, fn in (("torch", self._try_torch), ("onnx", self._try_onnx)):
                try:
                    if fn():
                        return
                except Exception as exc:
                    import traceback
                    errors.append(f"{name}: {exc!r}\n"
                                  f"{traceback.format_exc(limit=4)}")
                    self.last_error = "\n".join(errors)
            raise RuntimeError(
                "无可用 ASR 后端: 需要 funasr(torch) 或 funasr-onnx\n"
                + self.last_error)

    def transcribe(self, wav_path, hotwords=None):
        """WAV 路径 → 文本。出错抛异常, 调用方兜底。"""
        self._ensure()
        if self._backend == "torch":
            kwargs = {}
            hw = hotwords or self.cfg.get("hotwords")
            if hw:
                kwargs["hotword"] = " ".join(hw)
            res = self._model.generate(input=str(wav_path), batch_size_s=60,
                                       **kwargs)
            if not res:
                return ""
            text = res[0].get("text", "")
        else:
            res = self._model(str(wav_path), language="zh", use_itn=True)
            # funasr_onnx 返回 [[text, {...}]] 形态
            text = res[0][0] if res and isinstance(res[0], (list, tuple)) \
                else (res[0] if res else "")
        return _TAG.sub("", text).strip()

    def preload(self):
        """托盘开启语音时后台预热, 松手首句不用等模型下载。"""
        threading.Thread(target=self._ensure, daemon=True,
                         name="iris-asr-preload").start()
