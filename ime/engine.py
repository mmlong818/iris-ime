"""拼音整句解码引擎: 词格 DAG + Viterbi。

解码思路:
  1. 对输入串的每个位置 i, 找出所有从 i 出发的边——
     词典精确匹配(词/单字)、尾部前缀匹配(未打完的音节)、简拼(声母串)匹配;
  2. 在词格上做前向 Viterbi 得最优整句路径, 后向 DP 得每个位置到结尾的最优分;
  3. 候选 = 首段不同的各条路径, 按 (首段边分 + 首段之后最优分) 排序。
"""

import math
import pickle
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "dicts" / "cache.pkl"

# ---- 结构常量 ----
MAX_INPUT = 64          # 编码缓冲上限(字母)
MAX_SPAN = 24           # 单条边最多覆盖的字母数
WORD_EDGE_CAP = 16      # 句中跨度的词条数
WORD_EDGE_CAP_HEAD = 40 # 候选首段与句尾跨度的词条数
CHAR_EDGE_CAP = 14      # 句中跨度的单字数
CHAR_EDGE_CAP_HEAD = 40 # 候选首段(i=0)与句尾跨度的单字数(用户选字处, 高产音节 100+ 同音字)
CAND_HEAD_COUNT = 10    # 生成候选时首段跨度的额外词条数
MAX_CANDIDATES = 500    # 安全硬上限(防病态输入); 正常候选不受限, 翻页浏览
PAGE_SIZE = 9


@dataclass
class Candidate:
    text: str        # 首段文本, 选中即上屏
    commit_end: int  # 首段消耗的拼音字母数
    preview: str     # 整个缓冲的解码预览
    score: float
    kind: str = ""   # 首段边的来源: word/char(词典精确) partial(末音节补全) initial(简拼)
    pinyin: str = "" # 整句拼音, 音节以 ' 分隔(ni'hao); 未知切分(如自造词)为原始字母
    single_word: bool = False  # 整条读法=单个词典词/字(重排可信判据)


@dataclass
class DecodeResult:
    candidates: list = field(default_factory=list)
    remainder: str = ""    # 未能解码、需原样透传的尾部字母
    committed: str = ""    # 最优路径已确认部分的文本(预览用)
    consumed: int = 0      # 最优路径消耗的编码字母数(不含透传余部)
    key: str = ""          # 规范化后的按键字母串(重排序判词典命中用)


