<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Maintaining vLLM patches

English | [简体中文](README_zh.md)

Declare patches in `catalog.json` in application order. A patch used by both the pinned source and all Python engine profiles has one entry:

```json
{
  "path": "common/engine-abort-completion.patch",
  "rust": true,
  "python": {}
}
```

`rust: true` selects the patch for `make vllm-source`. A `python` rule selects it for installed-engine and engine-source builds; `{}` covers all profiles. Use `profiles`, `versions`, or `requires` inside that rule to restrict it to compatibility profiles, package versions, or an existing upstream file. The catalog's top-level `profiles` map Python versions to profile names.

| Location | Purpose |
| --- | --- |
| `common/` | Shared Python, communication, and native-code patches |
| `compatibility/` | Variants for different upstream interfaces |
| `rust/` | Patches to the upstream Rust crates |

Patch paths are relative to this directory. Patches use `-p1` against the upstream repository root or the installed package's parent directory, both of which contain `vllm/`.

Edit upstream source and regenerate unified diffs. Keep shared behavior in `common/` and interface-specific changes in `compatibility/`. Check the selected patch stack against each affected source version and exercise the changed runtime behavior.

The model-server image uses `apply.py`; engine-source builds use `engine_patches()` from `vllm_patches.py`. Both select Python rules from the catalog. Rust source preparation selects its `rust` entries. Repeat preparation or image construction to verify reuse of an already-patched tree.

Image and source builds share dependency adaptation in `vllm_patches.py`. Preserve the engine image's NCCL override and packages required by other consumers when updating dependencies.

Patches for another library belong in a separate directory alongside `vllm/`.

`common/scheduler-load.patch` owns token-demand accounting. Version-specific scheduler hooks supply all waiting queues and capture decode contexts before scheduling advances token counts. Preempted requests retain their context demand even though their computed-token counter has been reset.

## Engine completion contract

`rust/engine-request-completion.patch` provides a `RequestCompletion` handle subscribed before transport submission. It confirms termination only from an engine-origin terminal output or finished-request notification. The handle outlives the output consumer: stream drop, an abort acknowledgement, and locally generated abort output cannot complete it. Transport closure ends the observation with an error, not successful termination.

`common/engine-abort-completion.patch` makes Python EngineCore publish scheduler-confirmed aborted request IDs on every transport, including eager aborts. Keep both patches aligned when upstream request registration, scheduler termination, or output delivery changes.
Completion marks the end of request scheduling, not GPU synchronization or connector KV release.
When changing this contract, exercise normal completion, cancellation, and output-consumer drop on the affected transports, checking the independent completion signal as well as the returned output.

## Retained device prefixes

The [retained-prefix directory](../../model-server/python/connectors/retained.py) shares discovery and acquisition across NIXL and Mooncake. Native connectors own KV references and transfer completion. `common/kv-connector-utility.patch` exposes the directory through the existing EngineCore worker RPC; `catalog.json` selects the lifecycle patches for each supported engine interface.

`common/nixl-retained-prefix.patch` keeps acquired sources alive across the decoder's idle TTL. Readers release them after native READ completion; unstarted reads can be revoked. The engine retires with its buffers held if native transfer status is unknown or unavailable, or its heartbeat is lost. The 0.30 compatibility patch supplies the peer-replacement and expiry hooks used by this same lifecycle.

`common/mooncake-retained-prefix.patch` uses the native sender loop for acquisition, revocation and WRITE submission. Idle sources use `kv_lease_duration`, independently of the ordinary send timeout. Revocation and expiry require every admitted reader to have completed its WRITE or confirmed that no WRITE was submitted; remaining unstarted readers then receive a miss. Errors with unknown I/O completion retire the worker. Receivers join all source completions before reporting a load result, and the scheduler waits for every TP worker before freeing blocks.

When updating these patches, run two-turn P/D generation through different frontends, compare reused and recomputed output tokens, and inspect allocator references during cancellation, expiry, peer exit and TP completion. Use the [P/D configuration](../../../docs/features/inference-parameters.md#prefilldecode-separation-and-kv-reuse) to select the transport.
