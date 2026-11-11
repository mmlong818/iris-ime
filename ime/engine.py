"""拼音整句解码引擎: 词格 DAG + Viterbi。

解码思路:
  1. 对输入串的每个位置 i, 找出所有从 i 出发的边——
     词典精确匹配(词/单字)、尾部前缀匹配(未打完的音节)、简拼(声母串)匹配;
  2. 在词格上做前向 Viterbi 得最优整句路径, 后向 DP 得每个位置到结尾的最优分;
  3. 候选 = 首段不同的各条路径, 按 (首段边分 + 首段之后最优分) 排序。
"""

import heapq
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
RERANK_TOP = 8          # 分数序直入重排考虑集的条数
RERANK_MAX = 24         # 考虑集上限: 评分成本 ~O(条数×字数), 长句全量(200 条)
                        # 25M 模型曾达 94s, 是客户端卡死的根源

# ---- 按键容错(借鉴 WowType): 四种纠错边 ----
# 相邻键按错/两键按反/多按一字母/漏按一字母, 只在音节级(span 不含隔音处理)。
# 纠错边仅在该跨度本身不是合法音节时生成(正确输入零影响、性能可控),
# 每条纠错边付 typo_penalty; 路径含纠错时若存在零纠错全解析则整体排后。
QWERTY_NEIGHBORS = {
    "q": "wa", "w": "qeas", "e": "wrsd", "r": "etdf", "t": "ryfg",
    "y": "tugh", "u": "yihj", "i": "uojk", "o": "ipl", "p": "ol",
    "a": "qwsz", "s": "aedxz", "d": "serfc", "f": "drtgv", "g": "ftyhb",
    "h": "gyujn", "j": "huikm", "k": "jiol", "l": "kop",
    "z": "asx", "x": "zasdc", "c": "xsfv", "v": "cfgb", "b": "vghn",
    "n": "bhjm", "m": "njk",
}
INITIALS = {"b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h", "j",
            "q", "x", "r", "z", "c", "s", "y", "w", "zh", "ch", "sh"}
# 注意: 元音字母(a/o/e/i/u/v)不在此列——它们本身是音节或不是声母,
# 混进缩写会拼出"中欧姑娘果"这类垃圾读法


def _edge_stream(src, text, end, kind, syls, score):
    """k-best 归并的单条入边流(立即绑定循环变量, 见 decode 调用处)。"""
    return ((sc + score, edges + ((text, end, kind, syls),))
            for sc, edges in src)


