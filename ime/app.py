"""输入法界面: 置顶候选窗 + 打字板。

结构: InputController(纯逻辑状态机, 可脱离 GUI 测试) + ImeApp(Tk 视图)。
运行: python -m ime.app   (需本机有图形环境; 打字前请切到英文键盘,
      否则系统输入法会先拦截按键)

按键: 字母=编码  空格/数字=选词  回车=字母原样上屏  Esc=清空  退格=删编码
"""

import sys
import threading
import time
from pathlib import Path

import tkinter as tk

from .engine import PAGE_SIZE, Engine
from .textstyle import space_text  # 可选能力, 默认关闭(auto_space=False)

KEY_HINT = ("字母=编码 · 空格/回车=上屏中文 · 数字=选词 · 双击退格=单字模式 · "
            "Esc=清空 · 退格=删编码")


class InputController:
    """按键 -> 编码缓冲 / 候选 / 上屏 的状态机。"""

    def __init__(self, engine=None, use_reranker=True, learn=True, model_path=None,
                 user_path=None, auto_space=False, space_digits=False, fuzzy=True):
        self.engine = engine or Engine(learn=learn, user_path=user_path, fuzzy=fuzzy)
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
        self.char_mode = False  # 单字模式: 候选把单字提到最前(词表外组词)
        self._bs_ts = 0.0       # 上次退格时刻(双击检测)
        self._bs_restored = ""  # 双击还原: 第一次退格删掉的字母
        self._rerank_pending = False   # 打字中挂起重排, 停顿后补
        self._rerank_busy = False      # 后台重排线程在跑
        self._rerank_done = None       # (缓冲快照, result 对象, 置顶下标/None)
        self._rerank_lock = threading.Lock()
        self._last_key_ts = 0.0
        self.on_change = None  # 回调, 参数为状态 dict

    def _decode(self, rerank=True):
        if self.buffer:
            if self.char_mode:
                # 单字模式: 解码截到首音节——候选全是单字, 选中只上屏
                # 一个字, 余下编码留缓冲(逐字组词, 3 秒合成词学习接住)。
                # 不做神经重排: 模型偏爱词组, 会挤掉用户点名要的单字。
                k = self.engine.first_syllable(self.buffer)
                seg = self.buffer[:k] if k else self.buffer
                self.result = self.engine.decode(seg, context=self.committed[-64:],
                                                 rerank=False)
            else:
                self.result = self.engine.decode(self.buffer, context=self.committed[-64:],
                                                 rerank=rerank)
        else:
            self.result = None
            self.char_mode = False  # 新组合开始, 单字模式不跨组合
        self.page = 0  # 编码变了, 回到第一页

    def settle_rerank(self):
        """打字停顿后, 后台线程补一次重排(主线程零等待)。

        神经重排对长句可达秒级(25M 模型), 放主线程会把整个客户端卡死——
        v2.0.0-alpha.11 的"卡住"即此。算完由 poll_rerank 应用: 缓冲已变
        (用户继续打字/已选词)则丢弃。同一时刻至多一个重排线程。
        """
        if not (self._rerank_pending and self.buffer and not self.char_mode
                and self.engine.reranker is not None):
            return
        with self._rerank_lock:
            if self._rerank_busy:
                return
            self._rerank_busy = True
        res, buf, ctx = self.result, self.buffer, self.committed[-64:]
        self._rerank_pending = False

        def work():
            try:
                idx = self.engine.rerank_candidates(res, ctx)
            except Exception:
                idx = None
            self._rerank_done = (buf, res, idx)
            with self._rerank_lock:
                self._rerank_busy = False

        threading.Thread(target=work, daemon=True, name="iris-rerank").start()

    def poll_rerank(self):
        """取回后台重排结果; 缓冲未变才换头并通知视图。返回是否有变化。"""
        done = self._rerank_done
        if done is None:
            return False
        self._rerank_done = None
        if self.char_mode:
            return False  # 单字模式下不换头(模型偏爱词组)
        buf, res, idx = done
        if idx is None or buf != self.buffer or self.result is not res:
            return False
        cands = res.candidates
        if 0 < idx < len(cands):
            cands[0], cands[idx] = cands[idx], cands[0]
            self.page = 0
            self._notify()
            return True
        return False

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
            "char_mode": self.char_mode,
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
        # 连续上屏合成词学习: 3 秒内先后上屏的两段, 学成词组
        # (yingbian → 选"硬" → 选"编" => 记住"硬编"), 搜狗同款机制
        import time as _time
        now = _time.monotonic()
        prev = getattr(self, "_prev_commit", None)
        if (prev and now - prev[2] < 3.0 and key and prev[1]
                and kind != "initial" and prev[3] != "initial"
                and len(prev[0] + text) <= 10):
            self.engine.learn(prev[0] + text, prev[1] + key, kind)
        self._prev_commit = (text, key, now, kind)

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
            now = time.monotonic()
            if self.buffer and now - self._bs_ts <= 0.45:
                # 双击退格: 切换单字模式, 并还原第一次退格删掉的字母——
                # 词表外两字词不用翻页确认, 直接逐字选
                if self._bs_restored:
                    self.buffer += self._bs_restored
                    self._bs_restored = ""
                self.char_mode = not self.char_mode
                self._bs_ts = 0.0
                self._decode(rerank=False)
                self._rerank_pending = False
            else:
                self._bs_ts = now if self.buffer else 0.0
                self._bs_restored = ""
                if self.buffer:
                    self._bs_restored = self.buffer[-1]
                    self.buffer = self.buffer[:-1]
                    self._decode(rerank=False)
                    self._rerank_pending = True
                    self._last_key_ts = now
                else:
                    changed = False
        elif keysym == "Escape":
            self.buffer = ""
            self._decode()
        elif keysym == "Return":
            # 经典 IME 惯例: 回车 = 字母原样上屏(中文用空格)。不附加换行——
            # 终端类应用里再按一次回车(空编码透传)即发送
            if self.buffer:
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
            self._decode(rerank=False)
            self._rerank_pending = True
            self._last_key_ts = time.monotonic()
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
        r.title("鸢尾输入法 · 打字板")
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
        self._poll_rerank()
        self.render({})

    def _poll_rerank(self):
        # 后台重排结果落地(打字板没有 _tick 心跳, 自建 120ms 轮询)
        if self.controller.poll_rerank():
            self.render({})
        self.root.after(120, self._poll_rerank)

    # ---- 视图更新 ----

    def render(self, state):
        buf, cands = state.get("buffer", ""), state.get("candidates", [])
        preview, remainder = state.get("preview", ""), state.get("remainder", "")
        if buf or cands:
            page_note = (f"  第{self.controller.page + 1}/{self.controller.page_count}页"
                         if self.controller.page_count > 1 else "")
            char_note = "  [单字]" if self.controller.char_mode else ""
            text = f"{buf} → {preview}{'▏' + remainder if remainder else ''}{char_note}{page_note}"
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
