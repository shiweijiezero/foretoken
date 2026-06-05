"""数据准备(data_prepare):缝合真实数据源,打包为可复现的评测数据集。

与评测台(bench/)分工:data_prepare 生成负载,bench 回放负载。单一真实源各缺一半(docs/07 §6.6):

- Mooncake trace 有真实并发 / 时序 / 会话结构(hash 前缀链),但无真实文本、无 session id;
- 真实多轮对话(kimi-mtp-dataset)有真实文本,但无并发 / 时序。

缝合方式:由 Mooncake 重建会话与时序结构,填入真实多轮对话内容,得到会话内累积复用(由内容决定、
配置无关,不复刻 Kimi 的 512 块量化)。

- stitch.py:reconstruct_sessions(hash 前缀链重建会话)+ fill_sessions(逐轮填入累积 prompt)。
- build_dataset.py:打包为 parquet + dataset card,供 load_dataset 直接复用(入口
  python -m foretoken.data_prepare.build_dataset)。

产出三字段:timestamp_ms / prompt / expected_output_len,供 bench/replay.py 回放。纯数据处理,
本地可运行可测试(运行 build_dataset 需 datasets 与 tiktoken)。
"""
