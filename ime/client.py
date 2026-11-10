"""Windows 托盘客户端: 鸢尾输入法常驻程序。

托盘图标右键菜单:
  开启输入(Ctrl+Alt+P) —— 与热键等效
  模型: 4M(快) / 25M(准) —— 运行时切换
  开机自启 —— 写 HKCU Run 键
  学习记录…… —— 打开用户词文件所在目录
  统计…… —— 词条数/模型/状态一览
  退出

用法: python -m ime.client  (打包后直接双击 PinyinIME.exe)
自检: python -m ime.client --selftest
"""

import json
import os
import queue
import sys
import threading
import time
import winreg
from pathlib import Path

from PIL import Image, ImageDraw

from .global_ime import GlobalIme, send_text
from .reranker import SentenceModel, Reranker
from . import __version__
from .tsf_server import start_background as tsf_start_background, EngineService

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "PinyinIME"
MODELS = {"4M": "sentence-model-4m.safetensors", "25M": "sentence-model-25m.safetensors"}


def load_settings():
    path = app_data_dir() / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings):
    path = app_data_dir() / "settings.json"
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")


def app_data_dir():
    """打包后用户词写到 %APPDATA%, 开发态写项目 data/。"""
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", ".")) / "pinyin-ime"
    else:
        base = Path(__file__).resolve().parent.parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def model_path(name):
    """模型在包内 resources, 开发态在项目 models/。"""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "models"  # noqa: SLF001  PyInstaller 约定
    else:
        base = Path(__file__).resolve().parent.parent / "models"
    return base / MODELS[name]


