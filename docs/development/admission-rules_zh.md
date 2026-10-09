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

每个前端副本为各模型维护独立的规则实例和队列；路由变化时保留配置未变的规则。规则工厂负责校验和构造，不发布指标，也不影响正在处理的请求；框架在配置生效时启用指标。

模型规则更换时停止接纳新请求，取消等待中的准入调用，待已获准的工作结束后启用替代规则。交接期间新请求返回 HTTP 503，其他模型独立运行。
