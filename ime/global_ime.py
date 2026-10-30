"""全局输入模式: 低级键盘钩子 + 候选窗, 让引擎在任意应用里打字。

原理: WH_KEYBOARD_LL 钩子拦截按键 → 队列 → 主线程 Tk 候选窗处理并渲染;
空格/数字选词后用 SendInput 以 Unicode 注入中文文本。不需要任何编译器,
但注意这是"仿真输入法": 没有真正的 TSF 组合窗口, 目标应用看到的是直接
输入的文本。用前请把系统输入法切到英文。

快捷键: Ctrl+Alt+P 开关 · Shift 单击 中/英切换 · Esc 清编码 · 回车 原样上屏

用法:
  python -m ime.global_ime            # 常驻运行
  python -m ime.global_ime --selftest # 自动注入按键做端到端自检(不出中文到前台)

安全设计: 钩子线程只做入队, 解码在主线程; 注入事件(LLKHF_INJECTED)一律放行,
避免自反馈; 钩子异常时立即摘除。
"""

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import ctypes
from ctypes import wintypes

from .app import InputController

user32 = ctypes.WinDLL("user32", use_last_error=True)
ole32 = ctypes.OleDLL("ole32")
oleacc = ctypes.OleDLL("oleacc")

# COM 初始化(MSAA 需要各线程 STA; 已初始化时返回 S_FALSE, 无害)
ole32.CoInitialize(None)

# per-monitor DPI awareness: 混合缩放的多屏下, caret/MSAA 坐标与窗口坐标
# 才在同一物理像素空间; 必须在创建任何窗口前调用(已设置时失败, 无害)
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x100, 0x101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x104, 0x105
LLKHF_INJECTED = 0x10
VK_SHIFT, VK_CONTROL, VK_MENU, VK_SPACE = 0x10, 0x11, 0x12, 0x20
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1
# 低级钩子报告左右区分键码: 物理 Shift 的 vkCode 是 0xA0/0xA1 而非 0x10
SHIFT_VKS = (VK_SHIFT, VK_LSHIFT, VK_RSHIFT)
INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20
TOGGLE_VK = 0x50  # P


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndFocus", wintypes.HWND), ("hwndActive", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)


def send_text(text):
    """以 Unicode 事件向当前焦点窗口注入文本。"""
    text = text.replace("\n", "\r")
    events = (INPUT * (2 * len(text)))()
    for i, ch in enumerate(text):
        code = ord(ch)
        events[2 * i].type = INPUT_KEYBOARD
        events[2 * i].ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, None)
        events[2 * i + 1].type = INPUT_KEYBOARD
        events[2 * i + 1].ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
    user32.SendInput(len(events), events, ctypes.sizeof(INPUT))


def send_vk(vk):
    """注入一次虚拟按键(自检用)。"""
    events = (INPUT * 2)()
    events[0].type = INPUT_KEYBOARD
    events[0].ki = KEYBDINPUT(vk, 0, 0, 0, None)
    events[1].type = INPUT_KEYBOARD
    events[1].ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None)
    user32.SendInput(2, events, ctypes.sizeof(INPUT))


def caret_screen_pos():
    """定位输入光标, 返回 (x, y_bottom, y_top, src) 屏幕坐标; 失败返回 None。

    四级链: 经典 caret(GetGUIThreadInfo, 记事本/EDIT 控件) →
    MSAA 无障碍光标(AccessibleObjectFromWindow OBJID_CARET,
    Chrome/Edge/Office 等现代应用) → UIA 文字选区 → 焦点元素矩形。
    src 标记来源层(classic/msaa/uia_sel/field)——同一光标不同层给出的
    矩形可能差半行到一整框, 调用方做层粘性防"候选框乱跑"。
    """
    info = GUITHREADINFO()
    info.cb = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        return None

    if info.hwndCaret:
        rc = info.rcCaret
        pt = wintypes.POINT(rc.left, rc.bottom)
        if user32.MapWindowPoints(info.hwndCaret, None, ctypes.byref(pt), 1):
            top = wintypes.POINT(rc.left, rc.top)
            user32.MapWindowPoints(info.hwndCaret, None, ctypes.byref(top), 1)
            return pt.x, pt.y, top.y, "classic"

    if info.hwndFocus:
        pos = _msaa_caret_pos(info.hwndFocus)
        if pos:
            return pos[0], pos[1], pos[2], "msaa"

    # 终极通道 1: UIA 文字选区(Chromium/WPF/WinUI 等现代应用)
    try:
        from .uia_caret import uia_caret_rect
        pos = uia_caret_rect()
        if pos:
            return pos[0], pos[1], pos[2], "uia_sel"
    except Exception:
        pass
    # 终极通道 2: 焦点元素矩形(微信等封闭自绘 UI 给不出光标,
    # 但焦点元素=输入框本身, 候选条直接放进输入框内底部)
    try:
        from .uia_caret import uia_focused_rect
        pos = uia_focused_rect()
        if pos:
            return pos[0], pos[1], pos[2], "field"
    except Exception:
        pass
    return None


