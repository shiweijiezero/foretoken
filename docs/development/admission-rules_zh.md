<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 开发准入规则

[English](admission-rules.md) | 简体中文

新增规则可从 [allow_all](../../data-plane/frontend/src/admission/src/algorithm/allow_all.rs) 开始；需要排队和资源预留时，参考 [concurrency](../../data-plane/frontend/src/admission/src/algorithm/concurrency.rs)。使用现有规则见[前端准入配置](../../data-plane/frontend/README_zh.md#配置准入规则)。

## 实现准入决策

实现 `AdmissionRule::admit`：

```rust
async fn admit(
    &self,
    request: &AdmissionRequest,
    context: &AdmissionContext<'_>,
) -> Result<AdmissionPermit, AdmissionError>;
```

从 `request` 获取输入摘要和输出预算，从 `context` 获取截止时间及当前模型观测。字段说明见 [AdmissionRequest](../../data-plane/frontend/src/admission/src/request.rs) 和 [AdmissionContext](../../data-plane/frontend/src/admission/src/context.rs)。

接受请求时返回许可，拒绝时返回错误。规则可以等待，框架负责超时和调用方取消。排队期间持有 `context.queue.begin_wait()` 返回的 guard，以记录等待时长。

无需占用资源时返回 `AdmissionPermit::default()`；否则返回 `AdmissionPermit::new(reservation)`。reservation 应持有已取得的容量，在丢弃时释放，并通过 `split_one()` 将一个候选所需的份额交给批次子请求。

只有算法实际预留容量或进入队列时，才更新 `context.metrics.active` 和 `context.metrics.queued` 的工作量计数。随资源释放扣除相应计数；拆分只转移已计量的份额，不重复增加。调用结果和等待时长由框架独立记录。

## 注册规则

提供 `from_parameters(Value) -> Result<Self, String>`，校验参数并为模型构造规则。将其加入 [algorithm/mod.rs](../../data-plane/frontend/src/admission/src/algorithm/mod.rs) 的 `declare_admission_algorithms!` 列表；外部实现也可通过 `inventory` 注册 `AdmissionDescriptor`。

新增配置需同步 FrontendService 和 ModelService 共用的准入 API，并重新生成 CRD。可选方法 `capacity`、`requires_ready_runtime` 和 `close` 见 [AdmissionRule](../../data-plane/frontend/src/admission/src/lib.rs) 的接口说明。`close` 应唤醒算法自己的等待任务，不撤销已获准请求的资源预留。

## 配置生命周期

每个前端副本按模型维护独立的准入状态。准备阶段，`PreparedAdmissions::new` 调用各算法的工厂，校验参数并构造独立候选；工厂不能修改在线资源预留、等待任务或指标。候选无效时，已发布规则保持不变。运行时发布者接受准备好的配置后，才调用 `AdmissionRegistry::publish`。

内置的 `allow_all` 与 `concurrency` 通过 `AdmissionRule::capacity_state` 提供共享的工作量状态。发布时将候选限额写入原状态，保留原规则实例；计数、FIFO 等待和资源指标不重建。不限流的工作也返回计数许可，以便切回 `concurrency` 后仍计入在途工作。这个 hook 仅适用于决策完全由这些共享限额表达的规则，不是任意算法的替换接口。

降低并发上限不撤销已执行工作的许可，缩小或关闭队列只限制新入队，已有等待沿用原超时预算。原已排队批次若超过新并发上限，返回 `AdmissionError::Closed`（HTTP 503），而不是新请求的 `BatchTooLarge`（HTTP 400）；其队列预留释放后，后续等待才能继续推进。取消或超时也必须移除等待记录并释放相应计数。

新增规则若具有不同的准入决策，应保留 `capacity_state` 默认返回的 `None`。自定义规则更换时停止接收新请求，通过 `close` 取消等待任务，待已接收工作释放许可后启用替代规则；交接期间新请求返回 HTTP 503，其他模型独立运行。只要当前模型仍在等待旧工作结束或存在待启用规则，`AdmissionRegistry::is_applied` 就返回 false，配置确认以实际启用为准，而不是以候选准备成功为准。
