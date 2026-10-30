"""单字模式(双击退格)测试。

场景: 词表外两字词(硬编/先拿)要翻很久才能确认没有——双击退格把
候选列表切成单字优先, 逐字上屏后合成词学习自动记住词组。
"""

import tempfile
import time
import unittest
from pathlib import Path

from ime import Engine
from ime.app import InputController


class CharModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(learn=True, user_path=Path(self.tmp.name) / "u.json")
        self.ctl = InputController(engine=self.engine, use_reranker=False, learn=False)

    def tearDown(self):
        self.tmp.cleanup()

    def _type(self, s):
        for ch in s:
            self.ctl.feed_key(ch, ch)

    def _ctrl_backspace(self):
        self.ctl.feed_key("Control-BackSpace")

    def test_backspace_always_deletes(self):
        # 回归: 双击退格方案把快速连删劫持成模式切换, 退格删不掉字——
        # 退格现在必须是纯删除, 多快都不受时序影响
        self._type("yingbian")
        for _ in range(3):
            self.ctl.feed_key("BackSpace")
        self.assertEqual(self.ctl.buffer, "yingb")
        self.assertFalse(self.ctl.char_mode)
        time.sleep(0.5)
        self.ctl.feed_key("BackSpace")
        self.assertEqual(self.ctl.buffer, "ying")

    def test_ctrl_backspace_enters_char_mode(self):
        self._type("yingbian")
        self._ctrl_backspace()
        self.assertTrue(self.ctl.char_mode)
        # 解码截到首音节: 候选是单字, 缓冲原封不动
        self.assertEqual(self.ctl.buffer, "yingbian")
        c0 = self.ctl.result.candidates[0]
        self.assertEqual(len(c0.text), 1)
        self.assertEqual(c0.commit_end, 4)  # ying

    def test_ctrl_backspace_toggles_off(self):
        self._type("ying")
        self._ctrl_backspace()
        self._ctrl_backspace()
        self.assertFalse(self.ctl.char_mode)

    def test_ctrl_backspace_noop_without_buffer(self):
        self.assertFalse(self.ctl.feed_key("Control-BackSpace"))

    def _select_char(self, ch):
        idx = next(i for i, c in enumerate(self.ctl.result.candidates)
                   if c.text == ch)
        self.ctl._select(idx)

    def test_char_mode_persists_across_selection(self):
        # 逐字选: 硬 → 缓冲剩 bian, 第二个音节仍是单字
        self._type("yingbian")
        self._ctrl_backspace()
        self._select_char("硬")
        self.assertTrue(self.ctl.char_mode)
        self.assertEqual(self.ctl.buffer, "bian")
        c0 = self.ctl.result.candidates[0]
        self.assertEqual(len(c0.text), 1)
        self.assertEqual(c0.commit_end, 4)

    def test_compose_learn_after_char_flow(self):
        # 单字模式选 硬+编 后, 3 秒合成词学习应记住 硬编
        self._type("yingbian")
        self._ctrl_backspace()
        self._select_char("硬")
        self._select_char("编")
        self.assertIn("硬编", self.engine.user_counts)

    def test_char_mode_resets_when_buffer_empties(self):
        self._type("ying")
        self._ctrl_backspace()
        self.ctl.feed_key("Escape")
        self.assertFalse(self.ctl.char_mode)
        self._type("ni")
        self.assertFalse(self.ctl.char_mode)  # 不跨组合

    def test_rerank_skipped_in_char_mode(self):
        # settle_rerank 在单字模式下不启动(模型偏爱词组, 会挤掉点名要的单字)
        class FakeReranker:
            def best_of_all(self, ctx, texts, facts):
                raise AssertionError("单字模式不应调用重排")

        self.engine.reranker = FakeReranker()
        try:
            self._type("yingbian")
            self.ctl._rerank_pending = True
            self._ctrl_backspace()
            self.ctl.settle_rerank()
            self.ctl.poll_rerank()
        finally:
            self.engine.reranker = None


if __name__ == "__main__":
    unittest.main()
