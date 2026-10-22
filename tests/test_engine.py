"""引擎与交互控制器的单元测试。运行: python -m unittest tests.test_engine"""

import unittest

from ime import Engine
from ime.app import InputController


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine()

    def decode_top(self, pinyin):
        r = self.engine.decode(pinyin)
        return r, (r.candidates[0].preview if r.candidates else "")

    def test_known_words(self):
        for pinyin, gold in [("nihao", "你好"), ("zhongguo", "中国"),
                             ("shijian", "时间"), ("gongsi", "公司")]:
            _, top = self.decode_top(pinyin)
            self.assertEqual(top, gold, f"{pinyin} -> {top}")

    def test_sentence(self):
        _, top = self.decode_top("wojintianqushangban")
        self.assertEqual(top, "我今天去上班")
        _, top = self.decode_top("tazhengzaixiezuoye")
        self.assertEqual(top, "他正在写作业")

    def test_partial_pinyin(self):
        r = self.engine.decode("niha")
        self.assertTrue(any(c.preview.startswith("你") for c in r.candidates))

    def test_initials(self):
        r = self.engine.decode("nh")
        # rime-ice 权重下 你好 排第 8(女孩/你会…更高), 在候选列表内即可
        self.assertIn("你好", [c.text for c in r.candidates])
        r = self.engine.decode("zg")
        self.assertEqual(r.candidates[0].preview, "中国")

    def test_consumed_and_remainder(self):
        r = self.engine.decode("nihao")
        self.assertEqual(r.consumed, 5)
        self.assertEqual(r.remainder, "")
        r = self.engine.decode("vvv")
        self.assertEqual(r.remainder, "vvv")

    def test_no_crash_on_weird_input(self):
        for bad in ["", " ", "'", "vvv", "ii", "x", "a" * 100, "NIHAO", "ni'hao"]:
            self.engine.decode(bad)  # 不抛异常即可

    def test_case_and_separator_normalized(self):
        self.assertEqual(self.engine.decode("Ni'Hao").candidates[0].preview, "你好")


class ControllerTest(unittest.TestCase):
    def setUp(self):
        # 禁用学习: 共享的用户词文件会让断言依赖运行历史
        self.ctl = InputController(learn=False)

    def type(self, text):
        for ch in text:
            self.ctl.feed_key(ch, ch)

    def test_type_and_space_commits_sentence(self):
        self.type("nihao")
        self.ctl.feed_key("space")
        self.assertIn("你好", self.ctl.output)
        self.assertEqual(self.ctl.buffer, "")

    def test_digit_selects_first_segment(self):
        self.type("wojintianqushangban")
        self.ctl.feed_key("1", "1")
        self.assertEqual(self.ctl.output, "我")       # 只上屏首段
        self.assertEqual(self.ctl.buffer, "jintianqushangban")  # 余下编码继续

    def test_backspace_and_escape(self):
        self.type("abc")
        self.ctl.feed_key("BackSpace")
        self.assertEqual(self.ctl.buffer, "ab")
        self.ctl.feed_key("Escape")
        self.assertEqual(self.ctl.buffer, "")

    def test_enter_commits_chinese(self):
        self.type("nihao")
        self.ctl.feed_key("Return")
        self.assertIn("你好", self.ctl.output)
        self.assertEqual(self.ctl.buffer, "")

    def test_enter_unparseable_letters_raw_no_newline(self):
        # 命令/网址类无法解码的输入: 字母原样上屏, 不附加换行(终端安全)
        self.type("vvv")
        self.ctl.feed_key("Return")
        self.assertEqual(self.ctl.output, "vvv")

    def test_paging(self):
        self.type("shi")
        if self.ctl.page_count > 1:
            self.ctl.feed_key("Next", "")
            self.assertEqual(self.ctl.page, 1)
            first_of_page2 = self.ctl.result.candidates[9]
            self.ctl.feed_key("1", "1")
            self.assertEqual(self.ctl.output, first_of_page2.text)
        self.type("hao")          # 新编码应重置页码
        self.assertEqual(self.ctl.page, 0)

    def test_unknown_letters_stay_visible(self):
        self.type("vvv")
        self.assertEqual(self.ctl.buffer, "vvv")


if __name__ == "__main__":
    unittest.main()
