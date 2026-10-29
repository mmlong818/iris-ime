"""Shift 单击切换的状态机测试(直接驱动钩子 _handle, 两种模式全覆盖)。

回归背景: Shift 切换逻辑曾放在 enabled 门控之后——切到英模式后 Shift 事件
被提前放行, 永远切不回中文; 另有客户端二次翻转导致按键净效果为零。
"""

import ctypes
import unittest

from ime.global_ime import KBDLLHOOKSTRUCT, KeyboardHook

WM_KEYDOWN, WM_KEYUP = 0x100, 0x101
VK_SHIFT, VK_A, VK_P = 0x10, 0x41, 0x50
VK_CONTROL, VK_MENU = 0x11, 0x12


class ShiftToggleTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.held = set()                       # 模拟物理按住的键
        self.hook = KeyboardHook.__new__(KeyboardHook)  # 不启动真钩子线程
        self.hook.allow_injected = False
        self.hook.state = {"enabled": True, "buffer": "", "shift_armed": False,
                           "caps": False}
        self.hook.events = self
        self.hook._hook = None
        self.hook._down = lambda vk: vk in self.held

    # events 充当队列
    def put(self, ev):
        self.events.append(ev)

    def get_nowait(self):
        return self.events.pop(0)

    Empty = IndexError

    def key(self, vk, down):
        (self.held.add if down else self.held.discard)(vk)
        kb = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=0, time=0,
                             dwExtraInfo=None)
        self.hook._handle(0, WM_KEYDOWN if down else WM_KEYUP,
                          ctypes.addressof(kb))

    def shift_click(self, vk=VK_SHIFT):
        self.key(vk, True)
        self.key(vk, False)

    def test_toggle_to_english_and_back(self):
        self.assertTrue(self.hook.state["enabled"])
        self.shift_click()
        self.assertFalse(self.hook.state["enabled"])   # 中 -> 英
        self.shift_click()
        self.assertTrue(self.hook.state["enabled"])    # 英 -> 中(此前被门控挡死)

    def test_left_right_shift_codes_are_recognized(self):
        # 低级钩子报告左右区分键码 0xA0/0xA1, 而非 0x10 —— 真实键盘场景
        for vk in (0xA0, 0xA1):
            self.assertTrue(self.hook.state["enabled"])
            self.shift_click(vk)
            self.assertFalse(self.hook.state["enabled"])
            self.shift_click(vk)
            self.assertTrue(self.hook.state["enabled"])

    def test_toggle_from_disabled_mode(self):
        self.hook.state["enabled"] = False
        self.shift_click()
        self.assertTrue(self.hook.state["enabled"])    # 英模式下也能切回

    def test_shift_letter_does_not_toggle(self):
        self.key(VK_SHIFT, True)
        self.key(VK_A, True)          # Shift+A: 用掉了 Shift, 不算单击
        self.key(VK_A, False)
        self.key(VK_SHIFT, False)
        self.assertTrue(self.hook.state["enabled"])
        self.assertEqual(self.events, [])   # 字母因 Shift 按住而直通

    def test_shift_letter_in_english_mode_no_toggle(self):
        self.hook.state["enabled"] = False
        self.key(VK_SHIFT, True)
        self.key(VK_A, True)          # 英模式下打大写: 也不得误触发
        self.key(VK_A, False)
        self.key(VK_SHIFT, False)
        self.assertFalse(self.hook.state["enabled"])

    def test_letter_disarms_then_click_toggles(self):
        self.key(VK_A, True)          # 先打了字母
        self.key(VK_A, False)
        self.shift_click()            # 之后的干净单击仍然有效
        self.assertFalse(self.hook.state["enabled"])

    def test_ctrl_alt_p_toggles(self):
        self.held |= {VK_CONTROL, VK_MENU}
        self.key(VK_P, True)
        self.assertFalse(self.hook.state["enabled"])
        self.key(VK_P, True)
        self.assertTrue(self.hook.state["enabled"])


if __name__ == "__main__":
    unittest.main()