def _poke_accessibility(hwnd):
    """向窗口要一次 OBJID_CLIENT 的 IAccessible——WM_GETOBJECT 会让
    Chromium 系应用激活无障碍支持, 之后 OBJID_CARET 才有真身。"""
    try:
        GA_ROOT = 2
        root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        for target in (hwnd, root):   # 根窗口的 WM_GETOBJECT 才能激活 Chromium 无障碍
            pacc = ctypes.c_void_p()
            oleacc.AccessibleObjectFromWindow(
                target, 0xFFFFFFFC,  # OBJID_CLIENT
                ctypes.byref(IID_IAccessible), ctypes.byref(pacc))
            if not pacc.value:
                continue
            vtbl = ctypes.cast(ctypes.cast(pacc, ctypes.POINTER(ctypes.c_void_p)).contents,
                               ctypes.POINTER(ctypes.c_void_p))
            release = ctypes.cast(vtbl[2], ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p))
            release(pacc)
    except Exception:
        pass


def _msaa_caret_pos(hwnd_focus):
    """通过 MSAA 取现代应用的光标矩形(屏幕坐标)。

    Chromium/Electron 系(Chrome、VS Code、ZCode 等)的无障碍树默认休眠,
    需要先收到 WM_GETOBJECT(OBJID_CLIENT) 才激活——AccessibleObjectFromWindow
    恰好就是这条路径, 所以先"敲门"再取光标, 120ms 轮询下通常一两键后生效。
    """
    try:
        _poke_accessibility(hwnd_focus)
        pacc = ctypes.c_void_p()
        hr = oleacc.AccessibleObjectFromWindow(
            hwnd_focus, 0xFFFFFFF8,  # OBJID_CARET
            ctypes.byref(IID_IAccessible), ctypes.byref(pacc))
        if hr != 0 or not pacc.value:
            return None
        vtbl = ctypes.cast(ctypes.cast(pacc, ctypes.POINTER(ctypes.c_void_p)).contents,
                           ctypes.POINTER(ctypes.c_void_p))
        # IAccessible::accLocation 是 vtable 第 25 槽(3 IUnknown + 4 IDispatch + 18)
        proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_long),
                                   ctypes.POINTER(ctypes.c_long),
                                   ctypes.POINTER(ctypes.c_long),
                                   ctypes.POINTER(ctypes.c_long),
                                   ctypes.c_void_p)
        func = ctypes.cast(vtbl[25], proto)
        x, y, w, h = (ctypes.c_long() for _ in range(4))
        child = VARIANT()
        child.vt = 3  # VT_I4
        child.lVal = 0  # CHILDID_SELF
        hr = func(pacc, ctypes.byref(x), ctypes.byref(y),
                  ctypes.byref(w), ctypes.byref(h), ctypes.byref(child))
        # IUnknown::Release
        pacc_release = ctypes.cast(vtbl[2], ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p))
        pacc_release(pacc)
        if hr != 0 or h.value <= 0:
            return None
        # accLocation 返回的即是屏幕坐标
        return x.value, y.value + h.value, y.value
    except Exception:
        return None


# {618736E0-3C3D-11CF-810C-00AA00389B71} = IID_IAccessible
IID_IAccessible = (ctypes.c_ubyte * 16)(
    0xE0, 0x36, 0x87, 0x61, 0x3D, 0x3C, 0xCF, 0x11,
    0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71)


class VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort),
                ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort),
                ("wReserved3", ctypes.c_ushort),
                ("lVal", wintypes.LONG),
                ("extra", ctypes.c_ubyte * 8)]  # 凑满 x64 的 24 字节


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def monitor_work_area(px, py):
    """包含点 (px, py) 的显示器的工作区(不含任务栏); 多屏坐标可为负。"""
    pt = wintypes.POINT(int(px), int(py))
    hmon = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
    if hmon:
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            rc = info.rcWork
            return rc.left, rc.top, rc.right, rc.bottom
    # 兜底: 整个虚拟桌面
    SM_X, SM_Y, SM_CX, SM_CY = 76, 77, 78, 79
    x = user32.GetSystemMetrics(SM_X)
    y = user32.GetSystemMetrics(SM_Y)
    return x, y, x + user32.GetSystemMetrics(SM_CX), y + user32.GetSystemMetrics(SM_CY)


