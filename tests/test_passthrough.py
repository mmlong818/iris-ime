"""前台应用直通测试。

远程桌面/云游戏客户端(UU远程等)把键盘捕获转发给远端, 本地注入的
Unicode 文本到不了远端——直通白名单让 IME 对这些应用整体放行。
"""

import queue
import ctypes
import unittest
from unittest import mock

from ime import global_ime as gi
from ime.global_ime import KeyboardHook, foreground_exe


def make_hook(state=None):
    state = state or {"enabled": True, "buffer": "", "passthrough": set()}
    return KeyboardHook(queue.Queue(), state)


_kb_structs = []  # 钩子按裸地址读内存, 结构体必须保活


def keydown_lparam(vk=0x41):
    """真实 KBDLLHOOKSTRUCT 地址(非注入的 a 键按下)。"""
    kb = gi.KBDLLHOOKSTRUCT()
    kb.vkCode = vk
    kb.scanCode = 0x1E
    kb.flags = 0
    _kb_structs.append(kb)
    return ctypes.addressof(kb)


class ForegroundExeTest(unittest.TestCase):
    def test_returns_name_or_none(self):
        exe = foreground_exe()
        self.assertTrue(exe is None or
                        (isinstance(exe, str) and exe == exe.lower()
                         and exe.endswith(".exe")))


class PassthroughMatchTest(unittest.TestCase):
    def test_empty_list_never_matches(self):
        hook = make_hook({"enabled": True, "buffer": "", "passthrough": set()})
        self.assertIsNone(hook._pt_exe())

    def test_match_is_exact_exe_name(self):
        hook = make_hook({"enabled": True, "buffer": "",
                          "passthrough": {"uuremote.exe"}})
        with mock.patch.object(gi, "foreground_exe", return_value="uuremote.exe"):
            self.assertEqual(hook._pt_exe(), "uuremote.exe")

    def test_no_partial_match(self):
        hook = make_hook({"enabled": True, "buffer": "",
                          "passthrough": {"uu.exe"}})
        with mock.patch.object(gi, "foreground_exe", return_value="uuremote.exe"):
            self.assertIsNone(hook._pt_exe())
        # 子串也不行(避免误伤同名前缀应用)
        with mock.patch.object(gi, "foreground_exe", return_value="uuhelp.exe"):
            self.assertIsNone(hook._pt_exe())

    def test_other_app_not_matched(self):
        hook = make_hook({"enabled": True, "buffer": "",
                          "passthrough": {"uuremote.exe"}})
        with mock.patch.object(gi, "foreground_exe", return_value="notepad.exe"):
            self.assertIsNone(hook._pt_exe())


class PassthroughClearTest(unittest.TestCase):
    def test_clear_event_fires_once(self):
        """切进直通应用时组词中的编码应被清空(且只清一次)。"""
        st = {"enabled": True, "buffer": "ni", "pt_cleared": False,
              "passthrough": {"uuremote.exe"}}
        events = queue.Queue()
        hook = KeyboardHook(events, st)
        with mock.patch.object(gi, "foreground_exe", return_value="uuremote.exe"), \
                mock.patch.object(hook, "_hook", 0), \
                mock.patch.object(gi.user32, "CallNextHookEx", return_value=0), \
                mock.patch.object(gi.user32, "GetGUIThreadInfo", return_value=0):
            lp = keydown_lparam()
            hook._handle(0, gi.WM_KEYDOWN, lp)  # a 键按下
            self.assertEqual(events.get_nowait(), ("passthrough", None, ""))
            self.assertTrue(st["pt_cleared"])
            hook._handle(0, gi.WM_KEYDOWN, lp)  # 后续键不再重复发事件
            with self.assertRaises(queue.Empty):
                events.get_nowait()
        # 切回非直通应用 → 标志复位, 再切回直通会重新清空
        st["buffer"] = "ni"
        with mock.patch.object(gi, "foreground_exe", return_value="notepad.exe"), \
                mock.patch.object(hook, "_hook", 0), \
                mock.patch.object(gi.user32, "CallNextHookEx", return_value=0), \
                mock.patch.object(gi.user32, "GetGUIThreadInfo", return_value=0):
            hook._handle(0, gi.WM_KEYDOWN, keydown_lparam())
            self.assertFalse(st["pt_cleared"])


if __name__ == "__main__":
    unittest.main()
