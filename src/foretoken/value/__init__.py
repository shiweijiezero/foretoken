"""B8 P(reuse) 长视野估计器 + 价值函数(最难的核心,docs/03 / 06)。

P(reuse):因子化 流行度 x 生存(log-normal),按 token 类型加权;竞争安全 = 与 S3FIFO/LRU
混合,最坏情况有界(绝不盲信估计器)。价值函数规范式见 offloading 模块。

TODO(P1):token-type MVP(零模型推理)→ P2 因子化 + 不确定性。纯函数为主,放 tests/unit 单测。
"""
