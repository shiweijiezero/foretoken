"""数据准备(data_prepare):缝合真实数据源,打包成可复现的评测数据集。

与评测台(bench/)分开:这里造负载,bench/ 用负载。一套全真负载的来源(单一真实源都缺一半,
见 docs/07 §6.6):

- Mooncake trace 有真实并发 / 时序 / 会话结构(hash 前缀链),但无真实文本、无 session id;
- 真实多轮对话(kimi-mtp-dataset)有真实文本,但无并发 / 时序。

缝合 = Mooncake 的会话骨架 + 真实多轮对话的血肉(会话内累积复用,配置无关,不复刻 Kimi 的
512 块量化)。

- stitch.py:reconstruct_sessions(hash 前缀链重建会话)+ fill_sessions(逐轮塞累积 prompt)。
- build_dataset.py:打包为 parquet + dataset card,供 load_dataset 直接复用(入口
  scripts/build_dataset.sh)。

产出三字段:timestamp_ms / prompt / expected_output_len → 配 bench/replay.py 回放。纯数据处理,
本地可跑可测(真跑 build_dataset 需 datasets + transformers + GLM tokenizer)。
"""