def tray_image(engaged=False):
    """程序生成的托盘图标: 鸢尾紫方块 + P 字, 随输入状态变色。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body = (139, 124, 240, 255) if engaged else (110, 116, 125, 255)  # 鸢尾紫/灰
    d.rounded_rectangle([6, 6, 58, 58], radius=12, fill=body)
    d.rectangle([20, 14, 28, 50], fill="white")
    d.rectangle([20, 14, 46, 24], fill="white")
    d.rounded_rectangle([34, 24, 46, 38], radius=6, outline="white", width=3)
    return img


class WinClient(GlobalIme):
    def __init__(self):
        self.settings = {"fuzzy": True, "passthrough": [], **load_settings()}
        super().__init__(user_path=app_data_dir() / "user_words.json",
                         ui_settings_path=app_data_dir() / "ui.json",
                         fuzzy=self.settings.get("fuzzy", True),
                         error_log_path=app_data_dir() / "hook_errors.log")
        self.state["passthrough"] = set(self.settings.get("passthrough", []))
        # 语音输入(v2.1): FunASR 本地转写 + GLM 润色; PTT 默认右 Ctrl。
        # 依赖 funasr/torch(数 GB), 打包版首期不带——缺失时优雅降级,
        # 菜单置灰, 其余功能不受影响
        self.vcfg = None
        self.voice = None
        try:
            from .voice.config import VoiceConfig
            from .voice.voice_ime import VoiceIme
            self.vcfg = VoiceConfig()
            self.voice = VoiceIme(self.vcfg,
                                  on_state=self._voice_state,
                                  on_output=self._voice_output)
            self.state["voice_ptt"] = self.vcfg.ptt_vk
            self.state["voice_enabled"] = self.vcfg.enabled
            self.voice_win = None
            if self.vcfg.enabled:
                self.voice.preload()
        except Exception as exc:
            print(f"[client] 语音子系统不可用(打包版首期未内置): {exc}",
                  file=sys.stderr)
        self.model_name = "4M"
        self.stats_win = None
        self.icon = None  # 由 _init_tray 创建
        # TSF 原生前端的引擎服务随客户端自启(DLL 经命名管道查询本引擎);
        # 任何失败都不应拖垮客户端本身
        self.tsf_stop = threading.Event()
        try:
            tsf_start_background(service=EngineService(
                engine=self.controller.engine), stop=self.tsf_stop)
        except Exception as exc:
            print(f"[client] TSF 引擎服务未启动: {exc}", file=sys.stderr)

    # ---- 托盘 ----

    def _init_tray(self):
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem("开启输入 (Ctrl+Alt+P)",
                             self._menu_toggle, checked=lambda item: self.state["enabled"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: f"模型: {self.model_name}", None, enabled=False),
            pystray.MenuItem("用 4M(快, 默认)", lambda: self._menu_model("4M"),
                             radio=True, checked=lambda item: self.model_name == "4M"),
            pystray.MenuItem("用 25M(准, 较慢)", lambda: self._menu_model("25M"),
                             radio=True, checked=lambda item: self.model_name == "25M"),
            pystray.MenuItem("模糊音", self._menu_fuzzy,
                             checked=lambda item: self.controller.engine.fuzzy),
            pystray.MenuItem("语音输入 (按住右Ctrl)",
                             self._menu_voice,
                             enabled=lambda item: self.voice is not None,
                             checked=lambda item: bool(self.voice and
                                                       self.vcfg.enabled)),
            pystray.MenuItem("语音设置…", self._menu_voice_settings,
                             enabled=lambda item: self.voice is not None),
            pystray.MenuItem(
                lambda item: ("直通: " + (self.last_fg_app or "切到目标应用后点这里")),
                self._menu_passthrough,
                checked=lambda item: bool(self.last_fg_app
                                          and self.last_fg_app in
                                          self.state.get("passthrough", set()))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启", self._menu_autostart,
                             checked=lambda item: autostart_enabled()),
            pystray.MenuItem("学习记录…", self._menu_open_data),
            pystray.MenuItem("统计…", self._menu_stats),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._menu_quit),
        )
        self.icon = pystray.Icon("pinyin-ime", tray_image(),
                                 f"鸢尾输入法 v{__version__}", menu)
        self.icon.run_detached()

    def _menu_toggle(self, icon, item):
        self.state["enabled"] = not self.state["enabled"]   # 菜单线程翻转, tick 只提示
        self.events.put(("toggle", None, ""))

    def _menu_model(self, name):
        self.events.put(("model", name, ""))

    def _menu_fuzzy(self, icon, item):
        self.events.put(("fuzzy", None, ""))

    def _menu_voice(self, icon, item):
        if self.voice:
            self.events.put(("voice", None, ""))

    def _menu_voice_settings(self, icon, item):
        if self.voice:
            try:
                self.events.put(("voice_settings", None, ""))
            except Exception:
                pass
        else:
            try:
                with open(app_data_dir() / "hook_errors.log", "a",
                          encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S ") +
                            "[voice-settings] voice 子系统未加载\n")
            except OSError:
                pass

    # ---- 语音(主线程) ----

    def _voice_state(self, state, info):
        # 工作线程回调 → 只写共享字段, 渲染交给 _tick(主循环零阻塞红线)
        self.state["voice_state"] = state
        self.state["voice_info"] = {**info, "ts": time.monotonic()}

    def _voice_output(self, text, raw):
        # 工作线程回调: 注入走 SendInput, 线程安全; 不碰 Tk
        try:
            send_text(text)
        except Exception as exc:
            self.state["voice_state"] = "error"
            self.state["voice_info"] = {"message": f"上屏失败: {exc}"}

    def _apply_voice_toggle(self):
        self.vcfg.enabled = not self.vcfg.enabled
        self.voice.enabled = self.vcfg.enabled
        self.state["voice_enabled"] = self.vcfg.enabled
        if self.vcfg.enabled:
            self.voice.preload()
        else:
            self.voice.cancel()
        self._toast_until = time.time() + 0.9

    def _show_voice_settings(self):
        import tkinter as tk
        try:
            self._show_voice_settings_inner()
        except Exception:
            # 窗口程序异常不可见, 落盘诊断
            import traceback
            try:
                with open(app_data_dir() / "hook_errors.log", "a",
                          encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S ") +
                            "[voice-settings] " + traceback.format_exc() + "\n")
            except OSError:
                pass

    def _show_voice_settings_inner(self):
        import tkinter as tk
        if self.voice_win is not None:
            self.voice_win.destroy()
        win = self.voice_win = tk_toplevel(self.root)
        win.title("鸢尾输入法 · 语音设置")
        win.geometry("520x260")
        llm = self.vcfg.llm
        rows = [
            ("润色模型 base_url", "base_url", llm.get("base_url", "")),
            ("API Key", "api_key", llm.get("api_key", "")),
            ("模型", "model", llm.get("model", "")),
        ]
        entries = {}
        for label, key, val in rows:
            tk_label(win, label, row=0)
            e = tk.Entry(win, show="*" if key == "api_key" else "")
            e.insert(0, val)
            e.pack(fill="x", padx=12, pady=2)
            entries[key] = e
        status = tk.Label(win, text="", fg="#8b7cf0", bg="#20242c")
        status.pack(fill="x", padx=12, pady=4)

        def save():
            for key, e in entries.items():
                self.vcfg.llm[key] = e.get().strip()
            self.vcfg.save()
            ready = self.vcfg.llm_ready()
            status.configure(text="已保存 ✓ " + ("润色已就绪" if ready
                              else "Key 为空: 润色关闭, 松手直出转写稿"))
        tk.Button(win, text="保存", command=save,
                  bg="#8b7cf0", fg="#191236", relief="flat").pack(pady=6)

    def _menu_passthrough(self, icon, item):
        """把最近前台应用加入/移出直通白名单(远程桌面等转发键盘的应用)。"""
        exe = self.last_fg_app
        if not exe:
            return
        lst = list(self.settings.get("passthrough", []))
        if exe in lst:
            lst.remove(exe)
        else:
            lst.append(exe)
        self.settings["passthrough"] = lst
        save_settings(self.settings)
        self.state["passthrough"] = set(lst)   # 整体替换, 钩子线程读取安全
        self.icon.update_menu()

    def _menu_autostart(self, icon, item):
        set_autostart(not autostart_enabled(), exe_path())

    def _menu_open_data(self, icon, item):
        os.startfile(app_data_dir())

    def _menu_stats(self, icon, item):
        self.events.put(("stats", None, ""))

    def _menu_quit(self, icon, item):
        self.events.put(("quit", None, ""))

    # ---- 事件处理(主线程) ----

    def _apply_model(self, name):
        try:
            if name == "4M":
                self.controller.engine.attach_reranker(model_path("4M"))
            else:
                p = model_path("25M")
                self.controller.engine.reranker = (Reranker(SentenceModel(p))
                                                   if p.exists() else None)
            self.model_name = name
        except Exception as exc:
            self.status.configure(text=f"模型切换失败: {exc}")

    def _show_stats(self):
        if self.stats_win is not None:
            self.stats_win.destroy()
        win = self.stats_win = tk_toplevel(self.root)
        eng = self.controller.engine
        lines = [
            f"鸢尾输入法 v{__version__}",
            f"输入状态: {'开启' if self.state['enabled'] else '关闭'} (Ctrl+Alt+P)",
            f"重排序模型: {self.model_name}" if eng.reranker else "重排序模型: 未启用",
            f"TSF 引擎服务: 运行中"
            f"学习词条: {len(eng.user_counts)} 条",
            f"用户词文件: {eng.user_path}",
            "打字: 切到英文键盘后, 打全拼, 空格上屏",
        ]
        for i, line in enumerate(lines):
            w = tk_label(win, line, row=i)
        win.title("鸢尾输入法 · 统计")

    # ---- 运行 ----

    def run(self):
        self._init_tray()
        self.hook.start()
        # GlobalIme.__init__ 已排定 _tick 循环, 这里只跑主循环, 避免双 ticker
        self.root.mainloop()
        if self.icon:
            self.icon.stop()

    def on_fg_app_changed(self):
        if self.icon:
            self.icon.update_menu()  # 菜单标签显示新前台应用名

    def _tick(self):  # 覆写: 处理托盘菜单事件
        self._watchdog()
        toggled = False
        try:
            while True:
                kind, arg, ch = self.events.get_nowait()
                if kind == "toggle":
                    # 状态已由钩子/菜单翻转, 这里只做提示——二次翻转会让按键失效
                    self._toast_until = time.time() + 0.9
                    toggled = True
                elif kind == "passthrough":
                    # 前台应用直通: 清掉组词中的编码(按键已原生透传)
                    if self.controller.buffer:
                        self.controller.feed_key("Escape")
                        delta = self.controller.output
                        if delta:
                            self.controller.output = ""
                            send_text(delta)
                        self.state["buffer"] = self.controller.buffer
                elif kind == "voice":
                    self._apply_voice_toggle()
                elif kind == "voice_settings":
                    self._show_voice_settings()
                elif kind == "voice_ptt_down":
                    self.voice.ptt_down()
                elif kind == "voice_ptt_up":
                    self.voice.ptt_up()
                elif kind == "fuzzy":
                    self.controller.engine.fuzzy = not self.controller.engine.fuzzy
                    self.settings["fuzzy"] = self.controller.engine.fuzzy
                    save_settings(self.settings)
                    toggled = True
                elif kind == "model":
                    self._apply_model(arg)
                elif kind == "stats":
                    self._show_stats()
                elif kind == "quit":
                    self.root.after(10, self._shutdown)
                    return
                else:  # ("key", keysym, char)
                    self.controller.feed_key(arg, ch)
                    delta = self.controller.output
                    if delta:
                        self.controller.output = ""
                        send_text(delta)
                    self.state["buffer"] = self.controller.buffer
        except queue.Empty:
            pass
        if self.icon:
            self.icon.icon = tray_image(self.state["enabled"])
            if toggled:
                self.icon.update_menu()
        self.controller.settle_rerank()
        self.controller.poll_rerank()  # 后台重排算完且缓冲未变 → 换头
        self._render()
        self.root.after(20, self._tick)

    def _shutdown(self):
        try:
            self.hook.stop()
        finally:
            if self.icon:
                self.icon.stop()
            self.root.destroy()


# ---- 小工具(避免顶部到处 import tkinter) ----

def tk_toplevel(root):
    import tkinter as tk
    win = tk.Toplevel(root)
    win.geometry("460x210")
    win.attributes("-topmost", True)
    win.configure(bg="#20242c")
    return win


def tk_label(win, text, row):
    import tkinter as tk
    lbl = tk.Label(win, text=text, font=("Microsoft YaHei UI", 11),
                   fg="#e8eaee", bg="#20242c", anchor="w")
    lbl.pack(fill="x", padx=12, pady=4)
    return lbl


# ---- 开机自启(HKCU Run) ----

def exe_path():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve().parent.parent / "client_main.py"}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable, path):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, path)
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def main():
    if "--voice-selftest" in sys.argv:
        # 冻结版语音自检: 转写指定 WAV, 结果写 voice_selftest.txt(窗口程序
        # 无 stdout)。用法: IrisIME.exe --voice-selftest C:\path\to\test.wav
        import traceback
        from .voice.config import VoiceConfig, voice_data_dir
        from .voice.asr_local import LocalASR
        out = voice_data_dir() / "voice_selftest.txt"
        try:
            wav = sys.argv[sys.argv.index("--voice-selftest") + 1]
            diag = []
            for mod in ("funasr", "funasr_onnx", "sounddevice", "librosa",
                        "onnxruntime"):
                try:
                    __import__(mod)
                    diag.append(f"{mod}: OK")
                except Exception as e:
                    import traceback as _tb
                    diag.append(f"{mod}: {e!r}\n{_tb.format_exc(limit=3)}")
            with open(out, "w", encoding="utf-8") as f:
                f.write("\n".join(diag) + "\n----\n")
            cfg = VoiceConfig()
            asr = LocalASR(cfg.asr)
            t0 = time.time()
            text = asr.transcribe(wav)
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"backend={asr._backend} {time.time()-t0:.2f}s "
                        f"text={text!r}\n")
            sys.exit(0)
        except SystemExit:
            raise
        except Exception:
            with open(out, "a", encoding="utf-8") as f:
                f.write("FAIL\n" + traceback.format_exc())
            sys.exit(1)
    if "--selftest" in sys.argv:
        from .global_ime import selftest as hook_selftest
        sys.exit(hook_selftest())
    client = WinClient()
    try:
        client.run()
    except Exception:
        # 打包后无控制台, 崩溃写日志
        import traceback
        log = app_data_dir() / "crash.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