def fit_rect(x, y, w, h, left, top, right, bottom):
    """把 x,y 的 w×h 窗口收紧到矩形内(纯函数, 供测试)。返回 (x, y)。"""
    x = max(left, min(x, right - w))
    y = max(top, min(y, bottom - h))
    return x, y


class KeyboardHook:
    """后台线程: 安装 LL 钩子并跑消息泵。"""

    def __init__(self, events: queue.Queue, state: dict, allow_injected=False,
                 error_log_path=None):
        self.events = events
        self.state = state
        self.allow_injected = allow_injected
        self.error_log_path = error_log_path
        self.last_event_ts = time.time()   # 钩子存活看门狗的心跳
        self._hook = None
        self._proc = HOOKPROC(self._callback)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()

    def start(self):
        self._thread.start()
        self._ready.wait(3)

    def stop(self):
        if self._hook and self._thread.ident:
            user32.PostThreadMessageW(self._thread.ident, 0x0012, 0, 0)  # WM_QUIT

    def restart(self):
        """Windows 会静默摘除无响应的 LL 钩子(低级钩子超时机制), 用看门狗重装。"""
        self.stop()
        time.sleep(0.2)
        self._ready = threading.Event()
        self._proc = HOOKPROC(self._callback)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.last_event_ts = time.time()
        self.start()

    def _log_error(self, text):
        if not self.error_log_path:
            return
        try:
            with open(self.error_log_path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
        except OSError:
            pass

    def _run(self):
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            raise OSError("SetWindowsHookEx 失败", ctypes.get_last_error())
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            pass
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def _down(self, vk):
        return user32.GetAsyncKeyState(vk) & 0x8000

    def _callback(self, ncode, wparam, lparam):
        try:
            return self._handle(ncode, wparam, lparam)
        except Exception as exc:
            # 钩子里绝不能抛: 记录并保守放行(不摘钩, 看门狗负责续命)
            import traceback
            self._log_error(f"钩子回调异常: {exc!r} {traceback.format_exc(limit=3)}")
            return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)

    def _handle(self, ncode, wparam, lparam):
        self.last_event_ts = time.time()   # 看门狗心跳, 探针事件也算
        if ncode != 0:
            return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)
        kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        injected = bool(kb.flags & LLKHF_INJECTED)
        if self.allow_injected:
            if not injected:
                # 自检模式: 只认注入事件, 与真实键盘完全隔离(否则用户
                # 正在打的字会混进测试流)
                return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)
        elif injected:
            return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)

        vk = kb.vkCode
        is_down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        is_up = wparam in (WM_KEYUP, WM_SYSKEYUP)

        # Ctrl+Alt+P 总是可用作开关(两种模式下都有效)
        if is_down and vk == TOGGLE_VK and self._down(VK_CONTROL) and self._down(VK_MENU):
            self.state["enabled"] = not self.state["enabled"]
            self.state["shift_armed"] = False
            self.events.put(("toggle", None, ""))
            return 1

        # Shift 单击 = 中/英切换。必须在 enabled 门控之前处理——否则切到
        # 英模式后 Shift 事件被直接放行, 永远切不回中文(实测踩过)。
        # Shift+字母/其他键会重置 armed, 不构成单击。
        if vk in SHIFT_VKS:
            if is_down:
                self.state["shift_armed"] = True
            elif is_up:
                if self.state.get("shift_armed"):
                    self.state["enabled"] = not self.state["enabled"]
                    self.events.put(("toggle", None, ""))
                self.state["shift_armed"] = False
            return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)

        if is_down and vk not in SHIFT_VKS:
            self.state["shift_armed"] = False  # Shift 后跟了别的键, 不再算单击

        if not self.state["enabled"]:
            return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)

        # Caps Lock 开启 = 字母直通英文(与主流输入法一致), 供状态条显示
        self.state["caps"] = bool(user32.GetKeyState(0x14) & 0x0001)

        if not is_down:
            return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)

        composing = bool(self.state.get("buffer"))
        ctrl_or_alt = self._down(VK_CONTROL) or self._down(VK_MENU)
        shift = self._down(VK_SHIFT)
        caps_on = self.state.get("caps", False)

        if not ctrl_or_alt and not shift and not caps_on:
            if 0x41 <= vk <= 0x5A:            # a-z
                ch = chr(vk + 32)
                self.events.put(("key", ch, ch))
                return 1
            if vk == VK_SPACE:
                if composing:
                    self.events.put(("key", "space", " "))
                    return 1
                # 空编码时原生放行——注入的空格与直通无差别, 放行更稳
                return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)
            if composing and (0x31 <= vk <= 0x39):   # 1-9 选词
                ch = chr(vk)
                self.events.put(("key", ch, ch))
                return 1
            if composing and vk in (0x21, 0x22, 0xBD, 0xBB):
                # PgUp/PgDn 与 -/= 翻页(仅组词中, 不影响其他应用)
                keysym = {0x21: "Prior", 0x22: "Next",
                          0xBD: "Prior", 0xBB: "Next"}[vk]
                self.events.put(("key", keysym, ""))
                return 1
            if composing and vk == 0x08:      # BackSpace
                self.events.put(("key", "BackSpace", ""))
                return 1
            if composing and vk == 0x0D:      # Return
                self.events.put(("key", "Return", ""))
                return 1
            if composing and vk == 0x1B:      # Escape
                self.events.put(("key", "Escape", ""))
                return 1
        elif is_down and vk == 0x08 and self._down(VK_CONTROL) and composing:
            # Ctrl+BackSpace = 单字模式开关(仅组词中; 空编码原生放行,
            # 不影响其他应用的"删词")。双击退格方案废弃——快速连删
            # 每秒 5-10 下全落进双击窗口, 实测退格删不掉字
            self.events.put(("key", "Control-BackSpace", ""))
            return 1
        return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)


