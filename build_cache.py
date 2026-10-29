"""把 rime-ime 词库 YAML 编译成引擎用的紧凑缓存 (dicts/cache.pkl)。

产出结构:
  syllables       : 有效拼音音节集合
  pinyin_chars    : {音节: [(汉字, 权重), ...]} 按权重降序
  word_by_pinyin  : {无空格拼音串: [(词, 权重, 带空格拼音), ...]} 按权重降序
  initial_words   : {声母串(简拼): [(词, 权重), ...]} 2~6 字词, 每键取前 N 个

用法: python build_cache.py
"""

import pickle
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DICTS_DIR = ROOT / "dicts"
CACHE_PATH = DICTS_DIR / "cache.pkl"

SYLLABLE_RE = re.compile(r"[a-z]+")
MAX_PER_PINYIN = 120   # 同音词条上限, 超出的低频词丢弃
MAX_PER_INITIALS = 40  # 同简拼词条上限
MAX_INITIALS_WORD_LEN = 6
CHAR_TOP = 40          # 规范形音节下的单字合并上限


def parse_rime_yaml(path: Path):
    """解析 rime 词表: 跳过 YAML 头, 产出 (文本, 音节列表, 权重)。"""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            if line in ("---", "..."):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            text = parts[0].strip()
            syllables = parts[1].lower().split()
            if not text or not syllables:
                continue
            if not all(SYLLABLE_RE.fullmatch(s) for s in syllables):
                continue
            weight_part = parts[2].strip() if len(parts) > 2 else ""
            try:
                weight = int(weight_part) if weight_part else 1
            except ValueError:
                weight = 1
            yield text, syllables, weight


def canonical_syllable(s):
    """模糊音规范形: zh/ch/sh→z/c/s, 声母 n→l, 韵母 ang/eng/ing→an/en/in。

    只作用于声母与韵尾, 不碰音节中段的字母——min 的 m 与中段 n 均不动,
    ming→lin(in→in? 不, ing→in 且 n→l 仅声母) 等组合才是标准模糊音语义。
    """
    initials = "bpmfdtnlgkhjqxrzcswyw"
    if s[:2] in ("zh", "ch", "sh"):
        ini, final = s[0], s[2:]
    elif s[0] in initials and len(s) > 1:
        ini, final = s[0], s[1:]
    else:
        ini, final = "", s
    if ini == "n":
        ini = "l"
    if final.endswith("ang") or final.endswith("eng") or final.endswith("ing"):
        final = final[:-1]
    return ini + final


def main():
    char_files = [DICTS_DIR / "8105.dict.yaml"]
    word_files = [DICTS_DIR / "base.dict.yaml"]

    syllables = set()
    char_weight = {}          # (char, pinyin) -> max weight
    word_weight = {}          # (word, tuple(syllables)) -> max weight

    for path in char_files:
        if not path.exists():
            print(f"缺少字表: {path}", file=sys.stderr)
            sys.exit(1)
        count = 0
        for text, syls, w in parse_rime_yaml(path):
            if len(text) != 1:
                continue  # 字表只收单字
            key = (text, " ".join(syls))
            char_weight[key] = max(char_weight.get(key, 0), w)
            syllables.update(syls)
            count += 1
        print(f"{path.name}: {count} 条字音")

    for path in word_files:
        if not path.exists():
            print(f"缺少词表: {path}", file=sys.stderr)
            sys.exit(1)
        count = 0
        for text, syls, w in parse_rime_yaml(path):
            key = (text, tuple(syls))
            word_weight[key] = max(word_weight.get(key, 0), w)
            syllables.update(syls)
            count += 1
        print(f"{path.name}: {count} 条词条")

    pinyin_chars = {}
    for (ch, py), w in char_weight.items():
        pinyin_chars.setdefault(py, []).append((ch, max(w, 1)))
    for lst in pinyin_chars.values():
        lst.sort(key=lambda x: -x[1])

    # 口语读音别名: 这 zhei(词表按规范音只标 zhe, 但日常打字常输 zhei)
    for alias_py, (ch, std_py) in {"zhei": ("这", "zhe")}.items():
        for c, w in pinyin_chars.get(std_py, []):
            if c == ch:
                pinyin_chars.setdefault(alias_py, []).append((c, w))
                break
        syllables.add(alias_py)

    word_by_pinyin = {}
    for (word, syls), w in word_weight.items():
        key = "".join(syls)
        word_by_pinyin.setdefault(key, []).append((word, max(w, 1), " ".join(syls)))
    for key, lst in word_by_pinyin.items():
        lst.sort(key=lambda x: -x[1])
        if len(lst) > MAX_PER_PINYIN:
            word_by_pinyin[key] = lst[:MAX_PER_PINYIN]

    initial_words = {}
    for (word, syls), w in word_weight.items():
        n = len(word)
        if not (2 <= n <= MAX_INITIALS_WORD_LEN):
            continue
        initials = "".join(s[0] for s in syls)
        initial_words.setdefault(initials, []).append((word, max(w, 1)))
    for key, lst in initial_words.items():
        lst.sort(key=lambda x: -x[1])
        if len(lst) > MAX_PER_INITIALS:
            initial_words[key] = lst[:MAX_PER_INITIALS]

    # 模糊音规范形索引: 每个词/字在"规范拼音"键下再挂一份,
    # 引擎输入侧做同样的规范化后即可交叉命中(zhang→zan, nan→lan, ming→lin...)
    syl_canon = {s: canonical_syllable(s) for s in syllables}
    canon_words = {}
    for (word, syls), w in word_weight.items():
        ck = "".join(syl_canon.get(s, s) for s in syls)
        canon_words.setdefault(ck, []).append((word, max(w, 1), " ".join(syls)))
    for key, lst in canon_words.items():
        lst.sort(key=lambda x: -x[1])
        if len(lst) > MAX_PER_PINYIN:
            canon_words[key] = lst[:MAX_PER_PINYIN]
    canon_chars = {}
    for py, lst in pinyin_chars.items():
        ck = syl_canon.get(py, py)
        merged = {ch: w for ch, w in canon_chars.get(ck, [])}
        for ch, w in lst:
            merged[ch] = max(merged.get(ch, 0), w)
        canon_chars[ck] = sorted(merged.items(), key=lambda x: -x[1])[:CHAR_TOP]

    cache = {
        "syllables": syllables,
        "pinyin_chars": pinyin_chars,
        "word_by_pinyin": word_by_pinyin,
        "initial_words": initial_words,
        "syl_canon": syl_canon,
        "canon_words": canon_words,
        "canon_chars": canon_chars,
    }
    with open(CACHE_PATH, "wb") as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)

    total_words = sum(len(v) for v in word_by_pinyin.values())
    print(f"音节 {len(syllables)} 个 | 字音 {len(char_weight)} 条 | 词条 {total_words} 条 | "
          f"简拼键 {len(initial_words)} 个")
    print(f"缓存已写入 {CACHE_PATH}")


if __name__ == "__main__":
    main()
