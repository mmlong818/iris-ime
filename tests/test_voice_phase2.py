"""语音 Phase 2: 应用感知/直译/热词与纠错表/鼠标侧键/换行上屏策略,
外加 alpha.16 的 tick 链回归。ASR 与润色全部 mock, 不需要 funasr。
"""

import ctypes
import json
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ime import global_ime as gi
from ime.global_ime import KeyboardHook
from ime.voice.config import DEFAULTS, VoiceConfig
from ime.voice.polish import (PROFILE_NEWLINE, apply_corrections, build_prompt,
                              build_translate_prompt)
from ime.voice.voice_ime import VoiceIme, detect_profile


class ProfileTest(unittest.TestCase):
    def test_default_map(self):
        self.assertEqual(detect_profile("claude.exe"), "coding")
        self.assertEqual(detect_profile("WeChat.exe"), "chat")
        self.assertEqual(detect_profile("OUTLOOK.EXE"), "email")
        self.assertEqual(detect_profile("windowsterminal.exe"), "terminal")
        self.assertEqual(detect_profile("notepad.exe"), "default")
        self.assertEqual(detect_profile(None), "default")

    def test_user_profiles_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "voice.json"
            p.write_text(json.dumps({"profiles": {"notepad.exe": "email",
                                                  "wechat.exe": "default"}}),
                         encoding="utf-8")
            cfg = VoiceConfig(path=p)
            self.assertEqual(detect_profile("notepad.exe", cfg), "email")
            self.assertEqual(detect_profile("wechat.exe", cfg), "default")
            self.assertEqual(detect_profile("claude.exe", cfg), "coding")

    def test_newline_strategy_per_profile(self):
        # 聊天/AI 对话框回车=发送, 终端回车=提交: 不能原样打回车
        self.assertEqual(PROFILE_NEWLINE["chat"], "shift_enter")
        self.assertEqual(PROFILE_NEWLINE["coding"], "shift_enter")
        self.assertEqual(PROFILE_NEWLINE["terminal"], "flat")
        self.assertEqual(PROFILE_NEWLINE["default"], "enter")


class HotwordCorrectionTest(unittest.TestCase):
    def test_hotwords_in_prompts(self):
        p = build_prompt("用瑞迪斯做缓存", hotwords=["Redis", " ", "鸢尾"])
        self.assertIn("专有名词表", p["content"])
        self.assertIn("Redis、鸢尾", p["content"])
        self.assertNotIn("专有名词表", build_prompt("你好")["content"])
        t = build_translate_prompt("用瑞迪斯做缓存", hotwords=["Redis"])
        self.assertIn("Redis", t["content"])

    def test_corrections_longest_first(self):
        fixed = apply_corrections(
            "鸢尾输入发和输入发", {"输入发": "输入法", "鸢尾输入发": "Iris IME"})
        self.assertEqual(fixed, "Iris IME和输入法")
        self.assertEqual(apply_corrections("原样", None), "原样")


