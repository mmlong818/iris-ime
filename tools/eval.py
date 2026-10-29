"""在 chinese-ime-lm 评测集上评估解码准确率并调参。

用法:
  python tools/eval.py            # 用当前默认参数评估
  python tools/eval.py --grid     # 网格搜索 word_bonus / char_penalty
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ime import Engine  # noqa: E402

EVAL_DIR = ROOT.parent / "chinese-ime-lm" / "eval"
WORD_FILE = EVAL_DIR / "quanpin-words-v1.tsv"
SENT_FILE = EVAL_DIR / "sentences-v1.tsv"
WORD_SAMPLE = 2000  # 调参时抽样的词级用例数


def load_tsv(path):
    cases = []
    for line in open(path, encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 3 and parts[0] != "id":
            cases.append((parts[1], parts[2]))
    return cases


def accuracy(engine, cases):
    top1 = top5 = 0
    for pinyin, gold in cases:
        r = engine.decode(pinyin)
        texts = [c.preview for c in r.candidates]
        if texts and texts[0] == gold:
            top1 += 1
        if gold in texts[:5]:
            top5 += 1
    return top1 / len(cases), top5 / len(cases)


def evaluate(engine, words, sents, label=""):
    w1, w5 = accuracy(engine, words)
    s1, s5 = accuracy(engine, sents)
    print(f"{label:28s} 词 top1 {w1:6.1%} top5 {w5:6.1%} | 句 top1 {s1:6.1%} top5 {s5:6.1%}")
    return w1, s1


def main():
    words = load_tsv(WORD_FILE)
    sents = load_tsv(SENT_FILE)
    print(f"评测集: 词 {len(words)} 条, 句 {len(sents)} 条")
    engine = Engine()
    t0 = time.time()
    evaluate(engine, words[:WORD_SAMPLE], sents, "默认参数")
    print(f"耗时 {time.time()-t0:.1f}s")

    if "--grid" in sys.argv:
        word_sample = words[:WORD_SAMPLE]
        results = []
        for wb in (8.0, 10.0, 12.0, 14.0, 16.0):
            for cp in (8.0, 10.0, 12.0, 14.0, 16.0):
                engine.word_bonus, engine.char_penalty = wb, cp
                w1, _ = accuracy(engine, word_sample)
                s1, _ = accuracy(engine, sents)
                results.append((w1 + s1, wb, cp, w1, s1))
                print(f"word_bonus={wb:4.0f} char_penalty={cp:4.0f} -> "
                      f"词 top1 {w1:6.1%} | 句 top1 {s1:6.1%}")
        results.sort(reverse=True)
        print("\n最优组合:")
        for total, wb, cp, w1, s1 in results[:5]:
            print(f"  word_bonus={wb}, char_penalty={cp} -> 词 {w1:.1%}, 句 {s1:.1%}")


if __name__ == "__main__":
    main()
