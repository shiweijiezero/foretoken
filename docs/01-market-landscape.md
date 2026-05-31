# 01 — Market landscape (mid-2026)

Synthesis of a 3-front survey: inference **engine** layer, **orchestration/control-plane**
layer, and **distributed long-lived KV-cache** systems. Conclusion: don't build an engine or
a control plane or a transfer fabric; build the **KV policy/optimization brain** inside one
engine (vLLM). All claims source-grounded; vendor throughput multipliers are upper bounds.

## A. Engine layer — converged, commodity → build ON, don't reinvent
| engine | role | KV reuse | extensibility | note |
|---|---|---|---|---|
| **vLLM** | de-facto substrate (PyTorch Foundation, Apache-2.0) | **block-level APC, prefix-only, LRU** | best (plugins, KV-connector, `LLMEngine`) | the one to build on |
| **SGLang** | co-standard | **token-level RadixAttention + HiCache** (ahead) | fork-to-extend; commercial (RadixArk) | KV-reuse frontier + determinism |
| **TRT-LLM** | NVIDIA perf ceiling | paged + reuse | PyTorch backend default (v1.0) | vendor-locked |
| **TGI** | — | — | — | **archived 2026-03** (license cautionary tale) |
| gpt-fast / llama.cpp | reference / edge | — | — | steal patterns, don't build on |

Converged "standard stack": paged KV + prefix/radix caching · continuous batching · unified
chunked-prefill scheduler · CPU-overhead-isolating multiprocess loop · torch.compile + CUDA
graphs · P/D disaggregation + wide-EP for MoE. **vLLM = the library others build on** (llm-d,
KServe, Ray Serve all wrap it). → reinventing an engine is negative-EV.

## B. Control-plane / orchestration layer — crowded land grab → DON'T build #5
| system | owner | converged feature set | license |
|---|---|---|---|
| **Dynamo** | NVIDIA | KV-aware router + Planner autoscaler + KVBM tiering + NIXL | Apache-2.0 |
| **llm-d** | Red Hat+Google+IBM+CoreWeave | IGW routing + P/D + KV-aware (CNCF Sandbox) | Apache-2.0 |
| **AIBrix** | ByteDance | LLM-aware gateway + KV-util autoscaler + distributed KV pool | Apache-2.0 |
| **KServe** | CNCF | K8s substrate; delegates LLM routing to llm-d | Apache-2.0 |

All Apache-2.0, multi-engine, converged on (KV-aware router + P/D disaggregation + SLO
autoscaler). The smart-routing **interface** is standardizing (Kubernetes **Gateway API
Inference Extension** / Endpoint Picker). → conform to the standard; do not build a 5th control plane.

## C. KV mechanics — mature & converged → MOUNT, don't rebuild
- Per-node: PagedAttention (vLLM), RadixAttention (SGLang). 3–4-tier offload GPU→DRAM→NVMe→
  remote: **LMCache, SGLang HiCache, Dynamo KVBM, AIBrix**. **PCIe wall**: GPU↔CPU ~32–63 GB/s
  vs HBM ~3–8 TB/s = ~50–100× cliff governs every reuse-vs-recompute decision.
- Distributed: KV-aware routing via KV-events (Dynamo / llm-d approximate+precise / AIBrix);
  transfer fabrics **NIXL** (NVIDIA, emerging standard) + **Mooncake Transfer Engine** (battle-
  tested, Kimi). P/D disaggregation standard since 2025 (DistServe goodput, Mooncake FAST'25).
- MoE: **attention-DP + wide-EP** — KV is attention-only (experts add none); KV partitioned per
  DP rank. MLA compresses KV ~93% (DeepSeek). Long-context KV is the memory wall (70B @ 1M tok →
  ~135 GB KV > weights).

## D. The genuine gaps (un-won) — where we play
The KV *mechanics* are solved; the *policies* governing them are disjoint + heuristic (recency).
1. **Value-aware, long-lived (hours/days) cache lifecycle** — everyone evicts by LRU/LFU/S3FIFO;
   **nobody** admits/evicts/TTLs by `P(reuse)×recompute_cost×SLO_value`. Strongest differentiator;
   = the long-lived-reuse target. *(our MVP)*
2. **Cache-affinity ↔ load-balance** as one joint online optimum (DualMap/GORGO show it's open).
3. **Non-prefix / partial reuse** by default (CacheBlend/CacheGen prove it; only LMCache exposes it).
4. **Online reuse-vs-recompute** cost model per block/tier vs the PCIe↔HBM cliff.
5. **Coherent multi-engine global KV namespace** — *dropped* (we are single-engine vLLM).
6. MoE wide-EP KV co-scheduling; cross-region reuse; hot-block replication (Mooncake: >50% blocks
   never reused, a few hit 10,000s → replication mandatory).

**Moat** = the policy brain (goodput-per-byte) over KV, not another transfer engine/tier.

## E. Decisions this drove
- Single engine = **vLLM** (governance/ecosystem + KV-reuse headroom).
- Build = out-of-tree plugins (see `02-vllm-kv-hookpoints.md`); mount NIXL/Mooncake/LMCache.
- MVP = value-aware long-lived lifecycle (gap #1).

## Sources (representative)
Engines: vLLM V1 blog + PyTorch-Foundation post; SGLang RadixAttention/HiCache (LMSYS) + RadixArk;
TRT-LLM v1.0 PyTorch backend; TGI archival. Control plane: Dynamo (NVIDIA), llm-d (Red Hat), AIBrix
(arXiv 2504.03648), KServe (CNCF), Gateway API Inference Extension. KV: Mooncake (FAST'25, arXiv
2407.00079), DistServe (OSDI'24, 2401.09670), CacheBlend (EuroSys'25, 2405.16444), LMCache (arXiv
2510.09665), DualMap (2602.06502), GORGO (2602.11688), KVFlow (2507.07400), MLA/Wide-EP (vLLM blog
2025-12-17). *(Full per-claim citations in the underlying survey reports.)*