class ConfigTest(unittest.TestCase):
    def test_new_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = VoiceConfig(path=Path(tmp) / "voice.json")
            self.assertEqual(cfg.ptt_vk, [163])          # 只认右 Ctrl
            self.assertEqual(cfg.translate_vk, [165])    # 右 Alt
            self.assertEqual(cfg.ptt_mouse, 0)
            self.assertEqual(cfg.hotwords, [])

    def test_legacy_default_keys_migrated(self):
        # Phase 1 默认 [163,162] 把左 Ctrl 也当 PTT(Ctrl+C/V 被吞): 迁移;
        # 用户自己改过的键位不动
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "voice.json"
            p.write_text(json.dumps({"ptt_vk": [163, 162],
                                     "translate_vk": [164, 161]}),
                         encoding="utf-8")
            cfg = VoiceConfig(path=p)
            self.assertEqual(cfg.ptt_vk, [163])
            self.assertEqual(cfg.translate_vk, [165])
            p.write_text(json.dumps({"ptt_vk": [119]}), encoding="utf-8")
            self.assertEqual(VoiceConfig(path=p).ptt_vk, [119])

    def test_bad_mouse_value_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "voice.json"
            p.write_text(json.dumps({"ptt_mouse": "x"}), encoding="utf-8")
            self.assertEqual(VoiceConfig(path=p).ptt_mouse, 0)


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = json.loads(json.dumps(DEFAULTS))
        data["enabled"] = True
        data["llm"]["api_key"] = "test-key"
        cfg = VoiceConfig(path=Path(self.tmp.name) / "voice.json")
        cfg.data = data
        self.outputs, self.states = [], []
        self.voice = VoiceIme(
            cfg, on_state=lambda s, i: self.states.append((s, i)),
            on_output=lambda text, raw: self.outputs.append((text, raw)),
            history_dir=Path(self.tmp.name) / "history")
        wav = Path(self.tmp.name) / "fake.wav"
        wav.write_bytes(b"RIFF")
        self.voice.recorder = mock.Mock()
        self.voice.recorder.stop = mock.Mock(return_value=wav)
        self.voice.asr = mock.Mock()
        self.voice.polisher = mock.Mock()

    def tearDown(self):
        self.tmp.cleanup()

    def _speak(self, raw, until="idle", **kw):
        self.voice.asr.transcribe = mock.Mock(return_value=raw)
        self.voice.ptt_down(**kw)
        self.voice.ptt_up()
        deadline = time.time() + 5
        while self.voice.state != until and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.voice.state, until)

    def test_translate_mode_calls_translate(self):
        self.voice.polisher.translate = mock.Mock(
            return_value="Nice weather today.")
        self._speak("今天天气不错", mode="translate", exe="wechat.exe")
        self.assertEqual(self.outputs, [("Nice weather today.", "今天天气不错")])
        self.voice.polisher.polish.assert_not_called()
        self.assertEqual(self.voice.last_meta["mode"], "translate")
        self.assertEqual(self.voice.last_meta["newline"], "shift_enter")

    def test_translate_without_key_outputs_raw_and_warns(self):
        self.voice.cfg.llm["api_key"] = ""
        self._speak("今天天气不错", until="error", mode="translate")
        self.assertEqual(self.outputs, [("今天天气不错", "今天天气不错")])
        self.assertIn("API Key", self.states[-1][1]["message"])

    def test_translate_failure_outputs_raw_and_warns(self):
        self.voice.polisher.translate = mock.Mock(
            side_effect=RuntimeError("network down"))
        self._speak("今天天气不错", until="error", mode="translate")
        self.assertEqual(self.outputs, [("今天天气不错", "今天天气不错")])

    def test_profile_hotwords_and_corrections_reach_polisher(self):
        self.voice.cfg.asr["hotwords"] = ["Redis"]
        self.voice.cfg.data["corrections"] = {"瑞迪斯": "Redis"}
        self.voice.polisher.polish = mock.Mock(return_value="1. 用 Redis 做缓存")
        self._speak("用瑞迪斯做缓存", exe="Cursor.exe")
        self.voice.polisher.polish.assert_called_once_with(
            "用Redis做缓存", profile="coding", hotwords=["Redis"])
        self.assertEqual(self.outputs[0][1], "用Redis做缓存")
        self.assertEqual(self.voice.last_meta["profile"], "coding")

    def test_corrections_apply_without_llm(self):
        self.voice.cfg.llm["api_key"] = ""
        self.voice.cfg.data["corrections"] = {"瑞迪斯": "Redis"}
        self._speak("用瑞迪斯")
        self.assertEqual(self.outputs, [("用Redis", "用Redis")])

    def test_history_records_mode_and_profile(self):
        self.voice.polisher.polish = mock.Mock(return_value="成稿")
        self._speak("原稿", exe="outlook.exe")
        files = list((Path(self.tmp.name) / "history").glob("*.jsonl"))
        rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((rec["mode"], rec["profile"], rec["exe"]),
                         ("dictate", "email", "outlook.exe"))


class HookKeyTest(unittest.TestCase):
    def _handle(self, hook, vk, down):
        kb = gi.KBDLLHOOKSTRUCT()
        kb.vkCode = vk
        with mock.patch.object(hook, "_hook", 0), \
                mock.patch.object(gi.user32, "CallNextHookEx", return_value=0):
            return hook._handle(0, gi.WM_KEYDOWN if down else gi.WM_KEYUP,
                                ctypes.addressof(kb))

    def _make(self):
        events = queue.Queue()
        st = {"enabled": False, "buffer": "", "voice_enabled": True,
              "voice_ptt": [163], "voice_translate_vk": [165],
              "passthrough": set()}
        return KeyboardHook(events, st), events

    def test_left_ctrl_not_hijacked(self):
        # 左 Ctrl(162) 不是 PTT: Ctrl+C/V 必须照常可用
        hook, events = self._make()
        self.assertNotEqual(self._handle(hook, 162, True), 1)
        with self.assertRaises(queue.Empty):
            events.get_nowait()

    def test_translate_key_events(self):
        hook, events = self._make()
        self.assertEqual(self._handle(hook, 165, True), 1)
        self.assertEqual(events.get_nowait(),
                         ("voice_ptt_down", "translate", ""))
        self._handle(hook, 165, False)
        self.assertEqual(events.get_nowait(), ("voice_ptt_up", None, ""))

    def test_release_only_by_the_key_that_started(self):
        hook, events = self._make()
        self._handle(hook, 163, True)
        self._handle(hook, 165, True)    # 听写中碰到直译键: 吞掉, 不串台
        self._handle(hook, 165, False)   # 直译键松开: 不结束听写
        self.assertEqual(events.get_nowait(), ("voice_ptt_down", None, ""))
        with self.assertRaises(queue.Empty):
            events.get_nowait()
        self._handle(hook, 163, False)
        self.assertEqual(events.get_nowait(), ("voice_ptt_up", None, ""))