def _unit_zh(s):
    """贪婪把 zh/ch/sh 折算成 z/c/s(简拼联合声母键)。"""
    out = []
    i = 0
    while i < len(s):
        if s[i:i + 2] in ("zh", "ch", "sh"):
            out.append(s[i])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


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

        # 按键容错与省韵母混简拼(WowType 借鉴): 可关, 罚分可调
        self.typo = True
        self.typo_penalty = 8.0
        self.mixed = True
        self.mixed_penalty = 2.0       # 每个省略韵母的音节扣 2.0
        self.mixed_tail_penalty = 4.0  # 输入末字母当缩写声母再扣 4.0

        # 英文混打: code(小写) → 规范写法(github→GitHub); 与词库同目录
        self.english: dict = {}
        en_path = cache_path.parent / "english.txt"
        if en_path.exists():
            for line in en_path.read_text(encoding="utf-8").splitlines():
                parts = line.split("\t")
                if len(parts) == 2 and parts[0]:
                    self.english[parts[0]] = parts[1]

        # 缩写声母 → 最高频字(省韵母单边占位文本)
        self.initial_top_char: dict = {}
        for syl in self.syllables:
            chars = self.pinyin_chars.get(syl)
            if not chars:
                continue
            ini = syl[:2] if syl[:2] in ("zh", "ch", "sh") else syl[0]
            cur = self.initial_top_char.get(ini)
            if cur is None or chars[0][1] > cur[1]:
                self.initial_top_char[ini] = (chars[0][0], chars[0][1])

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
        if not self.learn_enabled or kind in ("initial", "english"):
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

    def _typo_variants(self, key):
        """跨度不是合法音节时, 四种单次编辑纠正 → 合法音节(≤3 个)。

        相邻键替换 / 两键按反 / 多按一字母(删一位) / 漏按一字母(插一位)。
        插入枚举量最大放最后, 命中够数即停。"""
        m = len(key)
        out = []
        for r in range(m):
            for nb in QWERTY_NEIGHBORS.get(key[r], ""):
                v = key[:r] + nb + key[r + 1:]
                if v in self.syl_set and v not in out:
                    out.append(v)
        for r in range(m - 1):
            if key[r] != key[r + 1]:
                v = key[:r] + key[r + 1] + key[r] + key[r + 2:]
                if v in self.syl_set and v not in out:
                    out.append(v)
        for r in range(m):
            v = key[:r] + key[r + 1:]
            if v in self.syl_set and v not in out:
                out.append(v)
        if len(out) < 3 and m <= 5:
            for r in range(m + 1):
                for ch in "abcdefghijklmnopqrstuvwxyz":
                    v = key[:r] + ch + key[r:]
                    if v in self.syl_set and v not in out:
                        out.append(v)
                        if len(out) >= 3:
                            return out
        return out[:3]

    def _mixed_word_edges(self, key):
        """省韵母混简拼词级匹配: 每段可打全拼或只打声母。

        key 递归切分成若干段(全拼音节 / 声母缩写), 按各段声母串查
        initial_words, 再校验全拼段必须等于词的真音节。
        返回 [(word, weight, nsyl, py, 缩写段数)]。"""
        results = []
        seen_w = set()

        def walk(pos, parts, abbrevs):
            if len(results) >= 12:
                return
            if pos == len(key):
                if not (1 <= len(parts) <= 4):
                    return
                # 逐字母声母约定(initial_words 构建同款): zh→z 即 p[0]
                ini = "".join(p[0] for p in parts)
                for w, wt, py in self.initial_words.get(ini, [])[:40]:
                    if w in seen_w:
                        continue
                    syls = py.split(" ")
                    if len(syls) != len(parts):
                        continue
                    ok = True
                    for p, s in zip(parts, syls):
                        # 全拼段须等于真音节; 缩写段=声母首字母,
                        # zh/ch/sh 联合缩写匹配 zh 开头的音节
                        if not (p == s or p == s[0]
                                or (len(p) == 2 and s.startswith(p))):
                            ok = False
                            break
                    if ok:
                        seen_w.add(w)
                        results.append((w, wt, len(syls), py, abbrevs))
                return
            rest = key[pos:]
            # 声母缩写段: 1-2 字母(zh/ch/sh)
            for ln in (2, 1):
                if len(rest) >= ln:
                    p = rest[:ln]
                    if p in INITIALS:
                        walk(pos + ln, parts + [p], abbrevs + 1)
            # 全拼段: 该起点开始的合法音节
            for ln in range(1, min(6, len(rest)) + 1):
                p = rest[:ln]
                if p in self.syl_set:
                    walk(pos + ln, parts + [p], abbrevs)

        walk(0, [], 0)
        # DFS 序 ≠ 权重序: 按词权排序后再截断, 否则高权正解(用中文)会被截掉
        results.sort(key=lambda x: -x[1])
        return results

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
                # 纠错边(WowType 借鉴): 跨度本身不是合法音节时尝试四种
                # 单次编辑(相邻键/换序/多按/漏按)。罚 typo_penalty; 分数
                # 竞争上浮(门控在 decode)。扇出收敛: 每变体字≤6/词≤2,
                # 变体≤3, 否则 k-best 枚举成本爆炸(实测 65ms→900ms)
                if self.typo and 2 <= j - i <= 6 and key not in self.syl_set:
                    for v in self._typo_variants(key):
                        for ch, wt in self.pinyin_chars.get(v, [])[:6]:
                            bucket.append((j, ch, self.log(wt) - self.char_penalty
                                           - self.typo_penalty, "typo", v))
                        for w, wt, nsyl, py in self._word_edges(v)[:2]:
                            bucket.append((j, w, self.log(wt) + self.word_bonus * (nsyl - 1)
                                           - self.char_penalty - self.typo_penalty, "typo", py))
                # 省韵母混简拼词级边: 每段可打全拼或只打声母(kyi→可以)。
                # 罚 每缩写段 mixed_penalty(末段缩写再+tail); 全拼可解析时
                # decode 门控丢弃。限制跨度长度控成本
                if self.mixed and 2 <= j - i <= 8 and key not in self.word_by_pinyin:
                    for w, wt, nsyl, py, n_abbrev in self._mixed_word_edges(key)[:6]:
                        pen = self.mixed_penalty * n_abbrev
                        if j == n and n_abbrev == nsyl:
                            pen += self.mixed_tail_penalty
                        bucket.append((j, w, self.log(wt) + self.word_bonus * (nsyl - 1)
                                       - self.char_penalty - pen, "abbrev", py))
                # 省韵母单边: 声母缩写占位(k→可), 供单字路径拼合
                if self.mixed and 1 <= j - i <= 2 and key in INITIALS \
                        and key not in self.syl_set:
                    ch, wt = self.initial_top_char.get(key, (None, 1))
                    if ch:
                        pen = self.mixed_penalty + \
                            (self.mixed_tail_penalty if j == n else 0.0)
                        bucket.append((j, ch, self.log(wt) - self.char_penalty
                                       - pen, "abbrev", key))
            rem = P[i:]
            if len(rem) <= MAX_SPAN and rem not in self.word_by_pinyin:
                for w, wt, nsyl, py in self._prefix_words(rem)[:wcap]:
                    bucket.append((n, w, self.log(wt) - self.char_penalty
                                   - self.partial_penalty, "partial", py))
                for ch, wt in self._prefix_chars(rem)[:ccap]:
                    bucket.append((n, ch, self.log(wt) - self.char_penalty
                                   - self.partial_penalty, "partial", key))
            if 2 <= len(rem) <= 8:
                # zh/ch/sh 当一个声母(WowType 借鉴): zhchi→支持(简拼键 zc)。
                # 贪婪替换有误伤可能(如 zhang→zang), 加 0.5 罚分兜底
                for alt, pen in ((rem, 0.0), (_unit_zh(rem), 0.5)):
                    if alt == rem:
                        continue
                    for w, wt, py in self.initial_words.get(alt, [])[:WORD_EDGE_CAP]:
                        bucket.append((n, w, self.log(wt) - self.char_penalty
                                       - self.initial_penalty - pen, "initial", key))
                for w, wt, py in self.initial_words.get(rem, [])[:WORD_EDGE_CAP]:
                    bucket.append((n, w, self.log(wt) - self.char_penalty
                                   - self.initial_penalty, "initial", key))
        # 同 (终点, 文本) 去重: 纠错/混简拼的多来源重复条目既浪费 k-best
        # 归并预算(同文本重复项挤占去重配额, 深位候选先拿会丢)又拖慢。
        # clean 边(非 typo/abbrev)优先保留——否则门控会把被折叠的干净
        # 读法一起丢掉(zg 的"中国"曾因此消失)
        for i, bucket in enumerate(edges_from):
            if len(bucket) > 1:
                best_clean, best_bad = {}, {}
                for e in bucket:
                    k = (e[0], e[1])
                    if e[3] in ("typo", "abbrev"):
                        if k not in best_bad or e[2] > best_bad[k][2]:
                            best_bad[k] = e
                    elif k not in best_clean or e[2] > best_clean[k][2]:
                        best_clean[k] = e
                merged = list(best_clean.values()) + [
                    v for k, v in best_bad.items() if k not in best_clean]
                edges_from[i] = sorted(merged, key=lambda e: -e[2])
        return edges_from

    # ---------- 解码 ----------

    def first_syllable(self, P: str) -> int:
        """P 的最长合法音节前缀长度(模糊音归一后匹配; 无则 0)。

        单字模式(客户端双击退格)用它把解码截到首音节——候选全是
        该音节的单字, 选中只上屏一个字, 余下编码留在缓冲逐字组词。
        """
        for k in range(min(len(P), 6), 0, -1):
            if P[:k] in self.syl_set or P[:k] in self.syl_canon:
                return k
        return 0

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
        # 双池 k-best(WowType 借鉴的配套改造): clean 池(无纠错/混简拼边,
        # beam 200, 语义与旧单池完全一致) + bad 池(含纠错/混简拼边, beam 50)。
        # 旧单池方案里 typo/abbrev 路径会把 clean 深位候选(先拿@167)挤出
        # beam, 而它们多数最终被门控丢弃——分池后互不挤占
        edges_by_end = [[] for _ in range(n + 1)]
        for i in range(n):
            for edge in edges_from[i]:
                edges_by_end[edge[0]].append((i, edge))
        paths_clean = [[] for _ in range(n + 1)]
        paths_bad = [[] for _ in range(n + 1)]
        paths_clean[0] = [(0.0, ())]
        for j in range(1, n + 1):
            incoming = edges_by_end[j]
            if not incoming:
                continue
            streams_clean, streams_bad = [], []
            for i, (end, text, score, kind, syls) in incoming:
                bad_edge = kind in ("typo", "abbrev")
                sc = _edge_stream(paths_clean[i], text, end, kind, syls, score)
                sb = _edge_stream(paths_bad[i], text, end, kind, syls, score)
                if bad_edge:
                    streams_bad.append(sc)
                    streams_bad.append(sb)
                else:
                    streams_clean.append(sc)
                    streams_bad.append(sb)
            for pools, streams, cap in ((paths_clean, streams_clean, beam),
                                        (paths_bad, streams_bad, 50)):
                if not streams:
                    continue
                seen = set()
                cnt = 0
                for item in heapq.merge(*streams, key=lambda x: -x[0]):
                    key = "".join(t for t, _, _, _ in item[1])
                    if key in seen:
                        continue
                    seen.add(key)
                    pools[j].append(item)
                    cnt += 1
                    if cnt >= cap:
                        break
            if __import__('os').environ.get('IRIS_DBG'):
                print(f"[dbg] pos {j}: clean={len(paths_clean[j])} "
                      f"bad={len(paths_bad[j])}")

        # 候选 = 完整读法(整条上屏语义): 每条 k-best 路径就是一个候选,
        # 药丸显示完整读法, 选中即整条上屏(词表外组合如"硬编"一次选中)。
        # 候选不限量, 不设同首段配额——配额是"27 槽"时代的产物, 会把
        # 用户想要的低频读法(硬编)按纯词频挤掉; 翻页可达全部读法。
        # 门控(WowType 借鉴): 存在零纠错全解析路径时, 含纠错路径整体排后
        # (只有纠错才能解析的输入它们才浮上来); 存在全拼全解析路径时,
        # 省韵母混简拼路径直接丢弃(正确输入零影响)。
        entries = []
        for sc, edges in (paths_clean[target] + paths_bad[target]):
            preview = "".join(t for t, _, _, _ in edges)
            pinyin = "'".join(" ".join(s for _, _, _, s in edges).split())
            n_typo = sum(1 for e in edges if e[2] == "typo")
            n_abbrev = sum(1 for e in edges if e[2] == "abbrev")
            n_initial = sum(1 for e in edges if e[2] == "initial")
            entries.append((sc, preview, edges[0][2], pinyin, len(edges),
                            n_typo, n_abbrev, n_initial, edges))
        entries.sort(key=lambda e: -e[0])
        # 全拼解析 = 无纠错/混简拼/简拼边(补全算词典应答, 不算)——
        # 简拼(作为儿女)是预测性读法, 它的存在不代表全拼走通了
        full_entries = [e for e in entries
                        if e[5] == 0 and e[6] == 0 and e[7] == 0]
        clean_entries = [e for e in entries if e[5] == 0 and e[6] == 0]
        best_clean = clean_entries[0][0] if clean_entries else None
        # 纠错路径按分竞争(WowType"纠错后明显更常见"条款): 分数不低于
        # 干净最优 -12 nat 的纠错读法按分混排(多按一字母 nihaoo→你好 这类
        # 字面合法但语义垃圾的解析会被纠错正解压过); 明显更差的排到最后,
        # 正确输入(无纠错边生成)不受任何影响
        typo_all = [e for e in entries if e[5] > 0 and e[6] == 0]
        typo_keep = [e for e in typo_all
                     if best_clean is None or e[0] >= best_clean - 12.0]
        typo_back = [e for e in typo_all if e not in typo_keep]
        # 省韵母混简拼门控: 只有存在全拼全解析路径时才丢混简拼——
        # 简拼/预测性读法(kind initial, 如 yongzwen 的"作为儿女")不算
        # 全拼走通, 混简拼按分正当竞争
        if full_entries:
            best_full = full_entries[0][0]
            typo_keep = [e for e in typo_keep if e[0] >= best_full - 12.0]
            # 多按前缀规则挂到全拼读法上
            pending = {}
            for te in typo_keep:
                for fe in full_entries[:60]:
                    if len(te[1]) < len(fe[1]) and fe[1].startswith(te[1]):
                        pending.setdefault(fe[1], []).append(te)
                        break
            # 只丢混简拼(e[6]>0); 简拼/纠错/其余 clean 读法按分保留——
            # 否则 initial 类读法(女孩/你好)会被整体丢掉
            keep = [e for e in entries
                    if e[6] == 0 and (e[5] == 0 or e in typo_keep)]
            emitted = set()
            ordered = []
            for e in sorted(keep, key=lambda e: -e[0]):
                for te in pending.get(e[1], []):
                    if te[1] not in emitted:
                        ordered.append(te)
                        emitted.add(te[1])
                if e[1] not in emitted:
                    ordered.append(e)
                    emitted.add(e[1])
            ordered += [e for e in typo_all if e not in typo_keep]
        else:
            # 全拼走不通: 纠错/混简拼/简拼读法按分竞争, 全保留
            ordered = entries
        seen = set()
        if __import__('os').environ.get('IRIS_DBG'):
            hit = [(i+1, round(e[0], 2)) for i, e in enumerate(entries) if e[1] == '先拿']
            print(f"[dbg] entries={len(entries)} 先拿: {hit or '不在'}")
        for sc, preview, kind, pinyin, n_edges, *_ in ordered:
            if preview in seen:
                continue
            seen.add(preview)
            result.candidates.append(
                Candidate(text=preview, commit_end=target, preview=preview,
                          score=sc, kind=kind, pinyin=pinyin,
                          single_word=(n_edges == 1 and kind in ("word", "char"))))
            if len(result.candidates) >= MAX_CANDIDATES:
                break

        # 神经重排只作用于中文整句读法; 句首词/英文在重排后定位插入
        if rerank and self.reranker is not None and result.remainder == "":
            self._apply_rerank(result, n, context)

        cands = result.candidates
        if cands:
            # 句首词(WowType 借鉴): 长输入第二位放整句开头最长段
            # (我今天晚上要加班 → 我今天晚上), 选中只上屏这一段、
            # 余下拼音留在缓冲继续
            top_edges = ordered[0][8] if ordered else ()
            if n >= 8 and len(top_edges) >= 2:
                # 句首词 = 整句开头到第一个多字词边界(网易叭哥说→网易,
                # 我今天晚上要加班→我今天晚上): 累积到 4 音节, 遇多字词
                # (nsyl≥2)即停——OOV 长串不把噪声尾巴带上
                tot, texts, syls, nsyl = 0, [], [], 0
                for t, end, _k, s in top_edges[:5]:
                    texts.append(t)
                    syls.append(s)
                    tot = end
                    nsyl += s.count(" ") + 1
                    if nsyl >= 4 or tot >= 10 or s.count(" ") >= 1:
                        break
                if 0 < tot < n:
                    head = "".join(texts)
                    if head not in seen and head != cands[0].preview:
                        cands.insert(1, Candidate(
                            text=head, commit_end=tot, preview=head,
                            score=ordered[0][0] - 0.1,
                            kind=top_edges[0][2], single_word=False,
                            pinyin="'".join(" ".join(syls).split())))
            # 英文混打: 词表命中按规范写法给候选(WowType 借鉴)——
            # 输入无干净全拼解析时排第一, 有则排第 6(前 5 留给中文)
            ew = self.english.get(P)
            if ew:
                pos = 5 if clean_entries else 0
                pos = min(pos, len(cands))
                if ew not in [c.preview for c in cands[:pos + 1]]:
                    cands.insert(pos, Candidate(
                        text=ew, commit_end=n, preview=ew, score=0.0,
                        kind="english", single_word=False))
            elif result.remainder:
                # 未登录串: 原样作为候选兜底(能选中上屏)
                cands.append(Candidate(
                    text=P, commit_end=n, preview=P, score=float("-inf") + 1,
                    kind="english", single_word=False))
        return result

    def rerank_candidates(self, result, context):
        """神经重排决策: 返回应置顶的候选下标, None 表示维持原序。

        候选单位是完整读法(preview), 不是首段。
        answers_key: remainder 为空时所有 preview 都完整覆盖按键; 简拼边是
        预测性的, 不算应答。
        trusted: leader 是"词典对整个按键的应答"时不重排——词典自带语料词频,
        字级模型没有。判据: 单个词典词/字的精确命中, 以及末音节补全——评测集
        里 "aizengfenmin"→爱憎分明 这类少打尾字母的情况, 补全边同样是词典
        应答, 不保护的话字级模型会把 418 个成语改坏; 另加预览整体恰为键对应
        词典词的兜底。多边拼装的 leader(整句)不受保护, 重排收益正在这里。
        考虑集: 12 nat 分数窗内, 前 RERANK_TOP 条(分数序)+ 等距采样补足到
        RERANK_MAX——深位读法(书放在/硬编)不被分数序埋没, 又约束模型成本。
        """
        cands = result.candidates
        if not (cands and self.reranker is not None and result.remainder == ""):
            return None
        key_words = [w for w, _, _ in self.word_by_pinyin.get(result.key, [])]

        def trusted(c):
            # 整条读法语义下所有候选都覆盖全键; "可信"仅指:
            # 单个词典词/字应答(自带语料词频), 或整条读法本身是词典词
            if c.single_word:
                return True
            return c.preview in key_words

        top_sc = cands[0].score
        cands = [c for c in cands if c.score >= top_sc - 12.0] or cands[:9]
        facts = [(c.kind != "initial", trusted(c)) for c in cands]
        sel = list(range(min(RERANK_TOP, len(cands))))
        if len(cands) > RERANK_TOP:
            rest = list(range(RERANK_TOP, len(cands)))
            step = max(1, -(-len(rest) // (RERANK_MAX - RERANK_TOP)))
            sel += rest[::step]
        idx = self.reranker.best_of_all(context, [cands[i].preview for i in sel],
                                        [facts[i] for i in sel])
        return sel[idx] if idx is not None else None

    def _apply_rerank(self, result, n, context):
        idx = self.rerank_candidates(result, context)
        if idx is not None:
            cands = result.candidates
            cands[0], cands[idx] = cands[idx], cands[0]
