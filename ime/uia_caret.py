"""UI Automation 文字选区光标探测。

对 Chromium 系(Chrome/VS Code/Electron)、WPF、WinUI 等现代应用,
MSAA 的 OBJID_CARET 越来越不可靠; UIA 的 TextPattern.GetSelection()
退化矩形是 Windows 官方的光标位置通道(输入法场景的本命接口)。
"""

import threading
import time

_uia = None
_uia_lock = threading.Lock()
_backoff_until = 0.0   # UIA 慢时退避 10 秒(主线程阻塞会造成打字卡顿)


def _get_uia():
    global _uia
    if _uia is None:
        with _uia_lock:
            if _uia is None:
                import comtypes.client
                UIA = comtypes.client.GetModule("UIAutomationCore.dll")
                obj = comtypes.client.CreateObject(
                    "{ff48dba4-60ef-4201-aa87-54103eef594e}",
                    interface=UIA.IUIAutomation)
                _uia = (obj, UIA)
    return _uia


def uia_caret_rect():
    """取焦点元素文字选区的退化矩形, 返回 (x, y_bottom, y_top) 或 None。

    调用线程须已 CoInitialize。首次调用会构建 UIA 类型库(约数百毫秒),
    之后走缓存。"""
    global _backoff_until
    if time.monotonic() < _backoff_until:
        return None
    try:
        t0 = time.monotonic()
        obj, UIA = _get_uia()
        el = obj.GetFocusedElement()
        if el is None:
            return None
        patt = el.GetCurrentPattern(10014)  # UIA_TextPatternId
        if not patt:
            return None
        tp = patt.QueryInterface(UIA.IUIAutomationTextPattern)
        sel = tp.GetSelection()
        if sel.Length < 1:
            return None
        rects = sel.GetElement(0).GetBoundingRectangles()
        if not rects or len(rects) < 4:
            return None
        x, y, w, h = rects[0], rects[1], rects[2], rects[3]
        if h <= 0 or h > 200 or w > 400:
            return None  # 整窗/整行矩形不是光标
        cost = time.monotonic() - t0
        if cost > 0.15:
            _backoff_until = time.monotonic() + 10.0
        return int(x), int(y + h), int(y), False
    except Exception:
        if time.monotonic() - t0 > 0.5:
            _backoff_until = time.monotonic() + 10.0
        return None


def uia_focused_rect():
    """焦点元素的包围矩形, 返回 (x, y_bottom, y_top) 或 None。

    供"封闭自绘 UI"(如微信)使用: 它们给不出精确光标, 但焦点元素
    (聊天栏/输入区)的矩形足够把候选框带到输入区域旁边。"""
    global _backoff_until
    if time.monotonic() < _backoff_until:
        return None
    try:
        t0 = time.monotonic()
        obj, UIA = _get_uia()
        el = obj.GetFocusedElement()
        if el is None:
            return None
        r = el.CurrentBoundingRectangle
        w, h = r.right - r.left, r.bottom - r.top
        if not (40 <= w <= 1600 and 40 <= h <= 1400):
            return None  # 整屏/零尺寸矩形不可用
        return int(r.left), int(r.bottom), int(r.top), True
    except Exception:
        return None
