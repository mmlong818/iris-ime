# 拼音输入法(自研原型) · 水杉

一个纯 Python 的全拼输入法: 词格 DAG + Viterbi 整句解码引擎 + 神经重排序,
两种前端——Tkinter 打字板与**全局键盘钩子模式**(任意应用里打字)。
支持全拼、未打完音节的预输入补全、简拼(声母串)。

词库基于 [rime-ice](https://github.com/gaboolic/rime-ice) 的字表与基础词库
(8105 单字 / 53.6 万词条); 评测集与重排序模型来自
[chinese-ime-lm](https://github.com/metasequoiaime/chinese-ime-lm)(MIT 语料,
模型 Apache-2.0)。

**许可分层**: 代码 MIT; `dicts/*.yaml` 词表为 rime-ice 的 GFDL 1.3;
模型权重 Apache-2.0——详见 [LICENSE](LICENSE) 与
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。发布包见
[Releases](../../releases)。

## 快速开始

```bash
# 1. 编译词库缓存(首次, 约 20 秒)——已提交 dicts/cache.pkl 可跳过
python build_cache.py

# 2. (可选, 启用神经重排序)下载 4M 模型权重并安装推理依赖
pip install numpy scipy
curl -L -o models/sentence-model-4m.safetensors \
  "https://huggingface.co/metasequoiaime/pinyin-ime-reranker-4M/resolve/main/sentence-model.safetensors"

# 3. 启动输入法
python -m ime.client      # 托盘客户端(推荐, 见下节)
python -m ime.app         # 打字板模式(自带输出区)
python -m ime.app --25m   # 同上, 用 25M 大模型(更准, 单次 ~160ms)
python -m ime.global_ime  # 全局模式(无托盘)

# 4. 测试与评测
python -m unittest discover -s tests
python tools/eval.py          # 默认参数评测
python tools/eval.py --grid   # 网格搜索调参
```

## 中英文混排空格(ime/textstyle.py, 默认关闭)

上屏文本自动在汉字与英文之间加半角空格("盘古之白")的能力已实现但**默认
关闭**——社区对"该不该加"没有共识, 故不作为默认行为。规则: 汉字↔英文字母
加空格; 数字默认不加(可开); 标点接缝不动; 已有空格不重复; 逐段上屏时按
上一段末字符补段首空格。开启方式: `InputController(auto_space=True)`。

## Windows 客户端(ime/client.py)

托盘常驻应用 = 全局钩子模式 + 托盘菜单。开发态 `python -m ime.client`;
发布版双击 `dist/PinyinIME/PinyinIME.exe` 即用, 无需 Python。

托盘菜单: 开启输入(与 Ctrl+Alt+P 等效) · 模型 4M/25M 运行时热切换 ·
开机自启(写 HKCU Run 键) · 打开学习记录目录 · 统计(词条数/模型/状态) · 退出。

打包:

```bash
pyinstaller --noconfirm --name PinyinIME --windowed --onedir --paths . \
  --add-data "models/sentence-model-4m.safetensors;models" \
  --add-data "models/sentence-model-25m.safetensors;models" \
  --add-data "dicts/cache.pkl;dicts" client_main.py
```

打包态行为差异: 用户词与崩溃日志写到 `%APPDATA%\pinyin-ime\`(开发态在
项目 `data/`); 词库缓存与双模型随包分发(约 65MB)。异常退出时先看该目录
下的 `crash.log`。

## 全局模式(ime/global_ime.py)

`python -m ime.global_ime` 启动后用 **Ctrl+Alt+P** 开关, **Shift 单击**切中/英,
**候选窗跟随输入光标**: 定位链为经典 caret(GetGUIThreadInfo)→ MSAA 无障碍
光标(OBJID_CARET, Chrome/Edge/Office 等现代应用; Chromium 系应用的无障碍树
默认休眠, 每次查询前先以 OBJID_CLIENT 敲门激活, 通常一两键后生效)→
跟随鼠标兜底; 光标贴屏幕底部时窗口自动翻到光标上方。**多屏**: 窗口按光标所在显示器的工作区钳制与
翻转(副屏负坐标正确处理), 进程声明 per-monitor DPI awareness 保证混合缩放
下坐标一致。空格整句上屏、数字选词,
**PgUp/PgDn 或 -/= 翻页**, Esc 清编码, 回车原样上屏。**Caps Lock 开启期间
字母一律直通**(不进编码, 与主流输入法一致), 状态条显示 "Caps(字母直通)";
Shift+字母同样直传。
**外观**: "水杉"主题, 刻意与主流输入法(白底蓝字)区分——深色玻璃拟态
(整窗 94% 不透明度 + 圆角抠角), 水杉绿点缀(品牌圆点/首选实心绿药丸/
拼音绿灰), 候选为**药丸形**(Canvas 绘制, 悬停变亮、可点击, NOACTIVATE
不抢目标焦点), 宽度按内容自适应(上限跟随虚拟桌面宽度, 超宽自动换行不裁词),
闲置时收成带绿点的细条; 托盘图标同色系。
`python -m ime.global_ime --selftest` 做无人值守端到端自检(注入合成按键验证
钩子→解码→上屏链路, 测试期间临时关闭 Caps Lock 并恢复)。

原理与边界: 低级键盘钩子(WH_KEYBOARD_LL, ctypes 实现, 零编译依赖)拦截按键,
主线程解码渲染, 选词后 SendInput 以 Unicode 注入文本。这是"仿真输入法"——
没有 TSF 组合窗口, 目标应用看到的是直接输入的文本, 高级应用的输入框内联
预览不会出现。注入事件一律放行避免自反馈; 钩子回调只入队不解码, 异常时
立即禁用并放行; 用前需把系统输入法切到英文(否则搜狗先截获按键)。
成为真正的 TSF 输入法需把引擎移植为原生 DLL(本机无 MSVC, 属后续工程)。

引擎单独使用:

```python
from ime import Engine
e = Engine()
r = e.decode("wojintianqushangban")
print(r.candidates[0].preview)   # -> 我今天去上班
```

## 按键

| 键 | 行为 |
|---|---|
| `a-z` | 输入编码 |
| `空格` / `回车` | 上屏中文(整句最优); **回车不附加换行**——终端类应用里不会把未确认内容直接发送 |
| `1-9` / 点候选 | 选当前页候选, 逐段确认模式只上屏首段 |
| `PageUp/PageDown` 或 `-`/`=` | 候选翻页(共 27 个, 3 页) |
| `回车`(无编码) | 原生回车(打字板里换行; 全局模式透传给应用) |
| `回车`(无法解码) | 字母原样上屏, 不附加回车 |
| `退格` / `Esc` | 删一个编码 / 清空编码 |

需要打纯英文时: Caps Lock 开启字母直通, 或 Shift 单击切到英模式。

## 评测(chinese-ime-lm 评测集)

| 指标 | 词级 25,119 条 | 整句 60 条 |
|---|---|---|
| 引擎(无重排) | top1 **78.8%**, top5 **95.8%** | top1 71.7%, 金标在列 88.3% |
| 引擎 + 4M 重排序 | top1 **78.8%**, top5 **95.8%** | top1 **83.3%**, 金标在列 88.3% |
| 引擎 + 25M 重排序 | top1 **78.8%**, top5 **95.8%** | top1 **88.3%**, 金标在列 88.3% |

- 词级不受重排影响(门控保护, 见下); 25M 把"金标在列"的句子**全部**选对,
  触顶 88.3%, 代价是整句单次解码 39ms→162ms。
- 60 句中约 4 条金标与输入拼音本身不匹配(如输入 "limin" 金标 "李明"),
  任何解码器都无解, 88.3% 即这组数据的实际天花板。

## 用户词学习

打字板与全局模式默认开启(`Engine(learn=False)` 可关)。每次上屏记录
(文本, 当时打的拼音)到 `data/user_words.json`:词典里已有的词获得高频
加权, 词典里没有的自造词(人名、口头语)按当时按键存为新词条, 下次输入
同键时直接置顶。简拼上屏不学(拼音与文本对不上), 条目封顶 2 万。

例: 打 "limin" 选"李敏"三次后, "李敏"永久排第一(实测它原本排在词典
低频同音词条之后)。

注: 整句 60 条中约 4 条金标与输入拼音本身不匹配(如输入 "limin" 金标 "李明"),
任何解码器都无解, 88.3% 已接近这组数据的实际天花板。

单次解码延迟 ~0.4ms; 挂重排序后触发模型时 20-40ms(多数按键在门控处
 decline, 不进模型)。

## 神经重排序(ime/reranker.py)

按 chinese-ime-lm 的 `docs/format.md` 规范自实现加载与前向(safetensors 解析、
int8 行反量化、pre-LN 因果 Transformer、erf 精确 GELU、绑定词表), 按
`reference/src/lib.rs` 的 `Reranker::best_where` 语义移植决策: leader 可信或
不应答则拒判, 只在应答候选(≤9)里取逐字平均 log 概率最大者。

两个本引擎特有的适配判据:

- **候选单位是完整读法(preview)**, 不是首段——引擎候选按首段组织, 但模型
  打分的是整句; facts 映射按 preview 判定(首版按 commit_end 判导致重排
  从不触发)。
- **末音节补全也算词典应答**: 评测集含 "aizengfenmin"→爱憎分明 这类少打
  尾字母的输入, 补全边由词典生成、携带词频; 不保护的话字级模型会把
  418 个成语改坏(爱憎分明→爱增分明)。

配套的引擎改进是 **k-best 整句路径枚举**: 候选不再只变首段——前向 beam
搜索(60 条)枚举完整路径, 整句任意位置的次优读法(民/敏、在/再、书放/书房)
都按路径总分进列表; 首选路径出"首段"条目、其余路径出"双段上屏"变体(提交
点推进到第二边末尾, 保证读法兑现), 同首段最多 4 条防止首字变体刷屏。
金标在列率 75%→88.3%, 重排序再从中选出正确读法(他把书**放**在桌子上)。

## 架构

```
rime-ice 词表 YAML ──build_cache.py──▶ dicts/cache.pkl
                                        (拼音→词索引 / 音节→字索引 / 简拼索引)
                                              │
                                       ime/engine.py
                       词格建边(精确词/单字/末音节补全/简拼)
                       → 前向 Viterbi(最优整句)
                       → 后向 3-best DP(各位置最优后缀 + 变体)
                       → 候选 = 首段不同的路径 × 后缀变体
                       → ime/reranker.py 神经重排(4M 字级 Transformer, 可选)
                                              │
                              ime/app.py InputController(纯逻辑状态机)
                                              │
                                     ImeApp(Tk 候选窗/打字板)
```

### 打分公式(调参后的关键)

每条边的分 = `log(词权重) + word_bonus×(音节数-1) − char_penalty`,
投机边(末音节补全/简拼)不享受词加成, 另加罚分。默认
`word_bonus=2, char_penalty=24, partial_penalty=2, initial_penalty=6`。

**为什么是"每条边都罚"而不是"单字才罚"**: unigram 下 P(字1)×P(字2) 恒大于
P(词), 逐字路径天然占优; 若只罚单字边, "我+明天"会比"我命"(词表里真有这个词)
多付一次罚分而输掉。改为按分段数罚分后, 两路径罚分相同, 纯词频竞争——整句
top1 从 28% 提升到 72%。

**末音节补全规则**: 输入 "niha" 允许补全成 "ni hao"(你好), 但 "ba" 不允许
补全成 "ba ba er di"(巴巴儿地)——差距必须落在词最后一个音节内, 否则解码器会
凭空补出大量没打过的字(开发中实测过这个 bug, 整句准确率从 0.1% 修到 56%)。

## 局限与路线图

- **全局模式是钩子仿真, 非原生 TSF**: 没有 TSF 组合窗口与应用内联预览;
  成为真正的系统输入法需把引擎移植为原生 DLL(需 MSVC/Rust 工具链)。
- **整句天花板**: 金标在列率 88.3%(25M 模型已触顶), 剩余缺失是评测数据
  金标与拼音不匹配, 无解。
- **无用户词频衰减/语境学习**: 学习只有计数加权, 无时间衰减与上下文模型。
- **候选翻页**: 引擎每页 9 个候选, 暂无翻页。
- **重排序依赖**: numpy + scipy; 权重缺失或依赖不全时自动降级为纯词频引擎。

## 许可注意

- rime-ice 词表为 GFDL 许可, 本项目用于个人学习没有问题; 若再分发词库衍生品
  需遵守 GFDL。
- chinese-ime-lm 评测集源自 MIT 许可的微软 SampleIME 数据。
- 评测集中少数用例(如 "aiban→呆板" 按旧读音标注)与本词表注音不一致, 属于
  chinese-ime-lm 文档中已记录的测量陷阱, 会略微压低词级 top1 上限。
