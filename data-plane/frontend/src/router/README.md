<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

The Router selects one routable ModelGroup and DP rank for each execution stage.

Its pipeline has three list-level contracts:

- **Filter** receives the compatible, healthy candidate snapshot. It may remove candidates but cannot create or modify them.
- **Scorer** returns one `ScoredCandidate` for every retained candidate. Built-in KV scoring compares matched prompt tokens, storage tier, locality, and load.
- **Picker** returns one unchanged candidate from the scored list. The Router then exposes a `RouteDecision` containing the ModelGroup route target, execution role, model revision, and exact DP rank.

A RouteTarget with `data_parallel_size: 1` contributes only rank `0`, so its decision explicitly returns `data_parallel_rank: 0`. Larger targets contribute one candidate per rank.

## Example

```text
ModelGroups:
  llama3-serve-r-2gosa7pa2jpf2-0  UID 2f48f8e1-7f89-4eb8-bf31-e6d482504f66
  llama3-serve-r-2gosa7pa2jpf2-1  UID 8c88ee9a-c10f-41fd-98ef-a09d256b5213

Candidates:
  2f48f8e1-7f89-4eb8-bf31-e6d482504f66 / rank 0  KV match:   0 tokens
  2f48f8e1-7f89-4eb8-bf31-e6d482504f66 / rank 1  KV match: 512 tokens
  8c88ee9a-c10f-41fd-98ef-a09d256b5213 / rank 0  KV match: 256 tokens

Filter:  retain all three healthy, compatible candidates
Scorer:  first Group/rank 0 → 0, first Group/rank 1 → 512, second Group/rank 0 → 256
Picker:  select the first Group at rank 1

RouteDecision:
  route_target_id: 2f48f8e1-7f89-4eb8-bf31-e6d482504f66
  data_parallel_rank: 1
```

ModelGroup names follow `<pool-name>-<revision>-<ordinal>`. Router identity uses the Kubernetes ModelGroup UID rather than `metadata.name`; the name is used by the Deployment, Service, and service DNS endpoint.

For Aggregate and Prefill, built-in KV scoring is lexicographic: longest complete matched prompt prefix first, then `Device > HostPinned > Disk > External`, then `Local > Remote`, then lower load. Decode prefix, tier, and locality scores are zero. Unavailable KV facts never become a confirmed miss.
