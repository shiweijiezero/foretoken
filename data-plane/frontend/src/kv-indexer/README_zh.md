<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV 前缀索引

## 它解决什么问题

大模型处理 prompt 时会生成 KV cache。后续请求如果包含相同的 prompt 前缀，就可以复用已有缓存，减少重复计算。

KV 前缀索引像一份缓存目录：它记录“哪个模型实例保存了哪些 prompt 前缀”。KV cache 本身仍由推理后端保存，索引只提供 Router 查询路由所需的信息，不负责保存、传输缓存或选择路由。

## 一个路由示例

假设一个请求有 1,000 个 prompt token：

- 路由目标 A 已缓存前 800 个 token；
- 路由目标 B 已缓存前 200 个 token。

索引会返回这两个匹配结果。Router 可以优先考虑 A，也可以根据负载和其他路由条件选择 B。

## 在 Foretoken 中如何工作

```text
推理后端 ──KV block 事件──> KV 前缀索引 ──前缀查询结果──> Router
```

- 推理后端通过 adapter 将自己的 KV cache 事件转换为 Foretoken 通用事件。
- KV 前缀索引分别维护每个事件源、路由目标和 DP rank 的缓存目录。
- Router 通过 `KvPrefixIndexer` 查询缓存命中情况，再结合负载等信息选择路由。

### KV block 事件

Foretoken 使用三类通用事件维护索引：

- `BlockStored`：一个新的 KV block 已保存；
- `BlockRemoved`：一个 KV block 已删除；
- `AllBlocksCleared`：一个事件源和 DP rank 的 KV block 已全部清空。

### 缓存位置

| 位置 | 含义 |
| --- | --- |
| `Device` | 当前计算设备上的缓存，可以直接使用 |
| `HostPinned` | 主机内存中的缓存，需要恢复到计算设备 |
| `Disk` | 本地磁盘中的缓存，需要从磁盘恢复 |
| `External` | 远端或共享存储中的缓存，需要通过网络传输 |

只有当目标路由具备相应的恢复或传输能力时，索引才会返回 `HostPinned`、`Disk` 或 `External` 缓存。

### 事件同步

每个事件源使用独立的 epoch 和连续序号。出现事件缺失、乱序或 epoch 变化时，索引会暂时返回 `Unavailable`，而不是使用可能不完整的数据。同步恢复后即可继续查询。

## 查询接口

一次 `KvPrefixLookup` 包含：

- `route_target_id`：要查询的路由目标；
- `data_parallel_rank`：目标中的具体 DP rank；
- `prompt_token_ids`：请求的 prompt token。

查询返回以下结果之一：

- `Matches`：查询成功。结果包含可复用的 prompt token 数量和缓存位置；没有命中也是一个正常结果。
- `Unavailable`：当前无法可靠查询，例如事件源尚未同步完成或请求不支持前缀查询。它不等于“没有缓存”。

## 接入新的推理后端

推理后端的专用逻辑应放在 adapter 中。adapter 负责把后端的事件、block 标识和存储类型转换为 Foretoken 通用类型；`KvPrefixIndexer` 本身不依赖某个特定推理后端。

接入新的推理后端时，应新增或扩展对应 adapter，而不是在索引实现中加入后端特例。

## 选择索引实现

该 crate 提供：

- `PositionalHashIndex`：按 prompt 位置匹配 block；
- `RadixTreeIndex`：使用压缩前缀树查找匹配前缀；
- `NoopKvPrefixIndexer`：关闭 KV 前缀查询时返回 `Unavailable`。

它们都实现 `KvPrefixIndexer`，Router 不需要了解具体使用哪种索引结构。
