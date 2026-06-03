# 02 —— vLLM v0.22.0 KV-cache 钩子点地图

在 vLLM 中何处实现 KV 优化,基于 v0.22.0 源码(file:line)。**结论:MVP 和大部分愿景都是
树外插件 —— 只有两件事需要 core fork,且都已推迟。**

> **【2026-06 最新核实】** 本文原基于 v0.22.0 fork;已对照 clone 的**最新版 vLLM 源码**重新核实,
> 权威结论(含 file:line)统一收在 **`docs/08`(vLLM 扩展点全表)**。要点:
> - **KV 优化全程纯插件、零 fork**:`OffloadingManager`/`OffloadingSpec`/`CachePolicy`(均为 ABC,
>   经 `spec_module_path` 加载)、KV connector(`kv_connector_module_path`)、`scheduler_cls` —— 全部
>   确认为开放接口;
> - **GPU 块驱逐确认硬编码 LRU、无插件接缝**(要 core fork)→ MVP **故意把 value-aware 放 offload
>   层绕过**,不碰它;
> - **新 MTP 算法走 `custom_class` proposer**(不改源码,详见 `docs/05`)。
> 下面 v0.22.0 的机制分析(块管理 / APC / MoE 等)**大体仍成立**,作内部机制理解;**具体 file:line
> 以 `docs/08` 与届时源码为准**。

## 内部实现(事实)
- **块管理**:`KVCacheBlock`(`kv_cache_utils.py:116`)、`BlockPool`(`block_pool.py:130`)
  持有 `blocks`、`FreeKVCacheBlockQueue`(`kv_cache_utils.py:164` —— 侵入式双向链表,
  **= LRU** 受害者顺序),以及 `cached_block_hash_to_block`(APC 映射)。分配 `get_new_blocks`
  (`block_pool.py:305`)弹出 LRU + 可能驱逐;释放时块以逆序返回(尾部先被驱逐)。
- **前缀缓存(APC)**:块哈希 = parent_hash + 自身 tokens + extra_keys(`kv_cache_utils.py:541`);
  **块级(16 tok)、仅完整块**;查找 = 严格最长前缀,**首次未命中即 `break`**
  (`single_type_kv_cache_manager.py:495`)—— 这是非前缀复用的障碍。驱逐 = 经 free 队列的
  惰性 LRU(`_maybe_evict_cached_block` `block_pool.py:333`);**无价值/成本/复用信号**。
- **调度器**:`sched/scheduler.py:329`;KV 可用性把守 `allocate_slots`;抢占按 FCFS 弹出尾部
  的运行中请求(`:481`),恢复时全量重算。前缀命中只减少 `num_new_tokens` —— 没有"优先选
  高命中请求"的逻辑。
- **MoE wide-EP**:KV **按 DP rank** 分区 —— 每个 `DPEngineCoreProc`(`v1/engine/core.py:1651`,
  仅 MoE)拥有自己的 Scheduler+KVCacheManager+BlockPool(engine_id `_dp{rank}`)。EP 与 KV
  正交。**无全局 KV 视图。**
- **确定性**:已交付 —— `VLLM_BATCH_INVARIANT` + attention `num_splits=1`(`flash_attn.py:1048`)。
  与 SGLang 持平;**无需额外工作**。

## 扩展接口面(干净 —— 树外,不 fork)
| 接口面 | 如何加载 | 它控制什么 | 引用 |
|---|---|---|---|
| **`OffloadingManager` + `OffloadingSpec`** | `spec_module_path` | 卸载层**准入**(`prepare_store`,拒绝/过滤 key)、**驱逐**(自定义 `CachePolicy.evict`)、分层管理;`ReqContext` 携带按请求的信号 | `v1/kv_offload/base.py:111`;参考 `cpu/manager.py`(`store_threshold` 复用计数准入 `:145`)、`cpu/policies/base.py:35`(LRU/ARC) |
| **经 `scheduler_cls` 的 `Scheduler`** | `scheduler_config` 里的 qualname | 准入/排序、复用 vs 重算闸门 | `config/scheduler.py:127`;`sched/interface.py:36` |
| **KV connector** | `kv_connector_module_path`;`MultiConnector` 可组合 | `bind_gpu_block_pool` → GPU BlockPool 句柄(引用/驱逐/遍历);worker 端 `save_kv_layer` → 读写任意 KV slot;`get_num_new_matched_tokens` → 注入外部前缀命中 / 拒绝加载 | `kv_transfer/kv_connector/v1/base.py:171,432,354`;`factory.py` |

## 两处 CORE FORK(已推迟)
1. **价值感知的 GPU 层驱逐** —— 受害者块被硬编码到 `FreeKVCacheBlockQueue` 的近因顺序
   (`kv_cache_utils.py:164` + `block_pool.py:305-365`),**没有插件接缝**。→ MVP 让 GPU 保持
   LRU;所有价值感知都放在卸载层。
2. **token 级前缀缓存** —— 哈希/查找/分配端到端块量化(缺口 #4)。深度 fork。(token 级
   *复用*(而非缓存)可经 worker 端 blend 达成。)

## 唯一的硬性结构约束
scheduler↔connector 之间的外部命中通道是一个**标量的连续前缀 token 计数**
(`kv_cache_manager.py:341`)。⟹ **非前缀复用无法走调度器路径** —— 它必须是 worker 端按层的
`save_kv_layer` blend(PIECEWISE cudagraph;目前与 vLLM APC 互斥)。LMCache 已经把这个作为
一个 connector 交付(`lmcache_integration/vllm_v1_adapter.py:859`)。⟹ 生命周期(MVP)是
正确的*第一*目标(干净的卸载插件);非前缀是成本已知的后续阶段。

## 钩子点地图(每个缺口 → 接口面 → 结论)
| 缺口 | 接口面(file:line) | 结论 |
|---|---|---|
| (a)价值感知的长生命周期 | 自定义 `OffloadingManager.prepare_store`/`evict` + 自定义 `CachePolicy`;+ `Scheduler` 排序 | **干净插件**(卸载层)。GPU 层驱逐 = 推迟的 fork。 |
| (b)非前缀 / CacheBlend | worker 端 `save_kv_layer` blend(自定义 connector) | **干净 connector**(PIECEWISE cudagraph 成本;LMCache 已验证) |
| (c)复用 vs 重算成本模型 | connector `get_num_new_matched_tokens`(返回 0 即拒绝)+ 自定义 `Scheduler` | **干净**(成本决策必须幂等/无副作用) |
| (d)比块更细的复用 | `block_size` 旋钮(便宜)/ worker 端 slot 写入(复用) | 复用是**干净**的;token 级*缓存*则需 **FORK** |
| (e)MoE wide-EP / 跨 rank | connector + 共享内容哈希存储(无单一块管理器) | **干净 connector**;协调 N 个按 DP rank 的池 |

## 结论
**OffloadingManager/Spec + `scheduler_cls` + KV connector** 这三件套已经足够丰富,无需 fork
vLLM 核心就能构建业界最佳的 KV 管理。让 GPU 驱逐保持 LRU;把价值/成本/复用的智能放进卸载层
+ 一个自定义调度器;把两处 core fork 留到后续,隔离开,且仅当实测的卸载层天花板成为瓶颈时
才做。

*来源:file:line 引用已对照 `draft_router/vllm/` @ tag v0.22.0(上一阶段的 fork)核实。*
