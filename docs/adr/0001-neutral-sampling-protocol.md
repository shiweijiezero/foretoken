# ADR-0001: Neutral sampling protocol with per-engine construction

- Status: Accepted
- Date: 2026-08-26
- Scope: `data-plane/model-protocol`, `data-plane/model-server`, and eventually `data-plane/frontend`

## Context

Foretoken routes chat/completion requests from a backend-agnostic frontend to a
model-server that runs one inference engine (vLLM or SGLang). The wire contract
between them is `foretoken-model-protocol`'s `GenerateInput`/`SamplingParams`.

The neutral `SamplingParams` models the common sampling subset as typed fields
(`temperature`, `top_p`, `top_k`, `max_tokens`, penalties, `stop_token_ids`,
...), but also carries an opaque escape hatch:

```rust
pub extra_args: BTreeMap<String, serde_json::Value>
```

### Root cause: the wire protocol is vLLM-centric

The frontend is internally modeled on vLLM (`vllm_llm::GenerateRequest` and
`EngineCoreSamplingParams`). Its `llm-facade` conversion
(`frontend/src/llm-facade/src/conversion.rs::to_neutral_sampling`) folds every
vLLM-specific field of `EngineCoreSamplingParams` into `extra_args` using the
vLLM field names (`all_stop_token_ids`, `structured_outputs`, `logit_bias`,
`eos_token_id`, `thinking_token_budget`, ...).

So the "neutral" `extra_args` map is in fact a vLLM-owned bag. Consequences:

1. **Leakage.** The SGLang adapter (`model-server/src/engine/sglang/backend.rs`)
   forwarded `extra_args` verbatim into SGLang's native `sampling_params`.
   SGLang's `SamplingParams` rejects unknown keyword arguments, producing
   `TypeError: Unexpected keyword argument 'all_stop_token_ids'` and a
   `request_failed` terminal event for every SGLang chat request.
2. **Asymmetry.** vLLM is the "core" that owns the bag and consumes its keys via
   a whitelist (`engine/vllm/conversion.rs::to_vllm_sampling`); SGLang is an
   appendage that can only ignore the bag. Adding a third engine re-creates the
   SGLang problem.

### Reference: Dynamo's design

NVIDIA Dynamo went through the same trajectory (vLLM-first, then SGLang/TRT-LLM
bolted on as separate handler trees), then did a unified-backend refactor
(commit `#8003`) introducing a neutral `GenerateRequest.sampling_options` of
engine-agnostic keys, with each engine adapter constructing its own backend
params (`_build_sampling_params`). Each engine has its own native passthrough —
vLLM's `SamplingParams.extra_args`, SGLang's `sampling_params["custom_params"]` —
so engines stay symmetric: the neutral protocol never carries a frontend-filled
engine-specific bag.

## Decision drivers

- **Zero leakage**: an engine must never receive another engine's sampling keys.
- **Peer equivalence**: vLLM and SGLang are equal citizens, each with its own
  typed extension channel; neither is the "core" that owns a shared bag.
- **Extensibility**: adding an engine means adding a neutral-concept-to-backend
  mapping, not touching the neutral protocol or other engines.
- **Stability**: keep the SGLang bring-up unblocked; do not destabilize the
  working vLLM path.

## Considered options

- **Blacklist filtering** (vLLM keys filtered out in the SGLang adapter).
  Rejected: a patch that hard-codes a list of another engine's keys; still
  asymmetric and unmaintainable.
- **Per-engine enum** (`enum SamplingExt { Vllm(...), Sglang(...) }`).
  Rejected as the target: still couples the neutral protocol to every engine's
  names, and requires the frontend to know the backend to tag correctly.
- **Neutral concepts + per-engine construction** (chosen target). The neutral
  protocol carries only engine-agnostic typed concepts; each engine adapter
  maps them to its backend and writes engine-specific data into its own native
  channel. This is Dynamo's pattern.

## Decision

