"""语音输入子系统测试: 状态机流转/润色 prompt/失败兜底/历史落盘。

不装 funasr 也能跑: ASR 与润色全部 mock, 状态机纯逻辑。
"""

import json
import ctypes
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ime import global_ime as gi
from ime.global_ime import KeyboardHook
from ime.voice.config import DEFAULTS, VoiceConfig
from ime.voice.polish import build_prompt, build_translate_prompt
from ime.voice.voice_ime import VoiceIme, detect_profile


def make_voice(tmp, **overrides):
    data = json.loads(json.dumps(DEFAULTS))
    data["enabled"] = True
    data["llm"]["api_key"] = "test-key"
    data["record"]["samplerate"] = 16000
    cfg = VoiceConfig(path=Path(tmp) / "voice.json")
    cfg.data = {**data, **overrides}
    outputs, states = [], []

    def on_state(state, info):
        states.append((state, info))
    voice = VoiceIme(cfg, on_state=on_state,
                     on_output=lambda text, raw: outputs.append((text, raw)),
                     history_dir=Path(tmp) / "voice" / "history")
    return voice, outputs, states


class PromptTest(unittest.TestCase):
    def test_base_rules_cover_revision_and_filler(self):
        p = build_prompt("呃 那个 用 Postgres 不对 用 SQLite")
        self.assertIn("口头禅", p["content"])
        self.assertIn("改口", p["content"])
        self.assertIn("编号", p["content"])

    def test_profiles_differ(self):
        base = build_prompt("加个按钮")["content"]
        coding = build_prompt("加个按钮", "coding")["content"]
        email = build_prompt("加个按钮", "email")["content"]
        chat = build_prompt("加个按钮", "chat")["content"]
        self.assertIn("编号", coding)
        self.assertIn("落款", email)
        self.assertIn("口语", chat)
        self.assertEqual(base.count("\n8."), 0)

    def test_translate_prompt(self):
        p = build_translate_prompt("今天天气不错")
        self.assertIn("英文", p["content"])


class DetectProfileTest(unittest.TestCase):
    def test_known_and_unknown(self):
        self.assertEqual(detect_profile("claude.exe"), "coding")
        self.assertEqual(detect_profile("unknown.exe"), "default")
        self.assertEqual(detect_profile(None), "default")


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.voice, self.outputs, self.states = make_voice(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_recorder(self, wav_text="呃 那个 用 Postgres 不对 用 SQLite"):
        wav = Path(self.tmp.name) / "fake.wav"
        wav.write_bytes(b"RIFF")
        rec = mock.Mock()
        rec.start = mock.Mock()
        rec.stop = mock.Mock(return_value=wav)
        self.voice.recorder = rec
        return wav

    def test_ptt_flow_emits_polished_output(self):
        self._fake_recorder()
        self.voice.asr = mock.Mock()
        self.voice.asr.transcribe = mock.Mock(
            return_value="呃 那个 用 Postgres 不对 用 SQLite")
        self.voice.polisher = mock.Mock()
        self.voice.polisher.polish = mock.Mock(return_value="用 SQLite。")

        self.voice.ptt_down()
        self.assertEqual(self.voice.state, "recording")
        self.voice.ptt_up()
        deadline = time.time() + 5
        while self.voice.state != "idle" and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.outputs, [("用 SQLite。",
                                         "呃 那个 用 Postgres 不对 用 SQLite")])
        self.assertEqual(self.voice.state, "idle")

    def test_polish_failure_falls_back_to_raw(self):
        # R8: 润色失败/断网 → 出 FunASR 原文, 绝不空手
        self._fake_recorder()
        self.voice.asr = mock.Mock()
        self.voice.asr.transcribe = mock.Mock(return_value="原始转写内容")
        self.voice.polisher = mock.Mock()
        self.voice.polisher.polish = mock.Mock(
            side_effect=RuntimeError("network down"))
        self.voice.ptt_down()
        self.voice.ptt_up()
        deadline = time.time() + 5
        while self.voice.state != "idle" and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.outputs, [("原始转写内容", "原始转写内容")])

    def test_asr_failure_sets_error(self):
        self._fake_recorder()
        self.voice.asr = mock.Mock()
        self.voice.asr.transcribe = mock.Mock(
            side_effect=RuntimeError("model missing"))
        self.voice.ptt_down()
        self.voice.ptt_up()
        deadline = time.time() + 5
        while self.voice.state != "error" and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.outputs, [])
        self.assertTrue(any(s == "error" and "识别失败" in i.get("message", "")
                            for s, i in self.states))

    def test_false_touch_ignored(self):
        # <0.25s 误触: stop() 返回 None → 回 idle, 不起线程
        rec = mock.Mock()
        rec.start = mock.Mock()
        rec.stop = mock.Mock(return_value=None)
        self.voice.recorder = rec
        self.voice.ptt_down()
        self.voice.ptt_up()
        self.assertEqual(self.voice.state, "idle")
        self.assertEqual(self.outputs, [])

    def test_ptt_ignored_while_busy(self):
        self._fake_recorder()
        self.voice.asr = mock.Mock()
        self.voice.asr.transcribe = mock.Mock(
            side_effect=lambda *a, **k: (time.sleep(0.3), "内容")[1])
        self.voice.polisher = mock.Mock()
        self.voice.polisher.polish = mock.Mock(return_value="内容")
        self.voice.ptt_down()
        self.voice.ptt_up()
        self.voice.ptt_down()   # 工作中再按: 忽略
        self.voice.ptt_up()
        deadline = time.time() + 5
        while self.voice.state != "idle" and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.outputs), 1)

    def test_disabled_ignores_ptt(self):
        self.voice.enabled = False
        self.voice.ptt_down()
        self.assertEqual(self.voice.state, "idle")
        self.assertEqual(self.outputs, [])

    def test_history_appended(self):
        self._fake_recorder()
        self.voice.asr = mock.Mock()
        self.voice.asr.transcribe = mock.Mock(return_value="原稿")
        self.voice.polisher = mock.Mock()
        self.voice.polisher.polish = mock.Mock(return_value="成稿")
        self.voice.ptt_down()
        self.voice.ptt_up()
        deadline = time.time() + 5
        while self.voice.state != "idle" and time.time() < deadline:
            time.sleep(0.02)
        hdir = Path(self.tmp.name) / "voice" / "history"
        files = list(hdir.glob("*.jsonl")) if hdir.exists() else []
        self.assertTrue(files and "成稿" in files[0].read_text(encoding="utf-8"))


