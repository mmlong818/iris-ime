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

## 3. 评测数据(仅开发期使用, 不随运行时分发)

- eval/quanpin-words-v1.tsv 派生自微软 Windows classic samples SampleIME
  的全拼词库(MIT), 经 metasequoiaime/chinese-ime-lm 整理
- eval/sentences-v1.tsv 为 chinese-ime-lm 项目原创
- 来源: https://github.com/metasequoiaime/chinese-ime-lm
  (仓库代码 GPL-3.0, 评测数据文件为 MIT 语料, 模型权重 Apache-2.0)

## 4. Python 运行时与库

- Python + numpy / scipy / tkinter(PyInstaller 打包)
- pystray(MIT)、Pillow(HPND)、PyInstaller(GPL 引导程序例外条款,
  打包产物的许可不受影响)
