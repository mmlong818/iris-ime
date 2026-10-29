"""盘古之白(中英文间加空格)规则测试。"""

import unittest

from ime.textstyle import space_text


class SpaceTextTest(unittest.TestCase):
    def test_cjk_latin_boundaries(self):
        self.assertEqual(space_text("我用python写代码"), "我用 python 写代码")
        self.assertEqual(space_text("你好world你好"), "你好 world 你好")
        self.assertEqual(space_text("我用Python写代码"), "我用 Python 写代码")

    def test_already_spaced_untouched(self):
        self.assertEqual(space_text("我用 Python 写代码"), "我用 Python 写代码")

    def test_digits_off_by_default(self):
        self.assertEqual(space_text("2019年是落地年"), "2019年是落地年")

    def test_digits_opt_in(self):
        self.assertEqual(space_text("第3版,2019年发布", space_digits=True),
                         "第 3 版,2019 年发布")

    def test_punctuation_not_triggered(self):
        # 中文标点与英文的接缝不动
        self.assertEqual(space_text("好,python"), "好,python")

    def test_cross_segment_junction(self):
        # 上一段以汉字结尾, 本段以英文开头 -> 补前导空格
        self.assertEqual(space_text("Python库", prev_tail="编辑"), " Python 库")
        # 上一段以英文结尾, 本段以汉字开头 -> 补前导空格
        self.assertEqual(space_text("很好用", prev_tail="IME"), " 很好用")
        # 不需要时不加
        self.assertEqual(space_text("很好用", prev_tail="写"), "很好用")

    def test_empty_and_ascii(self):
        self.assertEqual(space_text(""), "")
        self.assertEqual(space_text("hello world"), "hello world")


class ControllerSpaceTest(unittest.TestCase):
    def _make_ctl(self, **kw):
        import tempfile
        from pathlib import Path
        from ime.app import InputController
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return InputController(learn=False, use_reranker=False,
                               user_path=Path(tmp.name) / "u.json", **kw)

    def test_commit_applies_spacing_when_enabled(self):
        ctl = self._make_ctl(auto_space=True)
        ctl._commit("我用python写代码")
        self.assertEqual(ctl.output, "我用 python 写代码")     # 展示: 有空格
        self.assertEqual(ctl.committed, "我用python写代码")    # 学习/上下文: 原文

    def test_spacing_disabled_by_default(self):
        ctl = self._make_ctl()
        self.assertFalse(ctl.auto_space)                       # 需求已取消, 默认关
        ctl._commit("我用python写代码")
        self.assertEqual(ctl.output, "我用python写代码")


if __name__ == "__main__":
    unittest.main()
