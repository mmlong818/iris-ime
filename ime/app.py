"""输入法界面: 置顶候选窗 + 打字板。

结构: InputController(纯逻辑状态机, 可脱离 GUI 测试) + ImeApp(Tk 视图)。
运行: python -m ime.app   (需本机有图形环境; 打字前请切到英文键盘,
      否则系统输入法会先拦截按键)

按键: 字母=编码  空格/数字=选词  回车=字母原样上屏  Esc=清空  退格=删编码
"""

import sys
from pathlib import Path

import tkinter as tk

from .engine import PAGE_SIZE, Engine
from .textstyle import space_text  # 可选能力, 默认关闭(auto_space=False)

KEY_HINT = "字母=编码 · 空格/回车=上屏中文 · 数字=选词 · Esc=清空 · 退格=删编码"


class InputController:
    """按键 -> 编码缓冲 / 候选 / 上屏 的状态机。"""

    def __init__(self, engine=None, use_reranker=True, learn=True, model_path=None,
                 user_path=None, auto_space=False, space_digits=False):
        self.engine = engine or Engine(learn=learn, user_path=user_path)
        self.auto_space = auto_space      # 盘古之白, 默认关闭(需求已取消)
        self.space_digits = space_digits
        self.reranker_on = False
        if use_reranker and self.engine.reranker is None:
            try:
                self.engine.attach_reranker(model_path)
                self.reranker_on = True
            except Exception as exc:  # scipy/权重缺失时静默降级
                print(f"[重排序模型未启用: {exc}]", file=sys.stderr)
        elif self.engine.reranker is not None:
            self.reranker_on = True
        self.buffer = ""
        self.result = None
        self.output = ""       # 增量, 由视图取走
        self.committed = ""    # 累计上屏文本, 作为重排序上下文
        self.last_commit = ""
        self.page = 0          # 候选页码(0 起)
        self.page_size = PAGE_SIZE
        self.on_change = None  # 回调, 参数为状态 dict

    def _decode(self):
        if self.buffer:
            self.result = self.engine.decode(self.buffer, context=self.committed[-64:])
        else:
            self.result = None
        self.page = 0  # 编码变了, 回到第一页

    @property
    def page_candidates(self):
        """当前页的候选(绝对下标元组 + 文本)。"""
        if not self.result:
            return []
        cands = self.result.candidates
        start = self.page * self.page_size
        return list(enumerate(cands[start:start + self.page_size], start=start))

    @property
    def page_count(self):
        if not self.result:
            return 1
        return max(1, -(-len(self.result.candidates) // self.page_size))

    def turn_page(self, delta):
        if self.result and self.result.candidates:
            self.page = min(max(self.page + delta, 0), self.page_count - 1)

    def _notify(self):
        if not self.on_change:
            return
        cands = self.result.candidates if self.result else []
        self.on_change({
            "buffer": self.buffer,
            "candidates": [c.text for c in cands],
            "preview": cands[0].preview if cands else "",
            "remainder": self.result.remainder if self.result else "",
            "output": self.output,
        })

    def _commit(self, text, key="", kind=""):
        # 上屏展示用整形后的文本; 学习与重排上下文用原始文本(空格会变 UNK 噪声)
        if self.auto_space and text.strip():
            display = space_text(text, prev_tail=self.committed[-1:],
                                 space_digits=self.space_digits)
        else:
            display = text
        self.output += display
        self.committed += text
        self.last_commit = display
        self.engine.learn(text, key, kind)

    def _select(self, idx):
        if not (self.result and 0 <= idx < len(self.result.candidates)):
            return False
        cand = self.result.candidates[idx]
        self._commit(cand.text, self.buffer[:cand.commit_end], cand.kind)
        self.buffer = self.buffer[cand.commit_end:]
        self._decode()
        return True

    def feed_key(self, keysym, char=""):
        """处理一次按键, 返回是否产生了状态变化。"""
        changed = True
        if keysym == "BackSpace":
            if self.buffer:
                self.buffer = self.buffer[:-1]
                self._decode()
            else:
                changed = False
        elif keysym == "Escape":
            self.buffer = ""
            self._decode()
        elif keysym == "Return":
            if self.result and self.result.candidates:
                # 组词中回车 = 上屏中文(同空格)。不附加换行——否则终端类应用
                # (Claude Code)会把未确认的内容直接发送
                cand0 = self.result.candidates[0]
                self._commit(cand0.preview, self.buffer[:self.result.consumed], cand0.kind)
                self.buffer = self.buffer[self.result.consumed:]
                self._decode()
            elif self.buffer:
                # 无法解码的输入(命令/网址): 字母原样上屏, 不附加回车
                self._commit(self.buffer)
                self.buffer = ""
                self._decode()
            else:
                self._commit("\n")  # 打字板空编码换行; 全局模式空编码回车走原生放行
        elif keysym == "space":
            # 整句上屏: 空格提交整个预览(含前缀/简拼补全), 余下字母留在缓冲
            if self.result and self.result.candidates:
                cand0 = self.result.candidates[0]
                self._commit(cand0.preview, self.buffer[:self.result.consumed], cand0.kind)
                self.buffer = self.buffer[self.result.consumed:]
                self._decode()
            else:
                self._commit(" ")
        elif keysym in ("Next", "Page_Down") or char == "=":
            self.turn_page(+1)
        elif keysym in ("Prior", "Page_Up") or char == "-":
            self.turn_page(-1)
        elif keysym.startswith("KP_") and keysym[3:].isdigit():
            self._select(self.page * self.page_size + int(keysym[3:]) - 1)
        elif char.isalpha():
            self.buffer += char.lower()
            self._decode()
        elif char.isdigit():
            self._select(self.page * self.page_size + int(char) - 1)
        else:
            changed = False
        if changed:
            self._notify()
        return changed


class ImeApp:
    def __init__(self, root, model_path=None):
        self.root = root
        self.controller = InputController(model_path=model_path)
        self.controller.on_change = self.render
        self._build_widgets()

    def _build_widgets(self):
        r = self.root
        r.title("拼音输入法 · 打字板")
        r.geometry("560x430")
        r.attributes("-topmost", True)
        r.configure(bg="#1e1e1e")

        self.preedit = tk.Label(
            r, text="在此直接打字(切到英文键盘)", font=("Microsoft YaHei UI", 15),
            fg="#ffffff", bg="#1e1e1e", anchor="w", pady=8)
        self.preedit.pack(fill="x", padx=12)

        self.cand_frame = tk.Frame(r, bg="#1e1e1e")
        self.cand_frame.pack(fill="x", padx=12, pady=4)

        self.output = tk.Text(
            r, font=("Microsoft YaHei UI", 14), bg="#141414", fg="#e8e8e8",
            wrap="char", relief="flat", height=12, padx=10, pady=8)
        self.output.pack(fill="both", expand=True, padx=12, pady=(4, 2))
        self.output.configure(state="disabled")

        hint = tk.Label(r, text=KEY_HINT, font=("Microsoft YaHei UI", 10),
                        fg="#888888", bg="#1e1e1e", anchor="w")
        hint.pack(fill="x", padx=14, pady=4)

        r.bind("<Key>", self.on_key)
        self.render({})

    # ---- 视图更新 ----

    def render(self, state):
        buf, cands = state.get("buffer", ""), state.get("candidates", [])
        preview, remainder = state.get("preview", ""), state.get("remainder", "")
        if buf or cands:
            page_note = (f"  第{self.controller.page + 1}/{self.controller.page_count}页"
                         if self.controller.page_count > 1 else "")
            text = f"{buf} → {preview}{'▏' + remainder if remainder else ''}{page_note}"
        else:
            text = "在此直接打字(切到英文键盘)"
        self.preedit.configure(text=text)

        for w in self.cand_frame.winfo_children():
            w.destroy()
        for idx, cand in self.controller.page_candidates:
            btn = tk.Label(
                self.cand_frame, text=f"{idx % self.controller.page_size + 1}.{cand.text}",
                font=("Microsoft YaHei UI", 14), fg="#d0d0ff" if idx == 0 else "#bbbbbb",
                bg="#2a2a3a" if idx == 0 else "#1e1e1e", padx=8, pady=2, cursor="hand2")
            btn.pack(side="left")
            btn.bind("<Button-1>", lambda e, i=idx: (self.controller._select(i),
                                                     self._after_select()))

        out = state.get("output", "")
        if out:
            self.output.configure(state="normal")
            pos_before = self.output.index("end-1c")
            self.output.insert("end", out)
            self.output.see("end")
            self.output.configure(state="disabled")
            # 复制最近一次上屏内容到剪贴板
            if self.controller.last_commit:
                self.root.clipboard_clear()
                self.root.clipboard_append(self.controller.last_commit)
                self.controller.last_commit = ""
            self.controller.output = ""

    def _after_select(self):
        self.controller._decode()
        self.controller._notify()

    def on_key(self, event):
        self.controller.feed_key(event.keysym, event.char)


def main():
    if "--smoke" in sys.argv:
        # 冒烟自检不写用户词文件
        ctl = InputController(use_reranker=False, learn=False)
        for ch in "nihao":
            ctl.feed_key(ch, ch)
        ctl.feed_key("space")
        print("冒烟测试:", repr(ctl.output))
        assert "你好" in ctl.output, ctl.output
        for ch in "wojintianqushangban":
            ctl.feed_key(ch, ch)
        ctl.feed_key("space")
        print("整句:", repr(ctl.output))
        assert "我今天去上班" in ctl.output, ctl.output
        print("OK")
        return
    root = tk.Tk()
    model_path = None
    if "--25m" in sys.argv:
        model_path = Path(__file__).resolve().parent.parent / "models" / "sentence-model-25m.safetensors"
    ImeApp(root, model_path=model_path)
    root.mainloop()


if __name__ == "__main__":
    main()
