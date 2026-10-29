# 第三方组件与许可声明

本产品(拼音输入法 v0.9.0)包含以下第三方数据与模型, respective 许可与署名
义务随分发传播。再分发本产品时**必须保留本文件**。

## 1. 重排序模型 — pinyin-ime-reranker-4M / 25M

- 来源: https://huggingface.co/metasequoiaime/pinyin-ime-reranker-4M
        https://huggingface.co/metasequoiaime/pinyin-ime-reranker-25M
- 许可: Apache License 2.0(见 models/ 内随权重分发的 LICENSE 与 NOTICE)

模型权重的语料署名(依许可要求随衍生成果传播, 亦写在权重文件
safetensors `__metadata__` 的 `attribution` 字段中):

- C4 中文部分(ODC-BY) — https://huggingface.co/datasets/allenai/c4
- LCCC(MIT) — https://github.com/thu-coai/CDial-GPT

## 2. 词库 — rime-ice(部分)

- 来源: https://github.com/gaboolic/rime-ice (cn_dicts/8105.dict.yaml、cn_dicts/base.dict.yaml)
- 许可: **GNU Free Documentation License 1.3**
- 本产品将上述词表转换为二进制缓存(dicts/cache.pkl)随包分发。
  依 GFDL 义务, 再分发时须保证词库部分(含本声明中对其来源与许可的说明)
  以相同许可提供; 个人使用无额外要求。
- 字频数据: 《25亿字语料汉字字频表》(北京语言大学), 经 rime-ice 引入。

## 3. 评测数据(仅开发期使用, 不随运行时分发, 亦不含于本仓库)

- eval/quanpin-words-v1.tsv(词级 25,119 条): 固化自微软
  Windows-classic-samples 仓库 SampleIME 示例全拼词库(**MIT**),
  经 metasequoiaime/chinese-ime-lm 工具生成(丢弃单字与截断条目)。
- eval/sentences-v1.tsv(整句 60 条): chinese-ime-lm 项目**手写原创**;
  该文件无单独许可声明, 其所在仓库整体为 **GPL-3.0**, 故按 GPL-3.0 对待。
- 两者仅用于开发期本地评测, 本仓库与本产品均不再分发其副本;
  来源: https://github.com/metasequoiaime/chinese-ime-lm

## 4. 重排序模型的训练语料(署名义务随权重传播)

模型权重训练使用了以下语料(亦写在权重文件 safetensors `__metadata__`
的 `attribution` 字段中):

- C4 中文部分(ODC-BY) — https://huggingface.co/datasets/allenai/c4
- LCCC(MIT) — https://github.com/thu-coai/CDial-GPT

## 5. Python 运行时与库

- Python + numpy / scipy / tkinter(PyInstaller 打包)
- pystray(MIT)、Pillow(HPND)、PyInstaller(GPL 引导程序例外条款,
  打包产物的许可不受影响)

## 6. TSF 参考实现 — 微软 SampleIME

- 来源: microsoft/Windows-classic-samples 仓库 Samples/IME/cpp/SampleIME(MIT)
- 鸢尾的原生 TSF 前端(tsf/ 目录)在其基础上适配, 微软版权声明与 MIT 许可
  随 tsf/SampleIME/LICENSE 分发。
