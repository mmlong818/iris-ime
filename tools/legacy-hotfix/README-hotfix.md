# IrisIME（鸢尾输入法）v2.0.0-alpha.3 崩溃修复包

本文件夹包含对 IrisIME-Setup-v2.0.0-alpha.3 的 NameError 崩溃修复，可整体拷走，与开发项目无关。

## 问题

启动即崩溃：
```
File "ime\client.py", line 92, in __init__
NameError: name 'EngineService' is not defined
```

根因：`ime/client.py` 第 29 行只导入了 `from ime.tsf_server import start_background`，
第 92 行却用到了 `EngineService`（定义在 `ime/tsf_server.py` 第 31 行），漏了导入。

## 修复方式（无需厂商源码）

`patch_irisime.py`：替换 exe 内嵌 PYZ 代码包中的 `ime/__init__` 字节码，
在包初始化时把 `EngineService` 注入 builtins，兜住运行时全局查找，然后重组 PyInstaller 归档。

## 产物

| 文件 | 用途 |
| --- | --- |
| `IrisIME-Setup-v2.0.0-alpha.3-fixed.exe` | 重打包的完整安装包（推荐）。在目标机器先卸载旧版再安装。 |
| `IrisIME-patched.exe` | 仅替换主程序。覆盖安装目录里的 `IrisIME.exe`，**必须与 `_internal` 文件夹同目录运行**（单独拷到桌面运行会报 "Failed to load Python DLL ... python312.dll"）。 |

## 验证记录（2026-10-28/29）

- 原版 exe 复现崩溃 traceback 逐字一致
- 补丁版连续运行 50 秒以上无崩溃，模型正常加载
- 重打包安装包解包核对主程序与补丁版逐字节一致，解包运行正常

## 注意

安装向导界面为英文（原包内嵌中文向导资源无法还原），不影响程序本身。
厂商后续 alpha.4 / 正式版发布后建议直接升级覆盖本补丁。
