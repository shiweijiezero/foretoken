# ROADMAP

> 当前执行路线(2026-06 对齐)。相比 `docs/DESIGN.md` 的设计框架更新;`docs/DESIGN.md` 留作详细设计参考。

## 定位
基于 vLLM 的开源项目,目标分为三部分并列:① 主体是工业级真实推理(真实模型 + 真实业务负载、能服务);
② 在其上做优化(KV 管理、MTP 等是优化模块,KV 是其中最大的一块);③ 真实评测做裁判——以真实评测
判断优化的好坏。全程通过 vLLM 官方扩展点集成、不 fork;从单人维护起步,先聚焦 LM 长上下文 / reasoning。

## 工程原则
- 不 fork:只用 vLLM 官方扩展点(`OffloadingSpec`/`CachePolicy` / KV connector / `scheduler_cls` /
  `custom_class` / entry_points,见 `docs/08`);GPU 层驱逐这类无接缝接口的,用 offload 层绕过。
- 环境:cu128/cu129 + driver 550 即可,不升 driver、不上 cu13;紧跟一个 vLLM 版本 + 兼容矩阵。
- 单人维护:聚焦、最大复用现成组件、避免过度设计;跟进上游升级作为计划性维护任务。

## 阶段

### P0 · 真实推理基线(不依赖任何升级)
zxcpu 空卡 + `uv` 装 cu128 vLLM → GLM-4.5-Air-106B 起 OpenAI 兼容服务、确认出 token → 现成真实
benchmark(先 AIME/GPQA/LiveCodeBench,SWE-bench 后续)跑通 → 测真实基线 TTFT/TPOT/goodput。
门槛:稳定服务 + 一组基线数据。

### P1 · KV 管理 MVP(value-aware 卸载,纯插件)
自定义 `OffloadingSpec`/`OffloadingManager`/`CachePolicy`(`docs/06` 的 P1:token-type + S3FIFO,零模型
推理)+ `scheduler_cls` → 在真实场景负载上对照原生 LRU。门槛(`docs/04`/`docs/07`):受限容量
(<2×HBM)区间的 goodput / 每字节 goodput 胜过 LRU,无损校验通过。全插件、不 fork。

### P2 · MTP(用现成 + 自适应控制)
C1 用 GLM 内嵌 MTP(自测加载)→ C2 自适应 spec 长度控制(`scheduler` 插件 / 小 hook)→ 真实 benchmark
量接受率 / 加速。新 MTP 算法(若做)走 `custom_class`(`docs/05`)。

### P3+ · 后续
非前缀复用(CacheBlend)、跨 rank 共享、goodput 调度、EAGLE3 自训草稿头(可选研究)、多模态扩展
(VLM 图文 / VLA 动作 / omni 全模态 / diffusion 等,推理与评测负载均为远期)。

## 评测(核心支柱,贯穿全程)
评测是与推理、优化并列的第三件核心事,以真实评测判断优化的好坏(厂商倍数是营销上界,启发式策略
均自称最优,需以真实评测验证)。采用一套真实场景负载(会话重建缝合:Mooncake trace 提供真实时序/
并发/会话结构 + 真实多轮对话集提供内容 + 真模型现场生成)+ 4 对照配置(原生全关 / 只开 KV / 只开 MTP /
全开)→ 量 goodput / TTFT / TPOT /
显存 + 拆出 KV(命中率、每字节 goodput)和 MTP(接受率、加速)各自贡献 + 无损校验(输出 == 原生)+
对标真正的竞争者(LRU / T-LRU / SAECache / LMCache)与 PFOO 离线最优。每个优化模块在“完成”前都要过一个
对标基线的可证伪门槛(未达成即判定失败)。详见 `docs/04`(原则)+ `docs/07`(实操)。

## 当前状态
- 已完成:设计 / 文档梳理(定位、评测方案、不 fork 集成路径,已源码核实)、vLLM 扩展点设计(`docs/08`)。
- 已就绪:环境(最新 vLLM 已 clone);zxcpu A100 集群摸清(zxcpu2 常空可用)+ RAID 自动组装已加固。
- 待办:P0 起步(不被任何升级阻塞,空卡 + 用户态即可)。
