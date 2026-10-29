"""用户词学习测试。"""

import tempfile
import unittest
from pathlib import Path

from ime import Engine
from ime.app import InputController


class UserLearningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "user_words.json"
        self.engine = Engine(learn=True, user_path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_learned_word_wins(self):
        # 打三次"limin"选"李敏"之后, 李敏应排到词典拼装(李+敏)与补全(李明)之前
        self.engine.learn("李敏", "limin", "word")
        self.engine.learn("李敏", "limin", "word")
        self.engine.learn("李敏", "limin", "word")
        r = self.engine.decode("limin")
        self.assertEqual(r.candidates[0].preview, "李敏")

    def test_learn_persists(self):
        self.engine.learn("张三丰", "zhangsanfeng", "word")
        again = Engine(learn=True, user_path=self.path)
        self.assertIn("张三丰", again.user_counts)

    def test_initials_not_learned(self):
        self.engine.learn("你好", "nh", "initial")
        self.assertEqual(self.engine.user_counts, {})

    def test_learn_disabled_by_default(self):
        plain = Engine(user_path=self.path)
        plain.learn("李敏", "limin", "word")
        self.assertFalse(self.path.exists())
        self.assertEqual(plain.user_counts, {})

    def test_controller_learns_on_commit(self):
        ctl = InputController(learn=True)
        ctl.engine = Engine(learn=True, user_path=self.path)
        for ch in "nihao":
            ctl.feed_key(ch, ch)
        ctl.feed_key("space")
        self.assertIn("你好", ctl.engine.user_counts)
        self.assertEqual(ctl.engine.user_counts["你好"]["key"], "nihao")


if __name__ == "__main__":
    unittest.main()


class PhraseCompositionTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.ctl = InputController(learn=True, use_reranker=False,
                                   user_path=Path(self.tmp.name) / "u.json")

    def tearDown(self):
        self.tmp.cleanup()

    def type(self, text):
        for ch in text:
            self.ctl.feed_key(ch, ch)

    def test_out_of_dict_word_in_candidates(self):
        # 词表没有"硬编": 整句读法候选中应包含它
        self.type("yingbian")
        self.assertTrue(any(c.preview == "硬编"
                            for c in self.ctl.result.candidates))

    def test_out_of_dict_word_select_and_learn(self):
        # 整条上屏语义: 选中"硬编"候选即整条上屏并被学习, 第二遍直达
        self.type("yingbian")
        hard = next(i for i, c in enumerate(self.ctl.result.candidates)
                    if c.preview == "硬编")
        self.ctl.feed_key(str(hard + 1), str(hard + 1))
        self.assertEqual(self.ctl.output, "硬编")
        self.assertIn("硬编", self.ctl.engine.user_counts)
        self.type("yingbian")                             # 第二遍直达
        self.assertEqual(self.ctl.result.candidates[0].preview, "硬编")
