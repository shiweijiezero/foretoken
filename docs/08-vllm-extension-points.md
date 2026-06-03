# 08 —— vLLM 扩展点设计(留插件空间的套路 + 我们怎么用 / 怎么学)

基于 clone 的最新 vLLM 源码系统梳理(`file:line` 为证)。**双重用途**:
- **作为 guest**:我们的 KV / MTP 优化该挂哪个口子(全程不 fork,见 `docs/02`);
- **作为 host**:我们自己的项目(KV 策略框架 / 评测框架)将来给别人留扩展空间时,照抄 vLLM 这套。

> **核心哲学:分层开放 + 热路径敢封闭。** 顶层配置全开 → 中层接口(ABC)开放需实现契约 → 底层内核
> (CUDA/Triton 热路径)封闭不可换。优先级链:**用户代码 > OOT 插件 > 内置实现**。

## 一、扩展点全表(以下都能 OOT、不改源码)
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

## 二、6 种"留口子"模式(可直接抄)
1. **Factory + 双通道加载**:内置走"注册名"、外部走 `module_path` → OOT 覆盖内置、不改源码。例 `KVConnectorFactory`(`factory.py:96`)。
2. **窄 ABC 契约**:抽象基类定方法语义 + 调用时机,框架 `isinstance` 校验。例 `KVConnectorBase` / `OffloadingManager` / `CachePolicy`。
3. **Registry + decorator**:`@X.register("name")` 声明式注册、查重。例 `CustomOp.register`。
4. **entry_points 自动发现**:pip 装的包自动加载,完全解耦。例 LoRA resolver(`pyproject.toml:46`)。
5. **qualname 字符串延迟加载**:配置传类全限定名,`resolve_obj_by_qualname` 加载——延迟导入,避免 CUDA 污染 CPU-only。例 `scheduler_cls`。
6. **逃生舱(custom_class / CUSTOM 占位)**:没命名扩展点时,给个"传任意类路径"的通用口子。例 投机 `custom_class`、attention `CUSTOM=None` + `register_backend()`。

## 三、★ 封闭边界 + 为什么(最值得学的取舍)
**不是所有地方都留口子——性能热路径 / 核心正确性处故意封闭:**
| 封闭处 | 怎么封 | 为什么 | 逃生舱 |
|---|---|---|---|
| 投机 `method` | 封闭 Literal | 早抓拼写错 + 可枚举文档 + 热路径 | ✅ `custom_class` |
| GPU 块驱逐 | 硬编码 LRU、无 selector | 每 token 都调 `evict`,不能运行时 dispatch | ❌(改源码 / 走 offload 层) |
| rejection sampler | Triton kernel | 内联在采样热循环 | ❌ |
| attention backend | Enum | 类型安全 + 编译期常量折叠 | ✅ `register_backend` |
| platform | 单选、≥2 报错 | 线程/内存/通信平台特有、互斥 | ❌ |
| 量化方法 | 预定义 dict、无 register | 直接影响精度 + 每种对应专用 kernel | ⚠️ dict 可程序扩展 |

→ **取舍原则:冷路径开放(ABC + factory + 运行时 dispatch),热路径封闭(Literal + 编译期常量);
没人要扩展的就不留口子;`custom_class` 式逃生舱兜住 10% 长尾。**

## 四、可复用设计原则(我们自己留口子时抄这个)
1. **双通道**:OOT 路径优先 > 内置注册,允许用户打补丁、不改源码。
2. **ABC 契约 + qualname 延迟加载**:框架定接口、用户自带实现、框架不关心细节。
3. **entry_points 零侵入**:独立功能做成可 `pip install` 的 OOT 包。
4. **逃生舱**:Literal 枚举覆盖 90% 常见,留 `custom_class` 兜底 10%。
5. **延迟加载(`Callable` 闭包)**:热路径扩展按需导入,别让 CUDA 污染 CPU-only 用途。
6. **可测试**:接口都是 ABC → mock/patch 友好、插件独立测。
7. **★ 知道何时说"不"**:不该扩展的(热路径 / 正确性)就不给 registry —— **无 ABC/factory = "别扩展"**。

## 五、对 Foretoken 的具体含义
**作为 guest(用 vLLM,全程不 fork——见 `docs/02`)**:
- KV 策略 → KV 卸载 spec(factory + `spec_module_path`)+ `CachePolicy` ABC + `scheduler_cls`;
- 新 MTP 算法 → `custom_class` proposer(你 cross_vocab 走过这条);
- 量化 / attention / 模型 → 用现成,需要时按上表的口子接。

**作为 host(我们项目给别人留口子)**:KV 策略框架 / 评测框架将来想让别人插自定义策略 / benchmark,
直接抄这套:**ABC 定策略契约 + factory 双通道加载 + 配置驱动 + 留 `custom_class` 逃生舱 + 热路径
(命中判定)封闭**。

## 来源
基于 clone 的 vLLM(`C:\Users\weijie\Desktop\code\vllm`)最新源码系统梳理;`file:line` 见上表
(版本随上游演进,接口签名以届时 `--help` / 源码为准)。
