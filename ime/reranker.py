"""pinyin-ime-reranker-4M 的 Python/numpy 推理端口。

严格按 chinese-ime-lm 的 docs/format.md 实现加载与前向, 按 reference/src/lib.rs
的 Reranker::best_where 语义实现决策。要点(文档明确警告的两个陷阱):
  - GELU 用 erf 精确形式, 不是 tanh 近似;
  - 候选首字由前缀最后一个位置预测, 打分必须从该位置起算。
"""

import json
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.special import erf as _erf

PAD, UNK, BOS = 0, 1, 2
COMPARED = 9          # 最多参与比较的候选数(lib.rs 常量)
DEFAULT_MODEL = Path(__file__).resolve().parent.parent / "models" / "sentence-model-4m.safetensors"

_DTYPES = {"F32": np.float32, "F16": np.float16, "I8": np.int8}


@dataclass
class Facts:
    answers_key: bool            # 候选完整应答了按键(非前缀、非越界预测)
    trusted_dictionary_hit: bool  # 可信词典命中(整个按键的精确词/字, 无纠错)


class SentenceModel:
    """自描述 safetensors 加载 + numpy 前向。"""

    def __init__(self, path=DEFAULT_MODEL):
        with open(path, "rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            header = json.loads(f.read(n).decode("utf-8"))
            blob = f.read()
        meta = header.pop("__metadata__")
        if meta.get("format") != "chinese-ime-lm":
            raise ValueError(f"未知格式 {meta.get('format')!r}")
        cfg = json.loads(meta["config"])
        self.n_layer = cfg["n_layer"]
        self.n_head = cfg["n_head"]
        self.n_embd = cfg["n_embd"]
        self.context = cfg["context"]
        self.vocab_list = json.loads(meta["vocab"])
        # 保留 token 靠常量寻址; 建字符索引时跳过长度不为 1 的项
        self.char_id = {ch: i for i, ch in enumerate(self.vocab_list) if len(ch) == 1}

        raw = {}
        for name, info in header.items():
            start, end = info["data_offsets"]
            arr = np.frombuffer(blob, dtype=_DTYPES[info["dtype"]],
                                count=(end - start) // np.dtype(_DTYPES[info["dtype"]]).itemsize,
                                offset=start)
            raw[name] = arr.reshape(info["shape"])
        # int8 行量化: 真实值 = int8 * scale[行]
        self.t = {}
        for name, arr in raw.items():
            if name.endswith(".scale"):
                continue
            scale = raw.get(name + ".scale")
            if scale is not None and arr.ndim == 2:
                arr = arr.astype(np.float32) * scale[:, None].astype(np.float32)
            self.t[name] = arr.astype(np.float32, copy=False)

    # ---------- 编码 ----------

    def encode(self, text, limit=None):
        ids = [self.char_id.get(ch, UNK) for ch in text]
        return ids[:limit] if limit is not None else ids

    def prefix_tokens(self, context, longest):
        """[BOS] + 上下文(右对齐截断, 为候选留位)。"""
        room = max(0, self.context - (longest + 1))
        encoded = self.encode(context)
        start = max(0, len(encoded) - room)
        return [BOS] + encoded[start:]

    # ---------- 前向 ----------

    def _ln(self, x, w, b):
        mu = x.mean(axis=-1, keepdims=True)
        var = ((x - mu) ** 2).mean(axis=-1, keepdims=True)
        return (x - mu) / np.sqrt(var + 1e-5) * w + b

    def _linear(self, x, prefix):
        return x @ self.t[f"{prefix}.weight"].T + self.t[f"{prefix}.bias"]

    @staticmethod
    def _gelu(x):
        x64 = x.astype(np.float64)
        return (0.5 * x64 * (1.0 + _erf(x64 / np.sqrt(2.0)))).astype(np.float32)

    def forward(self, tokens):
        """tokens: [B, T] 的 int 数组, 返回 logits [B, T, vocab](绑定词表)。"""
        tokens = np.asarray(tokens, dtype=np.int64)
        bsz, tlen = tokens.shape
        x = self.t["tok.weight"][tokens] + self.t["pos.weight"][:tlen]
        hd = self.n_embd // self.n_head
        scale = 1.0 / np.sqrt(float(hd))
        mask = np.triu(np.full((tlen, tlen), -1e30, dtype=np.float32), k=1)
        for i in range(self.n_layer):
            p = f"blocks.{i}"
            y = self._ln(x, self.t[f"{p}.ln1.weight"], self.t[f"{p}.ln1.bias"])
            qkv = self._linear(y, f"{p}.qkv")                       # [B,T,3C]
            q, k, v = np.split(qkv, 3, axis=-1)
            shape = (bsz, tlen, self.n_head, hd)
            q, k, v = (z.reshape(shape).transpose(0, 2, 1, 3) for z in (q, k, v))
            scores = (q @ k.transpose(0, 1, 3, 2)) * scale + mask    # [B,h,T,T]
            scores = scores - scores.max(axis=-1, keepdims=True)
            att = np.exp(scores)
            att = att / att.sum(axis=-1, keepdims=True)
            y = (att @ v).transpose(0, 2, 1, 3).reshape(bsz, tlen, self.n_embd)
            x = x + self._linear(y, f"{p}.proj")
            y = self._ln(x, self.t[f"{p}.ln2.weight"], self.t[f"{p}.ln2.bias"])
            x = x + self._linear(self._gelu(self._linear(y, f"{p}.fc")), f"{p}.out")
        x = self._ln(x, self.t["ln_f.weight"], self.t["ln_f.bias"])
        return x @ self.t["tok.weight"].T

    # ---------- 打分 ----------

    def score(self, context, candidates):
        """每个候选的逐字平均 log P(候选|上下文), 与 lib.rs score() 一致。"""
        tails = []
        longest = 0
        for text in candidates:
            n = len(text)
            longest = max(longest, n)
        prefix = self.prefix_tokens(context, longest)
        limit = max(1, self.context - len(prefix))
        for text in candidates:
            tail = self.encode(text, limit)
            tails.append(tail)
        plen = len(prefix)
        seqs = [prefix + tail for tail in tails]
        tmax = max(len(s) for s in seqs)
        batch = np.zeros((len(seqs), tmax), dtype=np.int64)
        for r, s in enumerate(seqs):
            batch[r, :len(s)] = s
        logits = self.forward(batch)                                  # [B,T,V]
        out = []
        for r, tail in enumerate(tails):
            if not tail:
                out.append(float("-inf"))
                continue
            # 位置 p 预测 p+1; 候选首字由前缀最后位置(plen-1)预测
            rows = logits[r, plen - 1: plen - 1 + len(tail), :]       # [n, V]
            logp = rows - self._logsumexp(rows)
            total = float(np.sum(logp[np.arange(len(tail)), tail]))
            out.append(total / len(tail))
        return out

    @staticmethod
    def _logsumexp(rows):
        m = rows.max(axis=-1, keepdims=True)
        return m + np.log(np.exp(rows - m).sum(axis=-1, keepdims=True))


class Reranker:
    """best_where 决策: 返回模型偏好的候选下标, None 表示维持原序。"""

    def __init__(self, model):
        self.model = model

    def _comparable(self, facts):
        if not facts:
            return None
        answers, trusted = facts[0]
        # 可信词典命中携带语料词频证据, 字级模型没有; leader 不应答按键则无从比较
        if trusted or not answers:
            return None
        considered = [i for i, (a, _) in enumerate(facts) if a][:COMPARED]
        return considered if len(considered) >= 2 else None

    def best_where(self, context, texts, facts):
        """texts: 候选文本列表; facts: 同长 (answers_key, trusted) 元组列表。"""
        considered = self._comparable(facts)
        if considered is None:
            return None
        scores = self.model.score(context, [texts[i] for i in considered])
        best = max(range(len(scores)), key=lambda i: scores[i])
        return considered[best] if best != 0 else None

    def log_probabilities(self, context, candidates):
        return self.model.score(context, list(candidates))
