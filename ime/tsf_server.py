r"""TSF 前端的引擎服务: 命名管道 \\.\pipe\iris-ime。

架构(小狼毫同款): C++ TSF 前端 DLL 只做按键/组合/候选窗摆放,
解码与候选排序由本服务进程内的 Python 引擎承担, 经命名管道通信。

协议: 长度前缀(4 字节小端) + UTF-8 JSON 行
  请求: {"op": "candidates", "pinyin": "nihao", "context": "..."}
        {"op": "commit", "text": "你好", "key": "nihao"}
        {"op": "ping"}
  响应: {"candidates": [{"text": "...", "pinyin": "..."}], ...}
        {"ok": true}

用法: python -m ime.tsf_server   (需先启动, 再激活 TSF 输入法)
"""

import json
import sys
import threading
import time

import pywintypes
import win32file      # pip install pywin32
import win32pipe

from .engine import Engine

PIPE_NAME = r"\\.\pipe\iris-ime"
BUF_SIZE = 65536


class EngineService:
    """包装引擎, 提供管道协议到引擎调用的映射。"""

    def __init__(self, engine=None):
        self.engine = engine or Engine(learn=True)
        try:
            self.engine.attach_reranker()
        except Exception as exc:
            print(f"[tsf_server] 重排序未启用: {exc}", file=sys.stderr)
        self._lock = threading.Lock()   # 引擎非线程安全, 串行化

    def handle(self, req: dict) -> dict:
        op = req.get("op")
        with self._lock:
            if op == "ping":
                return {"ok": True, "version": __import__("ime").__version__}
            if op == "candidates":
                pinyin = req.get("pinyin", "")
                context = req.get("context", "")
                r = self.engine.decode(pinyin, context=context)
                return {
                    "candidates": [
                        {"text": c.text, "pinyin": c.pinyin, "kind": c.kind}
                        for c in r.candidates
                    ],
                    "consumed": r.consumed,
                }
            if op == "commit":
                # TSF 前端上屏时回调: 走用户词学习(连续合成词学习在
                # 前端侧记录先后两段, 以 prev_text/prev_key 传入)
                text, key = req.get("text", ""), req.get("key", "")
                self.engine.learn(text, key, req.get("kind", "word"))
                prev_text, prev_key = req.get("prev_text"), req.get("prev_key")
                if prev_text and prev_key:
                    self.engine.learn(prev_text + text, prev_key + key, "word")
                return {"ok": True}
            return {"error": f"unknown op {op!r}"}


def serve_client(pipe_handle, service: EngineService, stop: threading.Event):
    """处理单个客户端连接(一个 TSF DLL 实例一条管道)。"""
    try:
        while not stop.is_set():
            try:
                hdr = win32file.ReadFile(pipe_handle, 4)[1]
            except pywintypes.error:
                break  # 客户端断开
            if len(hdr) < 4:
                break
            length = int.from_bytes(hdr, "little")
            chunks, remain = [], length
            while remain > 0:
                data = win32file.ReadFile(pipe_handle, min(remain, BUF_SIZE))[1]
                chunks.append(data)
                remain -= len(data)
            try:
                req = json.loads(b"".join(chunks).decode("utf-8"))
                resp = service.handle(req)
            except Exception as exc:
                resp = {"error": repr(exc)}
            data = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            try:
                win32file.WriteFile(pipe_handle,
                                    len(data).to_bytes(4, "little") + data, None)
            except pywintypes.error:
                break
    finally:
        try:
            win32file.FlushFileBuffers(pipe_handle)
            win32file.DisconnectNamedPipe(pipe_handle)
        except pywintypes.error:
            pass


def accept_loop(service, stop):
    """接受连接循环: 每个客户端连接交给独立线程处理。"""
    while not stop.is_set():
        handle = None
        try:
            handle = win32pipe.CreateNamedPipe(
                PIPE_NAME,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE
                | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES,
                BUF_SIZE, BUF_SIZE, 0, None)
        except pywintypes.error as exc:
            print(f"[tsf_server] 建管道失败: {exc}", file=sys.stderr)
            stop.wait(1.0)
            continue
        try:
            win32pipe.ConnectNamedPipe(handle, None)
        except pywintypes.error:
            pass  # 客户端已在建立瞬间连上
        threading.Thread(target=serve_client, args=(handle, service, stop),
                         daemon=True).start()


def start_background(service=None, stop=None):
    """在守护线程中启动管道服务(托盘客户端随启随用)。"""
    service = service or EngineService()
    stop = stop or threading.Event()
    t = threading.Thread(target=accept_loop, args=(service, stop), daemon=True)
    t.start()
    return t


def main():
    print(f"[tsf_server] 引擎就绪, 监听 {PIPE_NAME}")
    start_background().join()


if __name__ == "__main__":
    main()