class MouseHookTest(unittest.TestCase):
    """鼠标侧键 PTT: 只认配置的那个侧键, 吞掉事件(不触发浏览器后退)。"""

    def _fire(self, hook, msg, button, flags=0):
        ms = gi.MSLLHOOKSTRUCT()
        ms.mouseData = button << 16
        ms.flags = flags
        return hook._handle(msg, ctypes.addressof(ms))

    def _make(self, **kw):
        events = queue.Queue()
        st = {"voice_enabled": True, "voice_mouse": 1, "passthrough": set(),
              **kw}
        return gi.MouseHook(events, st), events, st

    def test_side_button_down_up(self):
        hook, events, st = self._make()
        self.assertTrue(self._fire(hook, hook.WM_XBUTTONDOWN, 1))
        self.assertEqual(events.get_nowait(), ("voice_ptt_down", None, ""))
        self.assertTrue(self._fire(hook, hook.WM_XBUTTONUP, 1))
        self.assertEqual(events.get_nowait(), ("voice_ptt_up", None, ""))
        self.assertFalse(st["ptt_held"])

    def test_other_button_and_off_pass_through(self):
        hook, events, _ = self._make()
        self.assertFalse(self._fire(hook, hook.WM_XBUTTONDOWN, 2))
        hook2, events2, _ = self._make(voice_mouse=0)
        self.assertFalse(self._fire(hook2, hook2.WM_XBUTTONDOWN, 1))
        hook3, events3, _ = self._make(voice_enabled=False)
        self.assertFalse(self._fire(hook3, hook3.WM_XBUTTONDOWN, 1))
        for q in (events, events2, events3):
            with self.assertRaises(queue.Empty):
                q.get_nowait()

    def test_injected_and_passthrough_ignored(self):
        hook, events, _ = self._make()
        self.assertFalse(self._fire(hook, hook.WM_XBUTTONDOWN, 1,
                                    flags=hook.LLMHF_INJECTED))
        hook2, events2, _ = self._make(passthrough={"remote.exe"})
        with mock.patch.object(gi, "foreground_exe", return_value="remote.exe"):
            self.assertFalse(self._fire(hook2, hook2.WM_XBUTTONDOWN, 1))
        for q in (events, events2):
            with self.assertRaises(queue.Empty):
                q.get_nowait()

    def test_non_button_messages_skip_handler(self):
        # 鼠标移动等高频消息: 回调首行分流, 不进 _handle
        hook, _, _ = self._make()
        with mock.patch.object(hook, "_handle") as h, \
                mock.patch.object(gi.user32, "CallNextHookEx", return_value=0):
            hook._callback(0, 0x0200, 0)   # WM_MOUSEMOVE
        h.assert_not_called()


class SendTextLinesTest(unittest.TestCase):
    def test_flat_joins_lines(self):
        with mock.patch.object(gi, "send_text") as st:
            gi.send_text_lines("1. 加按钮\n\n2. 改颜色\n", "flat")
        st.assert_called_once_with("1. 加按钮 2. 改颜色")

    def test_enter_sends_verbatim(self):
        with mock.patch.object(gi, "send_text") as st:
            gi.send_text_lines("第一段\n第二段", "enter")
        st.assert_called_once_with("第一段\n第二段")

    def test_shift_enter_between_lines(self):
        with mock.patch.object(gi, "send_text") as st, \
                mock.patch.object(gi.user32, "SendInput") as si:
            gi.send_text_lines("第一行\n第二行\n第三行", "shift_enter")
        self.assertEqual([c.args[0] for c in st.call_args_list],
                         ["第一行", "第二行", "第三行"])
        self.assertEqual(si.call_count, 2)          # 两次软换行
        self.assertEqual(si.call_args.args[0], 4)   # Shift↓ Enter↓ Enter↑ Shift↑


class TickChainTest(unittest.TestCase):
    """alpha.16 回归: 重排定被误挪进 _track_foreground, 客户端每 1.5 秒多出
    一条永续 tick 链, 主线程被重绘占满(设置窗/状态条拖不动)。"""

    def _fake(self):
        fake = mock.Mock()
        fake.events = queue.Queue()
        fake.last_fg_app = None
        fake.state = {}
        return fake

    def test_track_foreground_never_schedules(self):
        fake = self._fake()
        fake._fg_ts = 0.0
        with mock.patch.object(gi, "foreground_exe", return_value="notepad.exe"):
            gi.GlobalIme._track_foreground(fake)
        fake.root.after.assert_not_called()
        self.assertEqual(fake.last_fg_app, "notepad.exe")

    def test_tick_reschedules_exactly_once(self):
        fake = self._fake()
        gi.GlobalIme._tick(fake)
        fake.root.after.assert_called_once_with(20, fake._tick)

    def test_client_tick_reschedules_exactly_once(self):
        try:
            from ime import client
        except ImportError as exc:      # CI 不装托盘依赖(Pillow/pystray)
            self.skipTest(f"client 依赖缺失: {exc}")
        fake = self._fake()
        fake.icon = None
        client.WinClient._tick(fake)
        fake.root.after.assert_called_once_with(20, fake._tick)


if __name__ == "__main__":
    unittest.main()
