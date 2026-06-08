"""评测台(bench):闭环回放真实会话负载,采集 TTFT/TPOT、算吞吐与 goodput,出可复现记录。

- `replay.py`:进程内自起 vLLM 引擎(`AsyncLLM`),按真实 timestamp 异步回放 `data_prepare/` 缝合的会话
  负载(现场生成回复拼下一轮),采每轮 TTFT/TPOT/输出 token;会话级下采样匹配单实例硬件,墙钟时限掐长尾。
- `report.py`:聚合(延迟分位 / 原始吞吐 / goodput SLO 阶梯)+ 人读产物(`summary.md` / 图 / `INDEX`)。

采样 + 引擎配置加载见顶层 `foretoken.config`(读 `config/models/*.toml`)。

自建闭环回放(而非 `vllm bench serve`)的原因:vLLM 的 `timed_trace` 仅接受 hash、`custom` 不含
timestamp,均无法同时承载真实 prompt 与真实到达时刻;且评测需闭环(下一轮 prompt 用模型现场回复)。
"""

from __future__ import annotations
