<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按张量生命周期共享图内存池，保留跨步状态

[English](graph-lifetime-pooling.md) | 简体中文

## 现象与动机

不同捕获分别保留缓冲区，会减少可用于 KV 的内存；不跟踪存活张量就共享缓冲区，又可能覆盖其他分支或后续模型步仍需的输出。分配器既要保留捕获地址，又要回收已无消费者的数据。

## 原理与分析

CUDA Graph 重放使用捕获时的指针。地址中的值可以变化，移动分配却不会更新图中指针。重放只提交内核，也不会重复普通 allocator 操作。

考虑 `A → B → D` 与 `A → C → D`。A 重放后，其输出仍存活；即使重放没有更新分配账目，记录 C 时也必须保护它。B、C 输出地址不同，D 还可能需要第二份记录。因此形状、路径与存活时间共同决定能否复用。

## 设计

PyTorch CUDA Graph Trees 在兼容记录之间共享图内存池，跟踪存活输出及路径边界的分配状态。进入新分支前恢复这些账目，再分配内存；已有分支只在地址和生命周期条件匹配时重放。

参数、已有图输出与普通 eager 输入的处理不同。Eager 输入可能先复制到固定暂存地址；修改这份副本不会同步修改原张量，因此修改 eager 输入的函数会跳过捕获。

## 实现

内存池的核心流程为：

```text
进入下一个编译区域：
    确定形状、执行路径和存活输入地址
    若存在兼容记录：
        将需要固定地址的输入复制到暂存区
        重放记录
    否则：
        恢复保护前驱存活输出的分配账目
        预热并捕获新区域
    输出存储保留到其消费者释放为止
```

参数／buffer 与已有图输出可在稳定地址检查后支持修改。跨迭代输出需要明确生命周期：PyTorch 提供 step marker 和复制方式，v2.14 还提供可选的用户可见输出存储复制。持久循环状态和 KV 仍由引擎持有，不作为普通临时输出回收。

## 影响与效果

共享内存池减少不同捕获之间的重复存储，为请求状态留下更多空间。存活跟踪避免记录新分支时覆盖尚在使用的前驱输出。

代价是额外管理，以及为不稳定输入或跨步保留输出进行复制。形状和分支增多仍可能增加记录数。每 token 复制完整循环状态虽然能避开某些复用错误，却会增加大量访存，因此状态归属直接影响最终成本。

## 参考

- [PyTorch v2.14 CUDA Graph Trees](https://github.com/pytorch/pytorch/blob/v2.14.0/docs/source/user_guide/torch_compiler/torch.compiler_cudagraph_trees.md)：池状态恢复、mutation 和输出生命周期。
- [vLLM v0.31.0 CUDA Graph 设计](https://docs.vllm.ai/en/v0.31.0/design/cuda_graphs/)：引擎 wrapper 与分派。
- [图分桶](launch-graph-buckets_zh.md)和[注意力状态](../attention-state/)：捕获覆盖与模型持久状态。
