<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 减少 Triton 无用特化

[English](triton-unused-specialization.md) | 简体中文

编译期参数即使已从优化后的 GPU 代码中消失，仍可能让 Triton 生成不同的 kernel 版本。当某个分支使布局参数不再参与计算时，可在调用处传入统一值，避免随张量元数据变化重复编译。

## 例子：打包序列的 batch stride

变长序列常被打包成外层 batch 维度为一的张量。外层步长随总 token 数变化，但 kernel 始终访问第零行：

```python
input_batch = 0 if IS_VARLEN else sequence_index
address = base + input_batch * stride_batch + token_offset
```

若 `stride_batch` 是 `tl.constexpr`，不同值仍可能触发编译，即使 `IS_VARLEN=True` 时乘零项最终会被优化器删除。调用处可以直接表达真实寻址需求：

```python
stride_batch = 0 if cu_seqlens is not None else tensor.stride(0)
```

定长 batch 仍须使用真实步长。token、head 和维度步长继续影响地址，不能因为张量形状相似就一并清零。

GDN 的 `fused_sigmoid_gating_delta_rule_update` 就存在这种 Q/K/V batch stride。对应 kernel 使用 `input_batch = 0 if IS_VARLEN else i_n`。应用前应阅读所用引擎版本的完整调用和 kernel，确认 wrapper 同时约束了变长输入的打包方式。

## 如何确认原因

从实际首请求延迟或重复编译出发，对照有关 kernel 的参数签名、编译选项和优化后的中间表示。源码文字相同还不够，调用参数和编译选项也可能改变生成结果。

在 MetaX C500、Triton 3.6 的 GDN 诊断中，总 token 数为 4、8、12 的变长打包输入分别生成了三个版本。这三个版本的优化后 TTIR 和 TTGIR 各自逐字节相同。调用处传入的 Q/K/V batch stride 不同，而 VARLEN 分支将它们与零相乘。仅将调用处的三个无用步长归零，变长版本数便从三个降为一个。另一个真实 batch size 为二的定长输入在两组中各保留一个版本，因此总数从四降为二。

两组分别使用新进程和独立空 Triton 缓存。四种输入的输出与完整可变状态页均通过 `torch.equal` 比较。这证明这些输入下的数值相等，不等同于位模式比较或穷尽所有状态转换。

## 在职责边界验证

只修改已证明对所选分支无用的参数。同时执行该分支和仍需要真实步长的分支，对照输出与完整可变状态。缓存统计按逻辑 kernel 和实际签名分类，不把每个文件都算作一个编译版本。

进入服务后，再分别观察首次请求延迟和暖态吞吐。减少编译版本可能改善准备或首次请求，而不改变暖态 kernel 执行速度；原始首次请求开销仍应保留在正式指标中。上述四到二的诊断验证了特化收敛，其进程总耗时不是编译节省或服务提速的测量。

实现入口位于 vLLM `third_party/flash_linear_attention/ops/fused_sigmoid_gating.py` 的 `fused_sigmoid_gating_delta_rule_update` 和 `fused_sigmoid_gating_delta_rule_update_kernel`。[Triton JIT API](https://triton-lang.org/main/python-api/generated/triton.jit.html) 还提供针对运行时参数的 `do_not_specialize`；它不会让一个多余的 `tl.constexpr` 值自动失去区分编译版本的作用。
