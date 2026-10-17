<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 平衡加载速度与内存：Mmap、固定页暂存和并行 I/O

[English](loading-io-pipeline.md) | 简体中文

## 现象与动机

延迟映射、整块读取和固定页暂存解决不同问题。mmap 省去初始全文件拷贝，却可能触发很多小缺页；eager 改变访问形态和内存占用；固定页主机缓冲区支持异步设备搬运，但既不加快文件系统，也不自动产生重叠。

## 原理与分析

lazy safetensors reader 映射文件页，在请求权重时触碰相应范围。热本地文件系统的页可能已在 RAM；网络存储上不规则张量访问则可能变成昂贵缺页和小读取。最终仍需将张量复制到设备，mmap 不是存储直接到 GPU 的搬运。

eager 先读完整 shard，再逐个提供张量，访问更顺序，但会实体化更大的 CPU state dictionary。并行读 shard 增加在途 I/O，也增加驻留主机内存。八线程不代表八倍带宽，尤其各 rank 争用同一磁盘或挂载时。

固定页暂存使用可复用缓冲区：读块、放入 pinned memory、排入 H2D，等对应拷贝完成才归还。占用约为“缓冲区数量 × 大小”。传输速率 B、阶段延迟 t 时，维持不饥饿约需 `B × t` 在途字节，这只是容量估计，不是通用默认值。固定太多内存会减少可回收 RAM，影响同机任务。

## 设计

建立有界的存储→主机→设备流水。存储需要在途 I/O 时采用顺序或并行读取，H2D 需要异步暂存时使用可复用固定页池。池的 owner 跟踪拷贝完成后才复用；reader 若循环使用内部存储，返回张量必须具有独立所有权。

页缓存预取与固定页暂存位于不同层。自动预取可将网络文件读入可回收 RAM，固定页缓冲区服务设备拷贝。GDS 只有在文件系统、驱动与 loader 兼容时才减少部分主机暂存。KV 卸载与注册内存会争用同一批主机资源。

## 实现

引用版本的 `weight_utils.py` 与 `DefaultModelLoader` 提供不同 reader：

1. Lazy 打开 safetensors 映射，按需返回张量范围；eager 先实体化一个 shard 的 CPU 张量再返回。
2. Prefetch 在张量迭代前将文件页读入页缓存；自动选择识别特定网络文件系统，并考虑可用 RAM。
3. 独立多线程迭代器并发读取 shard；`DefaultModelLoader` 拒绝将其与非默认 safetensors 策略组合。
4. 模型安装代码消费各张量，执行切片／布局转换并复制到设备参数。

固定页池流水则取空闲缓冲区、读取块、提交 H2D 并关联完成事件，事件完成后才归还缓冲区。RunAI 将并发与内存上限交给外部 streamer。InstantTensor 返回自有副本，以免 loader 复用存储导致失效；fastsafetensors 有 GDS 与非 GDS 路径。

## 影响与效果

Lazy 避免立即实体化整个 shard，eager 可用顺序读取替代不规则网络缺页。并行 I/O 增加在途请求，也增加驻留主机内存，不能突破共享存储带宽。固定页暂存可以让搬运与其他工作重叠，代价是不可回收 RAM。

这些选择解决不同瓶颈：增加存储线程不能消除 pageable 到 pinned 的准备，固定页缓冲区不能加速饱和文件系统。按完成顺序复用既约束内存，也避免破坏在途拷贝。

## 参考

- [vLLM weight utilities](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/weight_utils.py)
- [DefaultModelLoader](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/default_loader.py)
- [RunAI loader](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/model_loader/runai_streamer_loader.py)
- [ServerlessLLM 原方法](https://arxiv.org/html/2401.14351v1)
