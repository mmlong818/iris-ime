"""WowType 借鉴特性测试: 按键容错/英文混打/省韵母混简拼/句首词。"""

import unittest

from ime import Engine


class TypoTest(unittest.TestCase):
    """按键容错: 四种纠错边, 零纠错读法存在时纠错不抢位。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = Engine()

    def test_transposition(self):
        r = self.engine.decode("zhognguo", rerank=False)
        self.assertIn("中国", [c.preview for c in r.candidates[:8]])

    def test_extra_key(self):
        # 多按一字母: 字面合法但语义垃圾的解析(你好哦)被纠错正解压过
        r = self.engine.decode("nihaoo", rerank=False)
        self.assertEqual(r.candidates[0].preview, "你好")

    def test_missing_key(self):
        r = self.engine.decode("zhonguo", rerank=False)
        self.assertIn("中国", [c.preview for c in r.candidates[:10]])

    def test_adjacent_key(self):
        r = self.engine.decode("zhonggup", rerank=False)
        self.assertTrue(any(c.preview.startswith("钟鼓")
                            for c in r.candidates[:8]))

    def test_correct_input_unaffected(self):
        # 正确输入没有纠错边生成, 基线不受影响
        r = self.engine.decode("nihao", rerank=False)
        self.assertEqual(r.candidates[0].preview, "你好")


class EnglishTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine()

    def test_english_word_top(self):
        r = self.engine.decode("github", rerank=False)
        self.assertEqual(r.candidates[0].preview, "GitHub")

    def test_pinyin_ambiguous_english_behind_chinese(self):
        # 本身是合法拼音的英文词排后(前 5 留给中文), 但必须在列表内
        r = self.engine.decode("she", rerank=False)
        texts = [c.preview for c in r.candidates]
        self.assertIn("she", texts)
        self.assertNotEqual(texts[0], "she")


class MixedShorthandTest(unittest.TestCase):
    """省韵母混简拼: 每段可打全拼或只打声母。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = Engine()

    def test_kyi(self):
        r = self.engine.decode("kyi", rerank=False)
        self.assertEqual(r.candidates[0].preview, "可以")

    def test_zhchi(self):
        # zh/ch/sh 联合缩写
        r = self.engine.decode("zhchi", rerank=False)
        self.assertIn("支持", [c.preview for c in r.candidates[:2]])

    def test_yongzwen(self):
        r = self.engine.decode("yongzwen", rerank=False)
        self.assertEqual(r.candidates[0].preview, "用中文")

    def test_full_pinyin_unaffected(self):
        # 全拼可解析时混简拼路径被门控丢弃
        r = self.engine.decode("nihao", rerank=False)
        self.assertEqual(r.candidates[0].preview, "你好")


class HeadWordTest(unittest.TestCase):
    """句首词候选: 长输入第二位放整句开头段, 选中只上屏一段。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = Engine()

    def test_head_word_second(self):
        r = self.engine.decode("wojintianwanshangyaojiaban", rerank=False)
        self.assertGreaterEqual(len(r.candidates), 3)
        head = r.candidates[1]
        self.assertEqual(head.preview, "我今天晚上")
        self.assertLess(head.commit_end, len("wojintianwanshangyaojiaban"))

    def test_short_input_no_head(self):
        r = self.engine.decode("nihao", rerank=False)
        self.assertEqual(r.candidates[1].preview, "拟好")  # 无句首词插入


class DedupRegressionTest(unittest.TestCase):
    """(终点,文本) 去重必须保 clean 边: zg 的"中国"曾因被 abbrev 边
    折叠后在门控中一起消失。"""

    def test_zg_china_first(self):
        eng = Engine()
        r = eng.decode("zg", rerank=False)
        self.assertEqual(r.candidates[0].preview, "中国")


if __name__ == "__main__":
    unittest.main()
