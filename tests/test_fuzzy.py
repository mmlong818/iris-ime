"""模糊音(zh/z, ch/c, sh/s, n/l, ang/an, eng/en, ing/in)测试。"""

import unittest

from ime import Engine


class FuzzyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine()          # 默认开启
        cls.strict = Engine(fuzzy=False)

    def test_pingze_zh_z(self):
        # 南方口音: zici 想打 支持(zhichi)
        r = self.engine.decode("zici")
        previews = [c.preview for c in r.candidates]
        self.assertIn("支持", previews)

    def test_nl_initial(self):
        # lanhai 应能命中 男孩/南海(nan hai)
        r = self.engine.decode("lanhai")
        previews = [c.preview for c in r.candidates]
        self.assertTrue({"男孩", "南海"} & set(previews))

    def test_front_back_nasal(self):
        # mintian 想打 明天(mingtian): in/ing 模糊
        r = self.engine.decode("womintianqushangban")
        self.assertEqual(r.candidates[0].preview, "我明天去上班")

    def test_exact_still_wins(self):
        # 精确打字不被模糊命中挤掉
        self.assertEqual(self.engine.decode("shijian").candidates[0].preview, "时间")
        self.assertEqual(self.engine.decode("nihao").candidates[0].preview, "你好")

    def test_fuzzy_off_restores_strict(self):
        strict_top = [c.preview for c in self.strict.decode("sishi").candidates]
        self.assertIn("四十", strict_top[:3])
        self.assertNotIn("时事", strict_top)
        # 关闭后支持(zhichi)对 zici 不可见
        self.assertNotIn("支持", [c.preview for c in self.strict.decode("zici").candidates])

    def test_fuzzy_disabled_graceful(self):
        from ime.engine import Engine as E
        import tempfile
        from pathlib import Path
        # 无规范形数据的旧缓存: 模糊自动关闭, 不崩溃
        with tempfile.TemporaryDirectory() as tmp:
            import pickle
            cache = pickle.load(open("dicts/cache.pkl", "rb"))
            cache.pop("syl_canon", None)
            p = Path(tmp) / "old.pkl"
            p.write_bytes(pickle.dumps(cache))
            e = E(cache_path=p)
            self.assertFalse(e.fuzzy)


if __name__ == "__main__":
    unittest.main()