class GlobalIme:
    def __init__(self, selftest=False, model_path=None, user_path=None,
                 auto_space=False, space_digits=False, ui_settings_path=None,
                 fuzzy=True, error_log_path=None):
        self.controller = InputController(model_path=model_path, user_path=user_path,
                                          auto_space=auto_space, space_digits=space_digits,
                                          fuzzy=fuzzy)
        self.state = {"enabled": False, "buffer": "", "shift_armed": False}
        self.events = queue.Queue()
        self.hook = KeyboardHook(self.events, self.state, allow_injected=selftest,
                                 error_log_path=error_log_path)
        self.root = tk.Tk()
        self.root.title("pinyin-ime")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        hwnd = int(self.root.frame(), 16) if isinstance(self.root.frame(), str) else self.root.frame()
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                              user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE)
        # ---- 视觉: "鸢尾"主题 —— 深色玻璃 + 鸢尾蓝紫 + 药丸候选 ----
        # 与主流输入法(白底蓝字)刻意区分: 整窗半透明模拟玻璃, 候选为药丸形
        self.C_KEY = "#010203"        # 抠透明色(四角)
        self.C_BG = "#20242c"         # 深色底(玻璃)
        self.C_BORDER = "#39414d"
        self.C_PILL = "#2b323d"       # 普通候选药丸
        self.C_PILL_HOVER = "#3a4450"
        self.C_TEXT = "#e8eaee"
        self.C_GRAY = "#8a94a3"
        self.C_PINYIN = "#a29bd0"     # 拼音: 带紫意的灰
        self.C_ACCENT = "#8b7cf0"     # 鸢尾蓝紫
        self.C_ACCENT_TEXT = "#191236"
        self.root.configure(bg=self.C_KEY)
        self.root.attributes("-transparentcolor", self.C_KEY)
        try:
            self.root.attributes("-alpha", 0.94)  # 整窗微透明 = 玻璃感
        except Exception:
            pass
        self.canvas = tk.Canvas(self.root, bg=self.C_KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.rows = tk.Frame(self.canvas, bg=self.C_BG)
        self._win_w, self._win_h = 380, 60
        self._seen_output = 0
        self._toast_until = 0.0   # 中英切换大字提示的截止时刻
        self._last_probe = 0.0    # 钩子看门狗探针时刻
        self._last_buf_len = 0
        self._comp_mouse_anchor = None
        self._hook_restarts = 0
        self._comp_mouse_anchor = None  # 合成期鼠标兜底冻结位

        # 闲置停靠位: 用户可拖动状态条, 位置持久化; 组词时仍跟随光标
        self._ui_settings_path = ui_settings_path
        self._idle_pos = None
        self._drag = None
        self._dragging = False
        if ui_settings_path and Path(ui_settings_path).exists():
            try:
                pos = json.loads(Path(ui_settings_path).read_text(encoding="utf-8")).get("idle_pos")
                if isinstance(pos, list) and len(pos) == 2:
                    self._idle_pos = (int(pos[0]), int(pos[1]))
            except (json.JSONDecodeError, OSError, ValueError):
                pass
        self.canvas.configure(cursor="fleur")
        self.root.bind("<Button-1>", self._drag_start)
        self.root.bind("<B1-Motion>", self._drag_motion)
        self.root.bind("<ButtonRelease-1>", self._drag_end)
        self._caret_ts = 0.0      # 光标查询节流
        self._last_anchor = None  # 最近一次光标位置 (x, y_bottom, y_top, src)
        self._anchor_src = None   # 本组合采信的定位层(层粘性, 防层间拍动)
        self._src_miss = 0        # 惯用层之外的连续样本数
        self._last_geo = ""
        self._caret_log = None    # 诊断日志路径(懒建)
        if error_log_path:
            self._caret_log = Path(error_log_path).parent / "caret_debug.log"
        self.controller.on_change = lambda s: None  # 渲染由 _tick 统一驱动
        self.root.after(20, self._tick)

    # ---- 钩子看门狗 ----

    def _watchdog(self):
        """Windows 会静默摘除无响应的 LL 钩子(用户报过'突然失效')。
        每 5 秒注入一个保留键码探针; 钩子活着就会更新心跳, 死了就重装。"""
        now = time.time()
        if now - self._last_probe < 5.0:
            return
        if now - self.hook.last_event_ts > 6.0:
            self._hook_restarts += 1
            if self._hook_restarts <= 100:
                self._log_error_safe(f"看门狗重装钩子(第 {self._hook_restarts} 次)")
                self.hook.restart()
        self._last_probe = now
        send_vk(0xFF)  # 保留键码探针, 应用会忽略

    def _log_error_safe(self, text):
        try:
            self.hook._log_error(text)
        except Exception:
            pass

    # ---- 主线程轮询 ----

    def _flush_output(self):
        """把控制器新增的上屏文本注入目标应用(按键与鼠标点击共用)。"""
        delta = self.controller.output
        if delta:
            self.controller.output = ""
            send_text(delta)
        self.state["buffer"] = self.controller.buffer

    def _tick(self):
        self._watchdog()
        try:
            while True:
                kind, keysym, ch = self.events.get_nowait()
                if kind == "toggle":
                    self._toast_until = time.time() + 0.9  # 切换大字提示
                else:
                    self.controller.feed_key(keysym, ch)
                    self._flush_output()
        except queue.Empty:
            pass
        self.controller.settle_rerank()
        self.controller.poll_rerank()  # 后台重排算完且缓冲未变 → 换头
        self._render()
        self.root.after(20, self._tick)

    def _click_select(self, abs_idx):
        """鼠标点选候选(窗口 NOACTIVATE, 焦点仍在目标应用)。"""
        self.controller._select(abs_idx)
        self._flush_output()
        self._render()

    def _drag_start(self, event):
        if self.controller.buffer or self._drag is not None:
            return  # 组词中跟随光标, 不拖
        if event.widget is self.canvas:
            for item in self.canvas.find_withtag("current"):
                if "cand" in self.canvas.gettags(item):
                    return  # 药丸点击交给选词
        self._drag = (event.x_root - self.root.winfo_x(),
                      event.y_root - self.root.winfo_y())

    def _drag_motion(self, event):
        if self._drag is None:
            return
        self._dragging = True
        nx = event.x_root - self._drag[0]
        ny = event.y_root - self._drag[1]
        self.root.geometry(f"+{nx}+{ny}")
        self._idle_pos = (nx, ny)

    def _drag_end(self, event):
        if self._drag is None:
            return
        self._drag = None
        self._dragging = False
        if self._ui_settings_path and self._idle_pos:
            try:
                Path(self._ui_settings_path).write_text(
                    json.dumps({"idle_pos": list(self._idle_pos)}), encoding="utf-8")
            except OSError:
                pass

    def _render(self):
        import tkinter.font as tkfont
        enabled = self.state["enabled"]
        buf = self.controller.buffer
        page_cands = self.controller.page_candidates

        # 切换提示: 大字 中/英 独占卡片 0.9 秒(误触 Shift 一眼可见)
        if time.time() < self._toast_until:
            self._win_w, self._win_h = 96, 96
            self.canvas.delete("all")
            self._round_rect(1, 1, self._win_w - 2, self._win_h - 2, 18,
                             fill=self.C_BG, outline=self.C_ACCENT if enabled
                             else self.C_BORDER)
            self.canvas.create_text(
                self._win_w / 2, self._win_h / 2 - 12,
                text="中" if enabled else "英", anchor="center",
                font=("Microsoft YaHei UI", 30, "bold"),
                fill=self.C_ACCENT if enabled else self.C_GRAY)
            self._place()
            return

        f_status = tkfont.Font(family="Microsoft YaHei UI", size=9)
        f_pre = tkfont.Font(family="Microsoft YaHei UI", size=12)
        f_cand = tkfont.Font(family="Microsoft YaHei UI", size=15)
        f_badge = tkfont.Font(family="Microsoft YaHei UI", size=9)

        for child in self.rows.winfo_children():
            child.destroy()

        # 状态行: 绿点(中)/灰点(英) + 模式 + 页码
        dot = self.C_ACCENT if enabled else self.C_GRAY
        mode = "中" if enabled else "英"
        if self.state.get("caps"):
            mode += " · Caps 直通"
        if self.controller.char_mode:
            mode += " · 单字"
        page = (f"  {self.controller.page + 1}/{self.controller.page_count}"
                if buf and self.controller.page_count > 1 else "")
        status = tk.Label(self.rows, font=f_status, fg=self.C_GRAY, bg=self.C_BG)
        status_text = f"{mode} · 空格上屏 · PgUp/Dn 翻页{page}" if buf \
            else f"{mode} · Ctrl+Alt+P 开启"
        status.configure(text=status_text, anchor="w")
        status.pack(fill="x", pady=(0, 2))

        # 预编辑行: 拼音(紫灰, 音节 ' 分隔) + 最优预览(亮白)
        first = page_cands[0][1].preview if page_cands else ""
        if buf:
            pre_row = tk.Frame(self.rows, bg=self.C_BG)
            pre_row.pack(fill="x")
            py_disp = page_cands[0][1].pinyin if page_cands and page_cands[0][1].pinyin else buf
            tk.Label(pre_row, text=py_disp, font=f_pre, fg=self.C_PINYIN,
                     bg=self.C_BG).pack(side="left")
            if first:
                tk.Label(pre_row, text="  " + first, font=f_pre,
                         fg=self.C_TEXT, bg=self.C_BG).pack(side="left")

        # 尺寸: 先量后画——底板宽度必须用本轮内容算出
        pre_w = f_pre.measure((page_cands[0][1].pinyin if page_cands and page_cands[0][1].pinyin else buf) + "  " + first) + 8 if buf else 0
        status_w = f_status.measure(status_text) + 8
        pill_boxes = []
        for abs_idx, cand in page_cands:
            pw = f_cand.measure(f"{abs_idx % self.controller.page_size + 1}  {cand.text}") + 26
            pill_boxes.append((abs_idx, cand, pw))
        # 宽度上限 = 虚拟桌面宽-余量(多屏取总宽); 超宽则药丸换行, 不裁词
        virtual_w = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        max_w = max(320, min(2400, virtual_w - 60))
        pill_rows, cur, cur_w = [], [], 14
        for box in pill_boxes:
            pw = box[2]
            if cur and cur_w + pw + 8 > max_w - 14:
                pill_rows.append(cur)
                cur, cur_w = [], 14
            cur.append(box)
            cur_w += pw + 8
        if cur:
            pill_rows.append(cur)
        pills_w = max((sum(pw for _, _, pw in row) + 8 * len(row) + 14
                       for row in pill_rows), default=0)
        self._win_w = max(320, min(max_w, max(pills_w, status_w + 40, pre_w + 40)))
        self._win_h = (52 + len(pill_rows) * 42 + 4) if buf else 52

        # 圆角玻璃底板 + 品牌绿点 + 内容
        self.canvas.delete("all")
        self._round_rect(1, 1, self._win_w - 2, self._win_h - 2, 16,
                         fill=self.C_BG, outline=self.C_BORDER)
        dot = self.C_ACCENT if enabled else self.C_GRAY
        self.canvas.create_oval(14, 12, 20, 18, fill=dot, outline="")
        self.canvas.create_window(28, 7, window=self.rows, anchor="nw",
                                  width=max(status_w, pre_w, 260),
                                  height=(44 if buf else 26))

        # 药丸候选(自动换行, 画在 Canvas, 悬停/点击走 canvas tag)
        pill_h = 34
        pill_y = 52
        for row in pill_rows:
            x = 14
            for abs_idx, cand, pw in row:
                hot = abs_idx == 0
                num = abs_idx % self.controller.page_size + 1
                text = f"{num}  {cand.text}"
                tag = f"cand{abs_idx}"
                fill = self.C_ACCENT if hot else self.C_PILL
                self._round_rect(x, pill_y, x + pw, pill_y + pill_h, pill_h // 2,
                                 fill=fill, outline="", tags=("pillbg", tag))
                self.canvas.create_text(
                    x + 13, pill_y + pill_h / 2 - 11, text=text, anchor="nw",
                    font=f_cand, fill=self.C_ACCENT_TEXT if hot else self.C_TEXT,
                    tags=("pill", tag))
                self.canvas.tag_bind(tag, "<Enter>", lambda e, t=tag, p=(x, pill_y, x + pw, pill_y + pill_h), h=hot:
                                     (self._pill_hover(t, p, h, True),
                                      self.canvas.configure(cursor="hand2")))
                self.canvas.tag_bind(tag, "<Leave>", lambda e, t=tag, p=(x, pill_y, x + pw, pill_y + pill_h), h=hot:
                                     (self._pill_hover(t, p, h, False),
                                      self.canvas.configure(cursor="fleur")))
                self.canvas.tag_bind(tag, "<Button-1>",
                                     lambda e, i=abs_idx: self._click_select(i))
                x += pw + 8
            pill_y += 42
        self._place()

    def _pill_hover(self, tag, box, hot, enter):
        fill = self.C_ACCENT if hot else (self.C_PILL_HOVER if enter else self.C_PILL)
        x1, y1, x2, y2 = box
        self._round_rect(x1, y1, x2, y2, (y2 - y1) // 2, fill=fill,
                         outline="", tags=("pillbg", tag))
        self.canvas.tag_lower("pillbg")  # 药丸底永远压在文字下

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    @staticmethod
    def _sane_anchor(pos):
        """光标矩形合法性: 高度像光标(2~200px), 位置在虚拟桌面附近。
        pos 可为 (x, yb, yt) 或带来源标记的 (x, yb, yt, from_field)。"""
        x, yb, yt = pos[0], pos[1], pos[2]
        if not (2 <= yb - yt <= 200):
            return False
        l = user32.GetSystemMetrics(76)
        t = user32.GetSystemMetrics(77)
        r = l + user32.GetSystemMetrics(78)
        b = t + user32.GetSystemMetrics(79)
        return l - 100 <= x <= r + 100 and t - 100 <= yb <= b + 100

    def _caret_debug(self, event, src, pos, extra=""):
        """定位决策诊断(体积小: 只记换层/拒绝/换源事件, 恒开)。
        用户报"乱跑"时此日志可定位到具体应用与层。"""
        if not self._caret_log:
            return
        try:
            if not self._caret_log.exists() or \
                    self._caret_log.stat().st_size > 1_000_000:
                self._caret_log.write_text("", encoding="utf-8")  # 截断轮转
            with open(self._caret_log, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {event} src={src} "
                        f"pos={pos} {extra}\n")
        except OSError:
            pass

    def _place(self):
        w, h = self._win_w, self._win_h
        if self._dragging:
            return  # 拖拽中, 不让心跳 place 抢位置
        if self.controller.buffer:
            # 组词中才查询光标; 节流, 避免每次心跳都唤起目标应用的无障碍接口
            now = time.time()
            buf_len = len(self.controller.buffer)
            first_sample = self._last_buf_len == 0   # 新组合的第一针
            if now - self._caret_ts > 0.12:
                self._caret_ts = now
                pos = caret_screen_pos()
                if pos and self._sane_anchor(pos):
                    src = pos[3]
                    if first_sample or self._anchor_src is None:
                        # 新组合首针无条件采信(旧锚点属于上一个组合)
                        self._last_anchor = pos
                        if self._anchor_src != src:
                            self._caret_debug("adopt", src, pos,
                                              f"first={first_sample}")
                        self._anchor_src = src
                        self._src_miss = 0
                    elif src == self._anchor_src:
                        # 惯用层: 未打新字时位置变化=用户点击移光标, 采信;
                        # 打字中大跳丢弃(MSAA 对部分应用返回整窗矩形等脏数据)
                        prev = self._last_anchor
                        jumped = (prev and abs(pos[0] - prev[0]) > 600
                                  and abs(pos[1] - prev[1]) > 300)
                        if buf_len == self._last_buf_len or not jumped:
                            self._last_anchor = pos
                        elif jumped:
                            self._caret_debug("drop-jump", src, pos)
                    else:
                        # 异层样本: 默认拒绝(层间拍动=乱跑的典型来源);
                        # 靠近现锚点(60px)视为同位, 放宽采信; 惯用层连续
                        # 3 针失手才允许换层
                        near = (self._last_anchor
                                and abs(pos[0] - self._last_anchor[0]) <= 60
                                and abs(pos[1] - self._last_anchor[1]) <= 60)
                        self._src_miss += 1
                        if near or self._src_miss >= 3:
                            self._caret_debug(
                                "switch" if not near else "adopt-near",
                                src, pos, f"from={self._anchor_src}")
                            self._last_anchor = pos
                            self._anchor_src = src
                            self._src_miss = 0
                        else:
                            self._caret_debug("drop-layer", src, pos,
                                              f"keep={self._anchor_src}")
                elif pos:
                    self._caret_debug("drop-sane", pos[3], pos)
            self._last_buf_len = buf_len
        anchor = self._last_anchor if self.controller.buffer else None
        if anchor:
            from_field = anchor[3] == "field"
            x, y_bottom, y_top = anchor[0], anchor[1], anchor[2]
            area = monitor_work_area(x, y_bottom)
            if from_field:
                # 输入框层: 候选条放进输入框内底部(真输入法摆放), 放不下再翻上方
                y = y_bottom - h - 4
                if y < y_top:                     # 输入框比候选条矮, 贴框上沿
                    y = max(y_top - 2, 0)
            else:
                y = y_bottom + 2                  # 光标层: 默认在光标下方
                if y + h > area[3]:               # 贴该屏底部则翻到光标上方
                    y = y_top - h - 2
        elif self.controller.buffer:
            # 拿不到光标: 沿用最近一次文字光标位置(粘性)——用户明确要求
            # 跟随的是文字光标而非鼠标, 鼠标兜底已移除
            if self._last_anchor:
                x, y_bottom, y_top = self._last_anchor[:3]
                area = monitor_work_area(x, y_bottom)
                y = y_bottom + 2
                if y + h > area[3]:
                    y = max(0, y_top - h - 2)
                x, y = fit_rect(x, y, w, h, *area)
                geo = f"{w}x{h}+{x}+{y}"
                if geo != self._last_geo:
                    self._last_geo = geo
                    self.root.geometry(geo)
                return
            x = max(40, self.root.winfo_screenwidth() - w - 60)
            y = self.root.winfo_screenheight() - h - 80
            area = monitor_work_area(x, y)
        else:
            self._comp_mouse_anchor = None
            self._last_buf_len = 0    # 组合结束, 重置采样基线
            self._anchor_src = None
            self._src_miss = 0
            if self._idle_pos:
                x, y = self._idle_pos            # 用户拖过的停靠位优先
                area = monitor_work_area(x, y)
            else:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                left, top, right, bottom = monitor_work_area(pt.x, pt.y)
                x, y = right - w - 60, bottom - h - 80
                area = (left, top, right, bottom)
        x, y = fit_rect(x, y, w, h, *area)
        geo = f"{w}x{h}+{x}+{y}"
        if geo != self._last_geo:                 # 位置没变不重设, 防闪烁
            self._last_geo = geo
            self.root.geometry(geo)

    def run(self):
        self.hook.start()
        self.root.mainloop()


def selftest():
    """端到端自检: 注入 nihao+space, 期望引擎上屏 你好。"""
    # 运行中的托盘实例也装着 LL 钩子, 会截走测试按键——先结束它们
    import subprocess
    for exe in ("IrisIME.exe", "PinyinIME.exe"):
        subprocess.run(["taskkill", "/IM", exe, "/F"],
                       capture_output=True)
    time.sleep(0.3)
    app = GlobalIme(selftest=True)
    app.hook.start()
    app.state["enabled"] = True
    # Caps Lock 会令字母直通(设计行为), 测试期间临时关闭并恢复原状
    caps_was_on = bool(user32.GetKeyState(0x14) & 0x0001)
    if caps_was_on:
        send_vk(0x14)  # VK_CAPITAL
        time.sleep(0.15)

    def injector():
        time.sleep(0.8)
        for ch in "nihao":
            send_vk(ord(ch.upper()))
            time.sleep(0.12)
        time.sleep(0.3)
        send_vk(VK_SPACE)
        time.sleep(0.5)

    threading.Thread(target=injector, daemon=True).start()

    ok = False
    deadline = time.time() + 8
    while time.time() < deadline:
        app.root.update()   # 驱动 after 回调与渲染
        if "你好" in app.controller.committed:
            ok = True
            break
        time.sleep(0.05)
    print("自检通过: 全局钩子 -> 解码 -> 上屏 你好" if ok
          else f"自检失败: committed={app.controller.committed!r} buffer={app.controller.buffer!r}")
    app.hook.stop()
    if caps_was_on:
        send_vk(0x14)  # 恢复 Caps Lock 原状态
        time.sleep(0.1)
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    GlobalIme().run()


if __name__ == "__main__":
    main()
