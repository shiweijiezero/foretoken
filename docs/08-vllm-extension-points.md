# 08 —— vLLM 扩展点设计

基于 clone 的最新 vLLM 源码系统梳理(`file:line` 为证)。双重用途:
- **作为 guest**:Foretoken 的 KV / MTP 优化挂哪个扩展点(全程不 fork,见 `docs/02`);
- **作为 host**:Foretoken 自身的项目(KV 策略框架 / 评测框架)给外部留扩展空间时,复用 vLLM 这套设计。

> 核心设计:分层开放,热路径封闭。顶层配置全开 → 中层接口(ABC)开放、需实现契约 → 底层内核
> (CUDA/Triton 热路径)封闭、不可替换。优先级链:用户代码 > OOT 插件 > 内置实现。

## 一、扩展点全表(以下均可 OOT、不改源码)
| 扩展点 | 加载机制 | ABC? | file:line |
|---|---|---|---|
| 插件系统 | entry_points(`vllm.general_plugins` 等) | 否 | `plugins/__init__.py:28` |
| 模型注册 | `ModelRegistry.register_model`(类 / `"module:class"`) | 是 | `models/registry.py:965` |
| KV connector | Factory + `kv_connector_module_path` / register | 是 | `kv_connector/factory.py:31` |
| KV 卸载 spec | Factory + `spec_module_path` / register | 是 | `kv_offload/factory.py:21` |
| 缓存策略 | `CachePolicy` ABC(LRU/ARC) | 是 | `kv_offload/cpu/policies/base.py:36` |
| 调度器 | `scheduler_cls`(qualname) | 是 | `config/scheduler.py:127` |
| 投机 proposer | `custom_class`(`model=类路径`) | 约定式 | `spec_decode/custom_class_proposer.py:12` |
| attention backend | Enum + `register_backend()`(`CUSTOM` 占位) | 是 | `attention/backends/registry.py:34` |
| custom op / layer | `@CustomOp.register` / `PluggableLayer.register` | 是 | `model_executor/custom_op.py:21` |
| platform(硬件) | `platform_plugins` entry-point(**单选**) | 是 | `platforms/__init__.py:203` |
| tokenizer | `TokenizerRegistry`(module, class) | 是 | `tokenizers/registry.py:55` |
| 多模态处理 | `MultiModalRegistry`(per-model 工厂链) | 是 | `multimodal/registry.py:98` |
| reasoning / tool parser | ABC + 配置指定类 | 是 | `reasoning/`、`tool_parsers/` |

## 二、6 种扩展点模式
1. **Factory + 双通道加载**:内置走"注册名"、外部走 `module_path`,OOT 覆盖内置、不改源码。例 `KVConnectorFactory`(`factory.py:96`)。
2. **窄 ABC 契约**:抽象基类定方法语义 + 调用时机,框架 `isinstance` 校验。例 `KVConnectorBase` / `OffloadingManager` / `CachePolicy`。
3. **Registry + decorator**:`@X.register("name")` 声明式注册、查重。例 `CustomOp.register`。
4. **entry_points 自动发现**:pip 安装的包自动加载,完全解耦。例 LoRA resolver(`pyproject.toml:46`)。
5. **qualname 字符串延迟加载**:配置传类全限定名,`resolve_obj_by_qualname` 加载;延迟导入,避免 CUDA 污染 CPU-only。例 `scheduler_cls`。
6. **逃生舱(custom_class / CUSTOM 占位)**:无命名扩展点时,提供"传任意类路径"的通用接口。例 投机 `custom_class`、attention `CUSTOM=None` + `register_backend()`。

## 三、封闭边界及其原因
并非所有位置都留扩展点;性能热路径 / 核心正确性处刻意封闭:
| 封闭处 | 怎么封 | 为什么 | 逃生舱 |
|---|---|---|---|
| 投机 `method` | 封闭 Literal | 早抓拼写错 + 可枚举文档 + 热路径 | ✅ `custom_class` |
| GPU 块驱逐 | 硬编码 LRU、无 selector | 每 token 都调 `evict`,不能运行时 dispatch | ❌(改源码 / 走 offload 层) |
| rejection sampler | Triton kernel | 内联在采样热循环 | ❌ |
| attention backend | Enum | 类型安全 + 编译期常量折叠 | ✅ `register_backend` |
| platform | 单选、≥2 报错 | 线程/内存/通信平台特有、互斥 | ❌ |
| 量化方法 | 预定义 dict、无 register | 直接影响精度 + 每种对应专用 kernel | dict 可程序扩展 |

取舍原则:冷路径开放(ABC + factory + 运行时 dispatch),热路径封闭(Literal + 编译期常量);
无扩展需求处不留扩展点;`custom_class` 式逃生舱覆盖剩余 10% 的长尾。

## 四、可复用设计原则
1. **双通道**:OOT 路径优先于内置注册,允许用户打补丁、不改源码。
2. **ABC 契约 + qualname 延迟加载**:框架定接口、用户自带实现、框架不关心细节。
3. **entry_points 零侵入**:独立功能做成可 `pip install` 的 OOT 包。
4. **逃生舱**:Literal 枚举覆盖 90% 常见情形,留 `custom_class` 兜底 10%。
5. **延迟加载(`Callable` 闭包)**:热路径扩展按需导入,避免 CUDA 污染 CPU-only 用途。
6. **可测试**:接口均为 ABC,mock/patch 友好、插件可独立测。
7. **明确不可扩展的边界**:不应扩展的位置(热路径 / 正确性)不提供 registry;无 ABC/factory 即表示不开放扩展。

## 五、对 Foretoken 的具体含义
**作为 guest(用 vLLM,全程不 fork,见 `docs/02`)**:
- KV 策略 → KV 卸载 spec(factory + `spec_module_path`)+ `CachePolicy` ABC + `scheduler_cls`;
- 新 MTP 算法 → `custom_class` proposer(cross_vocab 即走此路径);
- 量化 / attention / 模型 → 用现成实现,需要时按上表的扩展点接入。

**作为 host(Foretoken 给外部留扩展点)**:KV 策略框架 / 评测框架要支持外部插入自定义策略 / benchmark,
复用同一套设计:ABC 定策略契约 + factory 双通道加载 + 配置驱动 + 留 `custom_class` 逃生舱 + 热路径
(命中判定)封闭。

## 来源
基于 clone 的 vLLM(`C:\Users\weijie\Desktop\code\vllm`)最新源码系统梳理;`file:line` 见上表
(版本随上游演进,接口签名以届时 `--help` / 源码为准)。