### Now (this change): S0 — stop the leak

The SGLang adapter constructs SGLang's native sampling params from the neutral
typed fields only, and never forwards `extra_args`. Engine-specific keys are
not dropped by filtering; the adapter simply does not consume them. SGLang's
own native channel (`custom_params`) will be wired up by the target design
below.

This unblocks SGLang chat with minimal risk: only the SGLang adapter changes,
plus a regression test asserting vLLM keys do not leak.

### Target (deferred future work): S1/S2 — peer-equivalent neutral protocol

The neutral `SamplingParams` becomes a pure set of engine-agnostic concepts:

```rust
pub struct SamplingParams {
    // existing neutral typed fields ...
    logit_bias: Option<LogitBias>,           // promoted from extra_args (neutral)
    guided_decoding: Option<GuidedDecoding>, // neutral enum: Json/Regex/Choice/Grammar
    allowed_token_ids: Option<Vec<u32>>,
    bad_words_token_ids: Option<Vec<u32>>,
    // extra_args is removed
}
```

- `extra_args` is deleted from the neutral protocol.
- Each engine adapter maps neutral concepts to its backend and owns its
  backend-specific defaults internally:
  - vLLM: `guided_decoding` → `structured_outputs`; `all_stop_token_ids`
    computed from `stop_token_ids` + the engine's real tokenizer eos; the rest
    of the vLLM-only fields (`thinking_token_budget`, `repetition_detection`,
    `eos_token_id`, `skip_reading_prefix_cache`, `logprob_token_ids`) become
    vLLM-internal; engine-specific passthrough goes to vLLM
    `SamplingParams.extra_args`.
  - SGLang: `guided_decoding` → `json_schema`/`regex`/`structural_tag`;
    engine-specific passthrough goes to SGLang `custom_params`.
- A concept becomes a neutral field only when a second engine needs it; this is
  what keeps the neutral protocol from growing vLLM-specific surface.
- The frontend (`frontend/src/llm-facade/src/conversion.rs` and
  `text/src/lower.rs`) stops injecting vLLM field names into the protocol and
  maps vLLM's `EngineCoreSamplingParams` back into neutral concepts. This is
  the part that removes the vLLM-centrism at its source.
- Sibling issue (same asymmetry) recorded for follow-up: `EngineExtensions`
  (`mm_features`, `lora_request`, `reasoning_parser_kwargs`) carries vLLM-typed
  opaque values; it should follow the same neutralization.

### SGLang rich-integration roadmap (deferred)

Dynamo research (its engines report no model dtype; registration carries only
`context_length`, KV metrics, DP, bootstrap) informs later SGLang integration
work: richer SGLang metadata, guided decoding, and `custom_params` wiring are
all downstream of the target design and should build on it.

## Consequences

- **Positive**: SGLang chat works again; the adapter layer is ready to become
  peer-equivalent; the plan for the deeper refactor is recorded here for when
  it is scheduled.
- **Negative**: vLLM-only request features that relied on SGLang forwarding
  `extra_args` are not forwarded (they never worked for SGLang); until S1/S2
  land, the wire protocol still carries a vLLM-shaped bag, so SGLang-specific
  sampling extensions cannot yet be expressed.
- **Risks**: S1/S2 touches the frontend (the largest chunk); schedule it as a
  dedicated effort with its own review, and keep the vLLM path green throughout.

## Implementation scope

- **S0 (done)**: `data-plane/model-server/src/engine/sglang/backend.rs` — stop
  forwarding `extra_args`; regression test.
- **S1/S2 (future)**: `data-plane/model-protocol/src/types.rs` (neutral concepts,
  remove `extra_args`); `data-plane/model-server/src/engine/vllm/conversion.rs`
  and `data-plane/model-server/src/engine/sglang/backend.rs` (per-engine
  construction); `data-plane/frontend/src/llm-facade/src/conversion.rs` and
  `data-plane/frontend/src/text/src/lower.rs` (frontend decoupling).
