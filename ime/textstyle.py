"""中英文混排整形: 汉字与英文字母之间加半角空格("盘古之白")。

拍板的规则(与 pangu.js / 微软拼音"自动空格"对齐的保守子集):
  1. 汉字 ↔ 英文字母: 加半角空格(默认开);
  2. 汉字 ↔ 阿拉伯数字: 默认不加("2019年" 保持原样), space_digits=True 可开;
  3. 标点不触发: 中文标点与英文之间的接缝保持原样, 避免过度加空格;
  4. 已有空格的接缝不重复加;
  5. 跨段接缝: 逐段上屏时, 段首根据上一段末字符决定是否补前导空格
     (已上屏的文本无法修改, 只能在新段前补)。
"""

import re

CJK = "\u3400-\u4dbf\u4e00-\u9fff"
LETTER = "A-Za-z"
DIGIT = "0-9"


def _boundary_rules(space_digits):
    """返回 [(前类, 后类)] —— 需要插空格的相邻字符类。"""
    rules = [(CJK, LETTER), (LETTER, CJK)]
    if space_digits:
        rules += [(CJK, DIGIT), (DIGIT, CJK)]
    return rules


def _is_cjk(ch):
    return bool(re.match(f"[{CJK}]", ch))


def _is_latin(ch):
    return bool(re.match(f"[{LETTER}]", ch))


def _is_digit(ch):
    return bool(re.match(f"[{DIGIT}]", ch))


def _junction_needs_space(a, b, space_digits):
    pairs = {
        (_is_cjk(a), _is_latin(b)),
        (_is_latin(a), _is_cjk(b)),
    }
    if space_digits:
        pairs |= {(_is_cjk(a), _is_digit(b)), (_is_digit(a), _is_cjk(b))}
    return any(x and y for x, y in pairs)


def space_text(text, prev_tail="", space_digits=False):
    """给 text 加盘古空格; prev_tail 是上一段已上屏文本的末尾(可为空)。"""
    if not text:
        return text
    out = text
    for left, right in _boundary_rules(space_digits):
        # 零宽断言不消费字符, 一趟处理所有相邻边界
        out = re.sub(f"(?<=[{left}])(?=[{right}])", " ", out)
    # 跨段接缝: 上一段末字符与本段首字符之间
    if prev_tail and out and not out[0].isspace():
        if _junction_needs_space(prev_tail[-1], out[0], space_digits):
            out = " " + out
    return out