class VoiceConfigTest(unittest.TestCase):
    def test_defaults_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "voice.json"
            cfg = VoiceConfig(path=p)
            self.assertEqual(cfg.ptt_vk, [163])
            self.assertFalse(cfg.llm_ready())
            cfg.llm["api_key"] = "abc"
            cfg.save()
            again = VoiceConfig(path=p)
            self.assertTrue(again.llm_ready())
            self.assertTrue(again.enabled is not None)


class HookPttTest(unittest.TestCase):
    """LL 钩子层: PTT 键 down/up 事件、长按去重、语音开关与直通门控。"""

    def _handle(self, hook, vk, down):
        kb = gi.KBDLLHOOKSTRUCT()
        kb.vkCode = vk
        kb.scanCode = 0x1D
        kb.flags = 0
        with mock.patch.object(hook, "_hook", 0), \
                mock.patch.object(gi.user32, "CallNextHookEx", return_value=0), \
                mock.patch.object(gi.user32, "GetGUIThreadInfo", return_value=0):
            return hook._handle(0, gi.WM_KEYDOWN if down else gi.WM_KEYUP,
                                ctypes.addressof(kb))

    def _make(self, state):
        events = queue.Queue()
        hook = KeyboardHook(events, state)
        return hook, events

    def test_ptt_down_up_events(self):
        st = {"enabled": False, "buffer": "", "voice_enabled": True,
              "voice_ptt": [163, 162], "passthrough": set()}
        hook, events = self._make(st)
        self._handle(hook, 163, True)
        self.assertEqual(events.get_nowait(), ("voice_ptt_down", None, ""))
        self._handle(hook, 163, False)
        self.assertEqual(events.get_nowait(), ("voice_ptt_up", None, ""))

    def test_ptt_repeat_down_deduped(self):
        st = {"enabled": False, "buffer": "", "voice_enabled": True,
              "voice_ptt": [163, 162], "passthrough": set()}
        hook, events = self._make(st)
        self._handle(hook, 163, True)
        self._handle(hook, 163, True)  # 长按自动重复
        self._handle(hook, 163, False)
        kinds = [events.get_nowait()[0], events.get_nowait()[0]]
        self.assertEqual(kinds, ["voice_ptt_down", "voice_ptt_up"])
        with self.assertRaises(queue.Empty):
            events.get_nowait()

    def test_ptt_dead_when_voice_disabled(self):
        st = {"enabled": True, "buffer": "", "voice_enabled": False,
              "voice_ptt": [163, 162], "passthrough": set()}
        hook, events = self._make(st)
        self._handle(hook, 163, True)
        with self.assertRaises(queue.Empty):
            events.get_nowait()

    def test_ptt_dead_in_passthrough_app(self):
        st = {"enabled": False, "buffer": "", "voice_enabled": True,
              "voice_ptt": [163, 162], "passthrough": {"remote.exe"}}
        hook, events = self._make(st)
        with mock.patch.object(gi, "foreground_exe", return_value="remote.exe"):
            self._handle(hook, 163, True)
        with self.assertRaises(queue.Empty):
            events.get_nowait()

    def test_ptt_works_even_when_text_ime_off(self):
        # 语音独立开关: 文字输入关闭(enabled=False)时 PTT 仍可用
        st = {"enabled": False, "buffer": "", "voice_enabled": True,
              "voice_ptt": [163, 162], "passthrough": set()}
        hook, events = self._make(st)
        self._handle(hook, 163, True)
        self.assertEqual(events.get_nowait(), ("voice_ptt_down", None, ""))


if __name__ == "__main__":
    unittest.main()
