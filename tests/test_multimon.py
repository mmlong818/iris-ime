"""多屏窗口定位的纯函数测试(不依赖真实显示器)。"""

import unittest

from ime.global_ime import fit_rect, monitor_work_area


class FitRectTest(unittest.TestCase):
    # 主屏 (0,0)-(1920,1080), 左副屏 (-1920,0)-(...), 上副屏 y<0 等多屏场景
    def test_primary_normal(self):
        self.assertEqual(fit_rect(100, 100, 600, 120, 0, 0, 1920, 1080), (100, 100))

    def test_secondary_monitor_negative_coords_kept(self):
        # 左副屏光标 x=-1500: 不再被拽回主屏
        self.assertEqual(fit_rect(-1500, 300, 600, 120, -1920, 0, 0, 1080),
                         (-1500, 300))

    def test_clamp_into_monitor(self):
        # 超出所在屏右缘 -> 收回
        self.assertEqual(fit_rect(1800, 100, 600, 120, 0, 0, 1920, 1080),
                         (1320, 100))

    def test_negative_overflow_clamped(self):
        self.assertEqual(fit_rect(-2500, 100, 600, 120, -1920, 0, 0, 1080),
                         (-1920, 100))

    def test_vertical_clamp(self):
        # 窗口必须完整落在所在屏内: x=100 越过右缘(0), 收到 -600
        self.assertEqual(fit_rect(100, -500, 600, 120, -1920, -1080, 0, 0),
                         (-600, -500))

    def test_taskbar_work_area_respected(self):
        # 工作区排除任务栏: bottom=1040
        self.assertEqual(fit_rect(100, 1000, 600, 120, 0, 0, 1920, 1040),
                         (100, 920))


class MonitorAreaTest(unittest.TestCase):
    def test_returns_sane_rect_for_current_desktop(self):
        # 真实环境冒烟: 返回值应为合法矩形(l <= r, t <= b), 多屏下可为负
        l, t, r, b = monitor_work_area(0, 0)
        self.assertLess(l, r)
        self.assertLess(t, b)


if __name__ == "__main__":
    unittest.main()
