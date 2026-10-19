<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Runtime configuration ownership

English | [简体中文](runtime-configuration_zh.md)

Use this guide when changing a default, a derived runtime value, or a listener used by more than one component. Deployment configuration is described in [inference parameters](../features/inference-parameters.md) and [model storage](../features/model-storage.md).

## Resolve once, then pass the result

For example, `kvCache.offload.cpuCacheSize: 0.5Gi` is user intent. The model compiler parses it as a Kubernetes quantity, and one shared conversion validates its exact `536870912`-byte capacity. Pools retain their existing quantity wire format through the Kubernetes codec. Group projection consumes the parsed quantity rather than parsing a string again; engine launch receives the byte count.

The same boundary applies to model identity and runtime paths. The compiler resolves omitted tokenizer and source revisions. Backend adapters compile their topology and native arguments, while Group projection and frontend discovery use the normalized identity. The workload projection selects the model root; the runtime retains that path instead of reconstructing a directory convention.

| Responsibility | Owner | Consumers |
| --- | --- | --- |
| Installation defaults and shared dependency bindings | Platform Chart and installer | Namespace controller configuration |
| Model identity, user quantities and Pool overrides | Model compiler | Pool resolution and frontend catalog |
| P/D connector and protocol intent | Model compiler | Engine-specific transport resolution |
| EC and external Store profile validation | Shared runtime configuration | Controller startup and Group resolution |
| Model startup and drain budget conversion | Shared lifecycle compiler | vLLM and Omni launch plans |
| Mounted model/cache locations | Workload projection | Preparation, serving and cache recovery |
| Native execution topology and arguments | Inference-engine adapter | The selected engine process |

A consumer may validate the contract it receives, but should not invent another default or repeat a domain conversion. Keep semantically different budgets separate: a whole-second Kubernetes field and a rounded engine startup budget have different requirements even when both are expressed in seconds.

## Keep port selection with its network scope

Changing a model's HTTP port must update its container listener, Service, probes, access rules and published address together. These consumers use the resolved runtime port or its Kubernetes named port. Controller metrics/health and application-file delivery have their own Chart-local definitions; sharing the number `8080` does not make them one service.

Model-server resolves its private execution ports as one layout before launching the engine. It accounts for the declared HTTP and cache-observation listeners, plus the bindings of enabled P/D and ModelExpress transports. It then chooses non-overlapping handshake, distributed rendezvous, calibration and KV observation ranges. A data-parallel endpoint reserves the entire rank range, not just its base port.

Members with the same execution configuration derive the same layout. Engine arguments, event subscribers, lookup clients and calibration workers consume that result. This coordinates the known listeners; each listener owner still performs the actual socket bind. Ports allocated internally by an upstream engine remain with that engine.

Local temporary servers follow a different lifecycle. The profiling viewer binds port `0`, retains the returned listener, and publishes its actual address. Kubernetes allocates NodePorts through the Service object. Keep these existing resource owners rather than adding a shared probe that finds a number, closes the socket and later tries to reuse it.

## Preserve ownership during recovery

When persistent runtime storage is unavailable, only paths below that storage root move to the Pod's temporary cache. An independently mounted model directory remains at its projected location. Source preparation, cache reuse and engine startup must consume the same resolved path for each attempt.

A new configuration value should follow its existing owner through creation, use, observation and cleanup. Migrate every consumer when replacing a representation, regenerate derived schemas through the control-plane Makefile, and remove the old parsing or derivation path. Installation defaults do not silently rewrite an existing execution cohort; [source deployment](source-deployment.md) describes when new selections take effect.
