"""F1 遥测面 —— KV / goodput 指标(docs/00 模块 F1,docs/10)。

扩展 vLLM Prometheus + 我们的:命中率 vs 容量、reuse-distance 分桶、每 GPU 字节秒 goodput、
(MTP)接受率 / 加速。

TODO(P0/P2):采集 + 导出(先从 vllm bench serve 的 --goodput 结果起步,见 scripts)。
"""
