# 鸢尾输入法 (Iris IME)

![Release](https://img.shields.io/github/v/release/mmlong818/iris-ime)
![Tests](https://img.shields.io/github/actions/workflow/status/mmlong818/iris-ime/tests.yml?branch=main&label=tests)
![License](https://img.shields.io/github/license/mmlong818/iris-ime)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-lightgrey)

**鸢尾输入法**——一个纯 Python 的全拼输入法: 词格 DAG + Viterbi 整句解码 +
k-best 路径候选 + 神经重排序, 外加一个**全局键盘钩子客户端**(任意应用里打字)。
支持整句、未打完音节的预输入补全、简拼(声母串)、模糊音、用户词学习、拼音音节分隔显示。

## 功能一览

- **整句输入**: 词格 DAG + Viterbi, k-best 路径枚举让句中歧义(民/敏、在/再)直接进候选
- **神经重排序**: chinese-ime-lm 的 4M/25M 字级 Transformer(Apache-2.0), 纯 numpy 推理, 托盘热切换
- **模糊音**(默认开启): zh/z, ch/c, sh/s, 声母 n/l, ang/an, eng/en, ing/in
- **用户词学习**: 上屏即学, 自造词(人名、口头语)也能学会
- **预输入补全**: 打 niha 出"你好"; **简拼**: 打 zg 出"中国"
- **跟随光标**: 候选窗贴光标(经典 caret → MSAA → 鼠标兜底), 多屏与混缩放 DPI 支持, 副屏负坐标正确
- **水杉…不对, 鸢尾紫视觉**: 深色玻璃 + 药丸候选, 宽度自适应、超宽换行, 闲置状态条可拖动停靠

## 快速开始

```bash
# 1. 安装依赖
pip install numpy scipy comtypes

# 2. 编译词库缓存(约 20 秒, 产出 dicts/cache.pkl)
python build_cache.py

# 3. 启动
python -m ime.client      # 托盘客户端(推荐, 见下节)
python -m ime.app         # 打字板模式(自带输出区)
python -m ime.app --25m   # 同上, 用 25M 大模型(更准, 单次 ~160ms)
python -m ime.global_ime  # 全局模式(无托盘)

# 4. 测试与评测
python -m unittest discover -s tests
python tools/eval.py          # 默认参数评测(需本地克隆 chinese-ime-lm 的 eval/)
python tools/eval.py --grid   # 网格搜索调参
```

不想装 Python? 去 [Releases](../../releases) 下载免安装 zip, 双击
`IrisIME/IrisIME.exe` 即用。

模型权重已随仓库分发(`models/`); 若手动获取, 亦从
[Hugging Face](https://huggingface.co/metasequoiaime/pinyin-ime-reranker-4M)
下载。

## 按键(全局模式)

| 键 | 行为 |
|---|---|
| `a-z` | 输入编码 |
| `空格` | 上屏中文(整句最优) |
| `回车` | 字母原样上屏(经典 IME 惯例, 不附加换行; 需要发英文时用) |
| `1-9` / 点候选 | 选当前页候选, 逐段确认模式只上屏首段 |
| `PageUp/PageDown` 或 `-`/`=` | 候选翻页(全量枚举不限量, 每页 9 个) |
| `回车`(无编码) | 原生回车透传给应用(终端里即"发送") |
| `回车`(无法解码) | 字母原样上屏, 不附加回车 |
| `退格` / `Esc` | 删一个编码 / 清空编码 |
| `Shift 单击` | 切中/英(有大字提示) |

需要打纯英文时: Caps Lock 开启字母直通, 或 Shift 单击切到英模式。
闲置状态条可**按住拖动**停靠, 位置持久化; 组词中窗口跟随光标。

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
- 整句集 n=60 的统计分辨率约 ±13pp, 小幅总分差异不构成证据。
- 单次解码延迟 ~0.4ms; 挂重排后触发模型时 20-40ms(多数按键在门控处
  decline, 不进模型)。

## 架构

```
rime-ice 词表 YAML ──build_cache.py──▶ dicts/cache.pkl
                      (拼音→词索引 / 音节→字索引 / 简拼索引 / 模糊音规范形索引)
                                              │
                                       ime/engine.py
                 词格建边(精确词/单字/用户词/末音节补全/简拼/模糊音)
                 → 前向 Viterbi(最优整句) + k-best 路径枚举(beam=60)
                 → 候选 = 完整路径(首选出首段, 次选出双段上屏变体, 同首段限 4 条)
                 → ime/reranker.py 神经重排(字级 Transformer, 可选)
                                              │
                       ime/app.py InputController(纯逻辑状态机)
                                              │
                        ImeApp 打字板 / global_ime+client 全局钩子客户端
```

### 打分公式

每条边的分 = `log(词权重) + word_bonus×(音节数-1) − char_penalty`,
投机边(末音节补全/简拼)不享受词加成, 另加罚分; 模糊命中边再减
`fuzzy_penalty`。默认 `word_bonus=2, char_penalty=24, partial_penalty=2,
initial_penalty=6, fuzzy_penalty=4`。

**为什么是"每条边都罚"而不是"单字才罚"**: unigram 下 P(字1)×P(字2) 恒大于
P(词), 逐字路径天然占优; 若只罚单字边, "我+明天"会比"我命"(词表里真有这个词)
多付一次罚分而输掉。改为按分段数罚分后, 两路径罚分相同, 纯词频竞争——整句
top1 从 28% 提升到 72%。

**末音节补全规则**: 输入 "niha" 允许补全成 "ni hao"(你好), 但 "ba" 不允许
补全成 "ba ba er di"(巴巴儿地)——差距必须落在词最后一个音节内, 否则解码器会
凭空补出大量没打过的字(开发中实测过这个 bug, 整句准确率从 0.1% 修到 56%)。

## 神经重排序(ime/reranker.py)

按 chinese-ime-lm 的 `docs/format.md` 规范自实现加载与前向(safetensors 解析、
int8 行反量化、pre-LN 因果 Transformer、erf 精确 GELU、绑定词表), 按
`reference/src/lib.rs` 的 `Reranker::best_where` 语义移植决策: leader 可信或
不应答则拒判, 只在应答候选里取逐字平均 log 概率最大者。

两个本引擎特有的适配判据:

- **候选单位是完整读法(preview)**, 不是首段——引擎候选按首段组织, 但模型
  打分的是整句。
- **末音节补全也算词典应答**: 少打尾字母的输入(aizengfenmin→爱憎分明)的
  补全边携带词频, 不保护的话字级模型会把 418 个成语改坏。

## 模糊音(ime/engine.py + build_cache.py)

7 组对: zh/z, ch/c, sh/s, 声母 n/l, ang/an, eng/en, ing/in, 默认开启。

实现为**规范形双索引**: 词典侧每个词在规范拼音键下再挂一份
(`zansan` 同时挂赞赏/站上/展商), 输入侧把按键串按音节切分、逐音节映射到
规范形后交叉查询。精确命中永远优先, 模糊边带罚分(词级 top1 代价约 0.2pp)。
关闭: `Engine(fuzzy=False)`。

## 全局模式与客户端(ime/global_ime.py / ime/client.py)

托盘常驻 = 全局钩子输入 + 托盘菜单(开关/模型热切换/开机自启/学习记录/统计/退出)。

- **光标跟随定位链**: 经典 caret(GetGUIThreadInfo)→ MSAA 无障碍光标
  (OBJID_CARET; Chromium 系应用无障碍树默认休眠, 每次查询前先以
  OBJID_CLIENT 敲门激活)→ 跟随鼠标兜底; 光标贴屏底自动上翻。
- **多屏**: 按光标所在显示器工作区钳制与翻转, 副屏负坐标正确;
  进程声明 per-monitor DPI awareness。
- **原理与边界**: 低级键盘钩子(WH_KEYBOARD_LL, ctypes, 零编译依赖)拦截按键,
  主线程解码渲染, 选词后 SendInput 注入 Unicode 文本。这是"仿真输入法"——
  没有 TSF 组合窗口, 应用内联预览不会出现; 用前把系统输入法切到英文。
  注入事件一律放行避免自反馈; 钩子回调只入队不解码, 异常时立即禁用放行。
- **自检**: `python -m ime.global_ime --selftest` 注入合成按键验证
  钩子→解码→上屏链路(会先结束运行中的托盘实例, 并临时关闭 Caps Lock)。

## TSF 原生前端(v2.0 实验)

系统级输入法前端已可编译(`tsf/IrisIME/`,基于微软 SampleIME MIT 适配):
C++ TSF DLL 只做按键捕获/组合/候选窗摆放, 解码经命名管道
`\.\pipe\iris-ime` 交给 Python 引擎——引擎、重排、用户词与钩子模式
全量复用。托盘客户端启动时自动运行引擎服务。

尝鲜:
1. 启动托盘客户端(或 `python -m ime.tsf_server`)
2. 管理员 PowerShell 执行 `tsf/install-admin.ps1` 注册
3. Windows 设置 → 键盘 → 添加 **鸢尾输入法 Iris IME**, Win+Space 切换

## Windows 客户端打包

```bash
pyinstaller --noconfirm --name IrisIME --windowed --onedir --paths . \
  --add-data "models/sentence-model-4m.safetensors;models" \
  --add-data "models/sentence-model-25m.safetensors;models" \
  --add-data "dicts/cache.pkl;dicts" client_main.py
```

打包态用户词与崩溃日志写到 `%APPDATA%\pinyin-ime\`(开发态在项目 `data/`)。

## 中英文混排空格(ime/textstyle.py, 默认关闭)

"盘古之白"能力已实现但**默认关闭**——社区对"该不该加"没有共识。规则:
汉字↔英文字母加空格; 数字默认不加; 标点接缝不动。开启:
`InputController(auto_space=True)`。

## 局限与路线图

- **全局模式是钩子仿真, 非原生 TSF**: 成为系统输入法需把引擎移植为原生
  DLL(需 MSVC/Rust 工具链)。
- **无用户词频衰减/语境学习**: 学习只有计数加权。
- **重排序依赖**: numpy + scipy; 权重缺失或依赖不全时自动降级为纯词频引擎。
- **模糊音方向可选**: 目前七组对全开, 未提供逐组配置与托盘开关。

## 许可

代码 **MIT**; `dicts/*.yaml` 词表为 rime-ice 的 **GFDL 1.3**; 模型权重
**Apache-2.0**(语料署名: C4 ODC-BY / LCCC MIT)。详见
[LICENSE](LICENSE) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

评测集中少数用例(如 "aiban→呆板" 按旧读音标注)与本词表注音不一致, 属于
chinese-ime-lm 文档中已记录的测量陷阱, 会略微压低词级 top1 上限。
