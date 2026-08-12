<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

Router 为每个执行阶段选择一个可路由的 ModelGroup 和精确 DP rank。

Filter–Scorer–Picker 是候选列表级接口：

- **Filter** 接收兼容且健康的候选快照。它可以移除候选，但不能新增或修改候选。
- **Scorer** 为每个保留候选返回一个 `ScoredCandidate`。内置 KV 评分比较 prompt 命中长度、存储层级、locality 和负载。
- **Picker** 从评分列表中原样返回一个候选。Router 随后输出 `RouteDecision`，其中包含 ModelGroup RouteTarget、执行角色、模型 revision 和精确 DP rank。

`data_parallel_size: 1` 的 RouteTarget 只产生 rank `0` 候选，最终决策仍显式返回 `data_parallel_rank: 0`。更大的 RouteTarget 会为每个 rank 产生一个候选。

## 示例

```text
ModelGroups：
  llama3-serve-r-2gosa7pa2jpf2-0  UID 2f48f8e1-7f89-4eb8-bf31-e6d482504f66
  llama3-serve-r-2gosa7pa2jpf2-1  UID 8c88ee9a-c10f-41fd-98ef-a09d256b5213

候选：
  2f48f8e1-7f89-4eb8-bf31-e6d482504f66 / rank 0  KV 命中：  0 tokens
  2f48f8e1-7f89-4eb8-bf31-e6d482504f66 / rank 1  KV 命中：512 tokens
  8c88ee9a-c10f-41fd-98ef-a09d256b5213 / rank 0  KV 命中：256 tokens

Filter：保留三个健康且兼容的候选
Scorer：第一个 Group/rank 0 → 0，第一个 Group/rank 1 → 512，第二个 Group/rank 0 → 256
Picker：选择第一个 Group 的 rank 1

RouteDecision：
  route_target_id: 2f48f8e1-7f89-4eb8-bf31-e6d482504f66
  data_parallel_rank: 1
```

ModelGroup 名称遵循 `<pool-name>-<revision>-<ordinal>`。Router 使用 Kubernetes ModelGroup UID 作为路由身份，而不是使用 `metadata.name`；Deployment、Service 和 Service DNS endpoint 使用 ModelGroup 名称。

Aggregate 和 Prefill 的内置 KV 评分按以下顺序做字典序比较：完整 prompt prefix 命中长度、`Device > HostPinned > Disk > External`、`Local > Remote`，最后比较负载。Decode 的 prefix、tier 和 locality 分数为零。Unavailable KV facts 不会被当作确认 miss。
