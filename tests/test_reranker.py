"""神经重排序集成测试。权重或 scipy 缺失时整组跳过。"""

import unittest
from pathlib import Path

from ime import Engine

MODEL = Path(__file__).resolve().parent.parent / "models" / "sentence-model-4m.safetensors"


def deps_ok():
    if not MODEL.exists():
        return False
    try:
        import scipy  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(deps_ok(), "需要 models/sentence-model-4m.safetensors 与 scipy")
class RerankerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ime.reranker import Reranker, SentenceModel
        cls.model = SentenceModel(MODEL)
        cls.reranker = Reranker(cls.model)
        cls.engine = Engine()
        cls.engine.attach_reranker(MODEL)

    def test_natural_sentence_scores_higher(self):
        good, bad = self.model.score("", ["我今天去上班", "握紧天趣上班"])
        self.assertGreater(good, bad)

    def test_context_disambiguates(self):
        fang, fang2 = self.model.score("他把书", ["放在桌子上", "房在桌子上"])
        self.assertGreater(fang, fang2)

    def test_declines_trusted_leader(self):
        idx = self.reranker.best_where(
            "", ["时间", "事件", "实践"],
            [(True, True), (True, False), (True, False)])
        self.assertIsNone(idx)

    def test_promotes_better_reading(self):
        idx = self.reranker.best_where(
            "他把书", ["房在桌子上", "放在桌子上", "预测"],
            [(True, False), (True, False), (False, False)])
        self.assertEqual(idx, 1)

    def test_word_input_protected(self):
        r = self.engine.decode("shijian")
        self.assertEqual(r.candidates[0].preview, "时间")

    def test_idiom_completion_protected(self):
        # 评测集里少打尾字母的成语, 末音节补全应被视为词典应答, 不被重排拆坏
        r = self.engine.decode("aizengfenmin")
        self.assertEqual(r.candidates[0].preview, "爱憎分明")

    def test_sentence_disambiguation(self):
        # 金标在候选列表内(翻页/学习可达); 首位受模型与模糊音影响
        r = self.engine.decode("tabashufangzaizhuozishang")
        previews = [c.preview for c in r.candidates]
        self.assertIn("他把书放在桌子上", previews)

    def test_gold_in_candidates_without_rerank(self):
        self.engine.reranker = None
        try:
            r = self.engine.decode("qingwenxishoujianzainali")
            previews = [c.preview for c in r.candidates[:3]]
            self.assertIn("请问洗手间在哪里", previews)
        finally:
            self.engine.reranker = self.reranker

    def test_consideration_set_capped(self):
        # 长句 200 候选全量评分曾达 94s(25M), 客户端卡死根源——
        # 送模型的候选必须封顶, 且等距采样保住深位读法
        from ime.engine import RERANK_MAX
        scored = []
        target = self.engine.reranker.model  # 引擎实际持有的模型实例
        real_score = target.score

        def spy(context, texts):
            scored.append(len(texts))
            return real_score(context, texts)

        target.score = spy
        try:
            r = self.engine.decode("yizhiyigeyizhideyizhidouzaishuozenmeban")
        finally:
            target.score = real_score
        self.assertTrue(scored and scored[-1] <= RERANK_MAX)

    def test_async_rerank_does_not_block_and_guards_stale(self):
        # 停顿补排必须发生在后台线程, 且缓冲已变时结果被丢弃
        import threading
        import time as _t
        from ime.app import InputController

        ctl = InputController(engine=self.engine, learn=False)
        main_thread = threading.current_thread()
        seen_threads = []
        real = self.engine.rerank_candidates

        def slow_rerank(result, context):
            seen_threads.append(threading.current_thread())
            _t.sleep(0.2)
            return real(result, context)

        self.engine.rerank_candidates = slow_rerank
        for ch in "tabashufang":
            ctl.feed_key(ch, ch)
        t0 = _t.perf_counter()
        ctl.settle_rerank()
        self.assertLess(_t.perf_counter() - t0, 0.1, "settle_rerank 阻塞了主线程")
        self.assertIsNone(ctl._rerank_done)
        ctl.poll_rerank()  # 线程还在跑: 不动
        self.assertIsNone(ctl._rerank_done)
        _t.sleep(0.4)
        ctl.poll_rerank()
        self.assertIn("他把书", ctl.result.candidates[0].preview)
        self.assertTrue(any(t is not main_thread for t in seen_threads))

        # 缓冲已变 → 旧结果丢弃, 不串到新组合
        for ch in "ni":
            ctl.feed_key(ch, ch)
        ctl.feed_key("Escape")
        for ch in "nihao":
            ctl.feed_key(ch, ch)
        ctl.settle_rerank()
        _t.sleep(0.5)
        ctl.poll_rerank()
        self.assertIn("你好", ctl.result.candidates[0].preview)


if __name__ == "__main__":
    unittest.main()
