# Foretoken

> Industrial LLM inference on vLLM, with plugin-based optimization and real-workload evaluation.

基于 vLLM 的推理优化项目:在工业级真实推理之上,以树外插件做各项优化(KV 管理、MTP 等),并用真实业务负载检验每一项优化的好坏。

## 评测数据准备

把真实生产时序与真实多轮对话缝合为可复现的评测负载,已发布为公开数据集 [`weijiezz/foretoken-trace`](https://huggingface.co/datasets/weijiezz/foretoken-trace):时序、并发与多轮会话结构取自 Mooncake 生产 trace,对话内容取自真实多轮对话并在会话内逐轮累积,每行 `{timestamp_ms, prompt}`。三个 split 各保留各自约 1 小时的真实时序:`conversation`(约 11k 请求)、`mooncake` / `toolagent`(各约 20k 请求;后两者内容同源、到达时序不同)。

数据准备命令:
```bash
pip install -e '.[dev]'
bash scripts/build_dataset.sh 
```

## License

[Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0)。