class Engine:
    def __init__(self, cache_path=DEFAULT_CACHE, *,
                 word_bonus=2.0, char_penalty=24.0,
                 partial_penalty=2.0, initial_penalty=6.0,
                 learn=False, user_path=None,
                 fuzzy=True, fuzzy_penalty=4.0):
        """可调打分参数:

        word_bonus      词每多一个音节的加成
        char_penalty    单字边罚分——unigram 下 P(字1)P(字2) 恒大于 P(词),
                        必须罚掉逐字路径让其只做兜底
        partial_penalty 尾部按前缀匹配未打完音节的罚分
        initial_penalty 简拼(纯声母串)匹配的罚分
        learn           开启用户词学习(上屏内容写入 user_path 并加权)
        fuzzy           模糊音(zh/z, ch/c, sh/s, n/l, ang/an, eng/en, ing/in)
        fuzzy_penalty   模糊命中边的罚分——精确命中永远排在模糊之前
        """
        with open(cache_path, "rb") as f:
            cache = pickle.load(f)
        self.pinyin_chars: dict = cache["pinyin_chars"]
        self.word_by_pinyin: dict = cache["word_by_pinyin"]
        self.initial_words: dict = cache["initial_words"]
        self.syllables: list = sorted(cache["syllables"])
        self.syl_set: set = set(self.syllables)
        self.sorted_keys: list = sorted(self.word_by_pinyin)
        self.log = lambda w: math.log(w if w > 0 else 1)
        self.word_bonus = word_bonus
        self.char_penalty = char_penalty
        self.partial_penalty = partial_penalty
        self.initial_penalty = initial_penalty
        self.reranker = None  # 由 attach_reranker 装配

        # 用户词: 学到的词条按"上屏时打的拼音"存储, 打分时当作高权词典边
        self.learn_enabled = learn
        self.user_path = Path(user_path) if user_path else \
            Path(__file__).resolve().parent.parent / "data" / "user_words.json"
        self.user_counts = {}   # text -> {"key": 拼音, "count": 次数}
        self.user_by_key = {}   # 拼音 -> {text: count}
        self._load_user()

        # 模糊音: 输入串按音节切分后映射到规范形(zhang→zan, nan→lan...),
        # 再查规范形索引; 缓存里没有规范形数据时自动降级为关闭
        self.fuzzy = fuzzy
        self.fuzzy_penalty = fuzzy_penalty
        self.syl_canon = cache.get("syl_canon", {})
        self.canon_words = cache.get("canon_words", {})
        self.canon_chars = cache.get("canon_chars", {})
        if not self.syl_canon:
            self.fuzzy = False

    def attach_reranker(self, model_path=None):
        """挂载 chinese-ime-lm 的神经重排序模型(可选)。"""
        from .reranker import DEFAULT_MODEL, Reranker, SentenceModel
        self.reranker = Reranker(SentenceModel(model_path or DEFAULT_MODEL))
        return self.reranker

    # ---------- 用户词学习 ----------

    def _load_user(self):
        import json
        if self.user_path.exists():
            try:
                data = json.loads(self.user_path.read_text(encoding="utf-8"))
                self.user_counts = data.get("words", {})
            except (json.JSONDecodeError, OSError):
                self.user_counts = {}
        now = time.time()
        for text, rec in list(self.user_counts.items()):
            eff = self._effective_count(rec, now)
            if eff < 0.3:  # 约 90 天未用且次数低: 遗忘
                del self.user_counts[text]
                continue
            rec["eff"] = eff
            key = rec.get("key", "")
            self.user_by_key.setdefault(key, {})[text] = eff

    def _save_user(self):
        import json
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_path.write_text(
            json.dumps({"words": self.user_counts}, ensure_ascii=False),
            encoding="utf-8")

    def learn(self, text, key, kind=""):
        """上屏学习: 记录 (上屏文本, 当时按键)。简拼、空键不记, 条目封顶。"""
        if not self.learn_enabled or kind == "initial":
            return
        text = text.strip()
        key = "".join(ch for ch in key.lower() if ch.isalpha())
        if not text or not key or len(key) > MAX_INPUT:
            return
        rec = self.user_counts.get(text)
        if rec is not None and rec.get("key") == key:
            rec["count"] = min(rec.get("count", 1) + 1, 100)
            rec["last"] = time.time()
        elif rec is None:
            if len(self.user_counts) >= 20000:
                return
            self.user_counts[text] = {"key": key, "count": 1, "last": time.time()}
            rec = self.user_counts[text]
        else:
            return  # 同文本换过键, 不动
        self.user_by_key.setdefault(key, {})[text] = self._effective_count(rec)
        self._save_user()

    @staticmethod
    def _effective_count(rec, now=None):
        """时间衰减后的有效次数: 半衰期 30 天。"""
        now = now if now is not None else time.time()
        days = max(0.0, (now - rec.get("last", now)) / 86400)
        return max(rec.get("count", 1) * 0.5 ** (days / 30), 0.1)

    def _user_edges(self, key):
        """用户学到的词条: {文本: 有效次数(已按时间衰减)}。"""
        return self.user_by_key.get(key, {})

    # ---------- 边收集 ----------

    def _word_edges(self, key):
        """key 对应的词条: [(词, 权重, 音节数, 拼音串)]。"""
        lst = self.word_by_pinyin.get(key)
        if not lst:
            return []
        return [(w, wt, py.count(" ") + 1, py) for w, wt, py in lst]

    def _prefix_words(self, prefix):
        """把 prefix 补全成完整词条的候选: [(词, 权重, 音节数)]。

        只允许在词的最后一个音节内补全——例如输入 "niha" 可以匹配
        "ni hao"(你好), 但 "ba" 不能匹配 "ba ba er di"(巴巴儿地),
        否则解码器会凭空 hallucinate 出大量没打过的字。
        """
        keys = self.sorted_keys
        lo = bisect_left(keys, prefix)
        out = []
        for k in keys[lo:lo + 300]:  # 只扫前 300 个键, 控制单次按键开销
            if not k.startswith(prefix):
                break
            w, wt, py = self.word_by_pinyin[k][0]
            last_len = len(py.split()[-1])
            if len(k) - len(prefix) >= last_len:
                continue  # 差距超过最后一个音节, 非法补全
            out.append((w, wt, py.count(" ") + 1, py))
            if len(out) >= 32:
                break
        out.sort(key=lambda x: -x[1])
        return out

    def _prefix_chars(self, prefix):
        """以 prefix 开头的音节里的高频单字: [(字, 权重)]。"""
        out = []
        for syl in self.syllables:
            if syl.startswith(prefix):
                lst = self.pinyin_chars.get(syl)
                if lst:
                    ch, wt = lst[0]
                    out.append((ch, wt))
        out.sort(key=lambda x: -x[1])
        return out

    def _canon_keys(self, span, memo):
        """把按键串按有效音节切分, 映射到规范形, 返回与原串不同的规范键。"""
        if not self.fuzzy or len(span) < 2 or len(span) > MAX_SPAN:
            return ()
        if span in memo:
            return memo[span]
        segs = [set() for _ in range(len(span) + 1)]
        segs[0].add("")
        for i in range(len(span)):
            if not segs[i]:
                continue
            for j in range(i + 1, min(i + 6, len(span)) + 1):
                s = span[i:j]
                if s in self.syl_set:
                    c = self.syl_canon.get(s, s)
                    for prefix in segs[i]:
                        if len(segs[j]) < 6:
                            segs[j].add(prefix + c)
        keys = tuple(segs[len(span)])[:4]
        memo[span] = keys
        return keys

    def _build_edges(self, P):
        """edges_from[i] = [(end, text, score, kind)]。

        kind: word/char(词典精确) partial(末音节补全) initial(简拼)——
        k-best 路径枚举要靠首边的 kind 生成候选 facts。
        """
        n = len(P)
        edges_from = [[] for _ in range(n)]
        canon_memo = {}
        for i in range(n):
            limit = min(i + MAX_SPAN, n)
            bucket = edges_from[i]
            # 首段(i=0)与句尾(j=n)跨度是用户选字处, 容量给大;
            # 句中跨度保持小容量保性能
            for j in range(i + 1, limit + 1):
                key = P[i:j]
                seen = set()
                wcap = WORD_EDGE_CAP_HEAD if (i == 0 or j == n) else WORD_EDGE_CAP
                ccap = CHAR_EDGE_CAP_HEAD if (i == 0 or j == n) else CHAR_EDGE_CAP
                # 用户学过的词条最先入格并占定文本位, 同文本的词典低频边被
                # seen 挡掉——否则 base.dict 里的同名低频词条(如李敏 w=800)
                # 会把高权用户边挤掉
                for utext, eff in self._user_edges(key).items():
                    if utext not in seen:
                        seen.add(utext)
                        wt = 1000000.0 * max(eff, 0.1)
                        bucket.append((j, utext, self.log(wt)
                                       + self.word_bonus * (len(utext) - 1)
                                       - self.char_penalty, "word", key))
                for w, wt, nsyl, py in self._word_edges(key)[:wcap]:
                    if w not in seen:
                        seen.add(w)
                        bucket.append((j, w, self.log(wt) + self.word_bonus * (nsyl - 1)
                                       - self.char_penalty, "word", py))
                if j - i <= 6:  # 单字音节最长 6 个字母(zhuang)
                    for ch, wt in self.pinyin_chars.get(key, [])[:CHAR_EDGE_CAP]:
                        if ch not in seen:
                            seen.add(ch)
                            bucket.append((j, ch, self.log(wt) - self.char_penalty, "char", key))
                # 模糊音: 规范形键查询(精确命中已占文本位, 模糊带罚分补位)
                for ck in self._canon_keys(key, canon_memo):
                    for w, wt, py in self.canon_words.get(ck, [])[:WORD_EDGE_CAP]:
                        if w not in seen:
                            seen.add(w)
                            nsyl = py.count(" ") + 1
                            bucket.append((j, w, self.log(wt) + self.word_bonus * (nsyl - 1)
                                           - self.char_penalty - self.fuzzy_penalty, "word", py))
                    if j - i <= 6:
                        for ch, wt in self.canon_chars.get(ck, [])[:ccap]:
                            if ch not in seen:
                                seen.add(ch)
                                bucket.append((j, ch, self.log(wt) - self.char_penalty
                                               - self.fuzzy_penalty, "char", key))
            rem = P[i:]
            if len(rem) <= MAX_SPAN and rem not in self.word_by_pinyin:
                for w, wt, nsyl, py in self._prefix_words(rem)[:wcap]:
                    bucket.append((n, w, self.log(wt) - self.char_penalty
                                   - self.partial_penalty, "partial", py))
                for ch, wt in self._prefix_chars(rem)[:ccap]:
                    bucket.append((n, ch, self.log(wt) - self.char_penalty
                                   - self.partial_penalty, "partial", key))
            if 2 <= len(rem) <= 8:
                for w, wt in self.initial_words.get(rem, [])[:WORD_EDGE_CAP]:
                    bucket.append((n, w, self.log(wt) - self.char_penalty
                                   - self.initial_penalty, "initial", key))
        return edges_from

    # ---------- 解码 ----------

    def decode(self, raw: str, context: str = "", rerank: bool = True) -> DecodeResult:
        """context: 已上屏文本, 供重排序模型做条件打分(未挂载时忽略)。
        rerank=False: 跳过神经重排(快速打字时用, 停顿后补一次)。"""
        P = raw.lower().replace("'", "").replace(" ", "")[:MAX_INPUT]
        n = len(P)
        result = DecodeResult()
        if n == 0:
            return result

        edges_from = self._build_edges(P)

        # 前向 Viterbi: best[j] = 从 0 到 j 的最优分
        NEG = float("-inf")
        best = [NEG] * (n + 1)
        back = [None] * (n + 1)   # back[j] = (i, text)
        best[0] = 0.0
        for i in range(n):
            if best[i] == NEG:
                continue
            for end, text, score, _kind, _syls in edges_from[i]:
                s = best[i] + score
                if s > best[end]:
                    best[end], back[end] = s, (i, text)

        # 终点: 全串可达则 n, 否则最远可达点
        reachable = [j for j in range(n + 1) if best[j] > NEG]
        target = max(reachable)
        result.remainder = P[target:]

        def path_text(j):
            parts = []
            while j > 0 and back[j] is not None:
                prev, text = back[j]
                parts.append(text)
                j = prev
            return "".join(reversed(parts))

        result.committed = path_text(target)
        result.consumed = target
        result.key = P
        if target == 0:
            return result  # 一个字母都用不掉, 全部透传

        # k-best 完整路径枚举(beam=60 经评测调得: 句 top1/在列 83.3%/88.3%,
        # 与旧变体机制持平且代码更简; beam=27 时句中歧义被首字变体挤出):
        # 候选不再只变首段——整句任意位置的次优读法(民/敏、在/再、书放/书房)
        # 都按路径总分直接进列表, 逐段按文本去重。取代"次优后缀+第二边变体"补丁。
        # 路径枚举深度: 200 为实测平衡点——词表外两字组合(先拿@167)
        # 可达列表深位(配合逐字组词+合成词学习), 整句解码 ~21ms
        beam = 200
        paths = [[] for _ in range(n + 1)]   # paths[j] = [(score, edges)]
        paths[0] = [(0.0, ())]
        for j in range(1, n + 1):
            merged = []
            for i in range(j):
                src = paths[i]
                if not src:
                    continue
                for end, text, score, kind, syls in edges_from[i]:
                    if end != j:
                        continue
                    for sc, edges in src:
                        merged.append((sc + score, edges + ((text, end, kind, syls),)))
            merged.sort(key=lambda x: -x[0])
            seen = set()
            for item in merged:
                key = "".join(t for t, _, _, _ in item[1])
                if key in seen:
                    continue
                seen.add(key)
                paths[j].append(item)
                if len(paths[j]) >= beam:
                    break

        # 候选 = 完整读法(整条上屏语义): 每条 k-best 路径就是一个候选,
        # 药丸显示完整读法, 选中即整条上屏(词表外组合如"硬编"一次选中)。
        # 候选不限量, 不设同首段配额——配额是"27 槽"时代的产物, 会把
        # 用户想要的低频读法(硬编)按纯词频挤掉; 翻页可达全部读法。
        entries = []
        for sc, edges in paths[target]:
            preview = "".join(t for t, _, _, _ in edges)
            pinyin = "'".join(" ".join(s for _, _, _, s in edges).split())
            entries.append((sc, preview, edges[0][2], pinyin, len(edges)))
        entries.sort(key=lambda e: -e[0])
        seen = set()
        if __import__('os').environ.get('IRIS_DBG'):
            hit = [(i+1, round(e[0], 2)) for i, e in enumerate(entries) if e[1] == '先拿']
            print(f"[dbg] entries={len(entries)} 先拿: {hit or '不在'}")
        for sc, preview, kind, pinyin, n_edges in entries:
            if preview in seen:
                continue
            seen.add(preview)
            result.candidates.append(
                Candidate(text=preview, commit_end=target, preview=preview,
                          score=sc, kind=kind, pinyin=pinyin,
                          single_word=(n_edges == 1 and kind in ("word", "char"))))
            if len(result.candidates) >= MAX_CANDIDATES:
                break
        if rerank and self.reranker is not None and result.remainder == "":
            self._apply_rerank(result, n, context)
        return result

    def _apply_rerank(self, result, n, context):
        """神经重排: 候选单位是完整读法(preview), 不是首段。

        answers_key: 进入本函数时 remainder 已为空, 所有 preview 都完整覆盖
        按键; 简拼边是预测性的, 不算应答。
        trusted: leader 是"词典对整个按键的应答"时不重排——词典自带语料词频,
        字级模型没有。判据: 首段单边覆盖全键且非简拼(词/字的精确命中, 以及
        末音节补全——评测集里 "aizengfenmin"→爱憎分明 这类少打尾字母的情况,
        补全边同样是词典应答; 不保护的话字级模型会把 418 个成语改坏); 另加
        预览整体恰为键对应词典词的兜底。多边拼装的 leader(整句)不受保护,
        重排收益正在这里。
        """
        cands = result.candidates
        key_words = [w for w, _, _ in self.word_by_pinyin.get(result.key, [])]

        def trusted(c):
            # 整条读法语义下所有候选都覆盖全键; "可信"仅指:
            # 单个词典词/字应答(自带语料词频), 或整条读法本身是词典词
            if c.single_word:
                return True
            return c.preview in key_words

        # 预筛: 只把路径分接近最优的候选送模型(12 nat 窗口——平衡
        # 长句批量评分从 ~120 条降到数十条
        if cands:
            top_sc = cands[0].score
            cands = [c for c in cands if c.score >= top_sc - 12.0] or cands[:9]
        facts = [(c.kind != "initial", trusted(c)) for c in cands]
        # 考虑集: 前 3 条(分数序)+ 全列表等距采样 6 条, 共 9 条——
        # 深位读法(书放在/硬编)也能获得模型评估, 不被分数序埋没
        # 全量评分(桌面端停顿补评可承受): 深位金标(书放在)不再被
        # 9 窗采样埋没; 门控仍拒绝可信 leader
        idx = self.reranker.best_of_all(context, [c.preview for c in cands], facts)
        if idx is not None:
            cands[0], cands[idx] = cands[idx], cands[0]
