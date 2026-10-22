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
