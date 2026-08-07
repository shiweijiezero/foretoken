use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use super::builtins::{AllowAllFilter, KvLoadScorer, StablePicker};
use super::{
    AdmissionTarget, BackendId, BackendRole, BackendRoute, ComponentCapacity, KvPrefixScorer,
    ModelRouter, NeutralScorer, PolicyRouter, RouteComponentCandidate, RouteContext, RouteError,
    RouteFilter, RouteInventory, RouteOptionCandidate, RouteOptionKind, RoutePicker, RouteScore,
    RouteScorer, Router, RouterAlgorithm, RouterPolicy, ScoredRouteOption,
};
use vllm_engine_core_client::protocol::lora::LoraRequest;
use vllm_llm::GenerateRequest;

fn backend(id: &str, revision: &str, ready: bool, capabilities: &[&str]) -> BackendRoute {
    BackendRoute {
        backend_id: BackendId::new(id),
        model: "Qwen/Qwen3-0.6B".into(),
        revision: revision.into(),
        capabilities: capabilities.iter().map(|value| (*value).into()).collect(),
        max_input_tokens: None,
        ready,
        role: BackendRole::Aggregate,
        domain_id: None,
    }
}
struct TestRouteContext {
    request: GenerateRequest,
    capabilities: BTreeSet<String>,
}
impl TestRouteContext {
    fn new(prompt_token_ids: Vec<u32>) -> Self {
        Self {
            request: GenerateRequest {
                request_id: "request".into(),
                prompt_token_ids,
                sampling_params: Default::default(),
                mm_features: None,
                arrival_time: None,
                cache_salt: None,
                trace_headers: None,
                priority: 0,
                data_parallel_rank: None,
                reasoning_parser_kwargs: None,
                lora_request: None,
            },
            capabilities: BTreeSet::from(["chat".into()]),
        }
    }
    fn context(&self) -> RouteContext<'_> {
        RouteContext {
            model: "Qwen/Qwen3-0.6B",
            revision: Some("r1"),
            required_capabilities: &self.capabilities,
            request: &self.request,
        }
    }
}

#[test]
fn returns_stable_ordered_candidates_from_matching_groups() {
    let router = ModelRouter::new(vec![
        backend("group-b", "r1", true, &["chat"]),
        backend("group-a", "r1", true, &["chat"]),
    ]);
    let request = TestRouteContext::new(vec![]);
    let candidates = router.candidates(request.context());
    assert_eq!(
        candidates
            .iter()
            .map(|candidate| candidate.backend_id.as_str())
            .collect::<Vec<_>>(),
        ["group-a", "group-b"]
    );
}

#[test]
fn filters_required_tool_and_structured_output_capabilities() {
    let router = ModelRouter::new(vec![
        backend("chat-only", "r1", true, &["chat"]),
        backend("tools", "r1", true, &["chat", "tool_calling"]),
        backend(
            "structured-tools",
            "r1",
            true,
            &["chat", "tool_calling", "structured_output.json_schema"],
        ),
    ]);
    let mut request = TestRouteContext::new(vec![]);
    request.capabilities.extend([
        "tool_calling".into(),
        "structured_output.json_schema".into(),
    ]);

    let candidates = router.candidates(request.context());
    assert_eq!(candidates.len(), 1);
    assert_eq!(candidates[0].backend_id.as_str(), "structured-tools");
}

#[test]
fn filters_lora_requests_to_capable_backends() {
    let router = ModelRouter::new(vec![
        backend("plain", "r1", true, &["chat"]),
        backend("lora", "r1", true, &["chat", "lora"]),
    ]);
    let mut request = TestRouteContext::new(vec![1]);
    request.request.lora_request = Some(LoraRequest::new(
        "adapter".into(),
        1,
        "/adapters/adapter".into(),
        false,
        false,
    ));

    let candidates = router.candidates(request.context());
    assert_eq!(candidates.len(), 1);
    assert_eq!(candidates[0].backend_id.as_str(), "lora");
}

#[test]
fn rejects_requests_that_exceed_the_group_token_limit() {
    let router = ModelRouter::new(vec![BackendRoute {
        backend_id: BackendId::new("small-group"),
        model: "Qwen/Qwen3-0.6B".into(),
        revision: "r1".into(),
        capabilities: BTreeSet::new(),
        max_input_tokens: Some(2),
        ready: true,
        role: BackendRole::Aggregate,
        domain_id: None,
    }]);
    let request = TestRouteContext::new(vec![1, 2, 3]);
    assert!(router.candidates(request.context()).is_empty());
}

struct TestInventory {
    router: ModelRouter,
    healthy: BTreeSet<BackendId>,
    targets: BTreeMap<BackendId, AdmissionTarget>,
}
impl RouteInventory for TestInventory {
    fn model_router(&self) -> &ModelRouter {
        &self.router
    }
    fn is_backend_healthy(&self, backend_id: &BackendId) -> bool {
        self.healthy.contains(backend_id)
    }
    fn admission_target(&self, backend_id: &BackendId) -> Option<AdmissionTarget> {
        self.targets.get(backend_id).cloned()
    }
}
struct TestScorer(BTreeMap<BackendId, usize>);
impl KvPrefixScorer for TestScorer {
    fn score_prefill_prefix(&self, backend_id: &BackendId, _: RouteContext<'_>) -> usize {
        self.0.get(backend_id).copied().unwrap_or(0)
    }
}

type ComponentSpec<'a> = (&'a str, u64, u64);
type RouteSpec<'a> = (&'a str, &'a [ComponentSpec<'a>]);

fn policy_router(targets: &[RouteSpec<'_>], scores: &[(&str, usize)]) -> PolicyRouter {
    let targets = targets
        .iter()
        .map(|(route, components)| {
            (
                BackendId::new(*route),
                AdmissionTarget {
                    components: components
                        .iter()
                        .map(|(id, running, max)| ComponentCapacity {
                            component_id: BackendId::new(*id),
                            running_requests: *running,
                            max_concurrent_requests: *max,
                        })
                        .collect(),
                },
            )
        })
        .collect();
    let healthy = ["a", "b", "pd-a", "pd-b"]
        .into_iter()
        .map(BackendId::new)
        .collect();
    PolicyRouter::new(
        Arc::new(TestInventory {
            router: ModelRouter::new(vec![
                backend("a", "r1", true, &["chat"]),
                backend("b", "r1", true, &["chat"]),
                backend("pd-a", "r1", true, &["chat"]),
                backend("pd-b", "r1", true, &["chat"]),
            ]),
            healthy,
            targets,
        }),
        Arc::new(TestScorer(
            scores
                .iter()
                .map(|(id, score)| (BackendId::new(*id), *score))
                .collect(),
        )),
    )
}

#[test]
fn capacity_is_hard_while_kv_is_soft_and_load_breaks_kv_ties() {
    let request = TestRouteContext::new(vec![1]);
    let router = policy_router(
        &[("a", &[("a", 1, 1)]), ("b", &[("b", 2, 4)])],
        &[("a", 100), ("b", 0)],
    );
    assert_eq!(
        router
            .select(request.context())
            .unwrap()
            .decision
            .backend_id
            .as_str(),
        "b"
    );
    let router = policy_router(
        &[("a", &[("a", 3, 4)]), ("b", &[("b", 1, 4)])],
        &[("a", 2), ("b", 2)],
    );
    assert_eq!(
        router
            .select(request.context())
            .unwrap()
            .decision
            .backend_id
            .as_str(),
        "b"
    );
}

#[test]
fn reservations_never_exceed_capacity_and_drop_releases() {
    let request = TestRouteContext::new(vec![]);
    let router = policy_router(&[("a", &[("a", 0, 1)]), ("b", &[("b", 0, 0)])], &[]);
    let first = router.select(request.context()).unwrap();
    assert_eq!(first.decision.backend_id.as_str(), "a");
    assert!(matches!(
        router.select(request.context()),
        Err(RouteError::NoCapacity { .. })
    ));
    drop(first);
    assert_eq!(
        router
            .select(request.context())
            .unwrap()
            .decision
            .backend_id
            .as_str(),
        "a"
    );
}

#[test]
fn pd_reservation_is_atomic_across_shared_components() {
    let request = TestRouteContext::new(vec![]);
    let router = policy_router(
        &[
            ("pd-a", &[("prefill", 0, 1), ("decode-a", 0, 1)]),
            ("pd-b", &[("prefill", 0, 1), ("decode-b", 0, 1)]),
            ("a", &[("a", 0, 0)]),
            ("b", &[("b", 0, 0)]),
        ],
        &[],
    );
    let first = router.select(request.context()).unwrap();
    assert!(matches!(
        first.decision.backend_id.as_str(),
        "pd-a" | "pd-b"
    ));
    let second = router.select(request.context());
    assert!(matches!(second, Err(RouteError::NoCapacity { .. })));
    drop(first);
}

#[test]
fn epd_triplet_is_selected_and_reserved_atomically() {
    let request = TestRouteContext::new(vec![]);
    let route = |id: &str, role: BackendRole| BackendRoute {
        role,
        domain_id: Some("epd-a".into()),
        ..backend(id, "r1", true, &["chat"])
    };
    let inventory = TestInventory {
        router: ModelRouter::new(vec![
            route("encoder", BackendRole::Encoder),
            route("prefill", BackendRole::Prefill),
            route("decode", BackendRole::Decode),
        ]),
        healthy: ["encoder", "prefill", "decode"]
            .into_iter()
            .map(BackendId::new)
            .collect(),
        targets: ["encoder", "prefill", "decode"]
            .into_iter()
            .map(|id| {
                (
                    BackendId::new(id),
                    AdmissionTarget {
                        components: vec![ComponentCapacity {
                            component_id: BackendId::new(id),
                            running_requests: 0,
                            max_concurrent_requests: 1,
                        }],
                    },
                )
            })
            .collect(),
    };
    let router = PolicyRouter::new(Arc::new(inventory), Arc::new(NeutralScorer));

    let first = router.select(request.context()).unwrap();
    assert!(matches!(
        first.plan,
        super::ExecutionPlan::EncoderPrefillDecode { .. }
    ));
    assert_eq!(first.decision.backend_id.as_str(), "decode");
    assert!(matches!(
        router.select(request.context()),
        Err(RouteError::NoCapacity { .. })
    ));
    drop(first);
    assert!(router.select(request.context()).is_ok());
}

fn router_with_policy(policy: RouterPolicy) -> PolicyRouter {
    let targets = ["a", "b"]
        .into_iter()
        .map(|id| {
            (
                BackendId::new(id),
                AdmissionTarget {
                    components: vec![ComponentCapacity {
                        component_id: BackendId::new(id),
                        running_requests: 0,
                        max_concurrent_requests: 1,
                    }],
                },
            )
        })
        .collect();
    PolicyRouter::with_policy(
        Arc::new(TestInventory {
            router: ModelRouter::new(vec![
                backend("a", "r1", true, &["chat"]),
                backend("b", "r1", true, &["chat"]),
            ]),
            healthy: ["a", "b"].into_iter().map(BackendId::new).collect(),
            targets,
        }),
        policy,
    )
}

struct OnlyB;
impl RouteFilter for OnlyB {
    fn allows(&self, option: &RouteOptionCandidate, _: RouteContext<'_>) -> bool {
        option
            .components
            .iter()
            .all(|component| component.backend_id.as_str() == "b")
    }
}

struct PreferA;
impl RouteScorer for PreferA {
    fn score(&self, option: &RouteOptionCandidate, _: RouteContext<'_>) -> RouteScore {
        RouteScore {
            topology: 0,
            locality: i64::from(option.components[0].backend_id.as_str() == "a"),
            load: 0,
        }
    }
}

struct ReversePicker;
impl RoutePicker for ReversePicker {
    fn order(&self, options: &[ScoredRouteOption], _: RouteContext<'_>, _: usize) -> Vec<usize> {
        (0..options.len()).rev().collect()
    }
}

#[test]
fn custom_filter_can_only_reduce_core_validated_options() {
    let request = TestRouteContext::new(vec![]);
    let router = router_with_policy(RouterPolicy::new(
        Arc::new(OnlyB),
        Arc::new(KvLoadScorer::new(Arc::new(NeutralScorer))),
        Arc::new(StablePicker),
    ));

    assert_eq!(
        router
            .select(request.context())
            .unwrap()
            .decision
            .backend_id
            .as_str(),
        "b"
    );
}

#[test]
fn custom_scorer_and_picker_order_complete_options_without_owning_reservation() {
    let request = TestRouteContext::new(vec![]);
    let router = router_with_policy(RouterPolicy::new(
        Arc::new(AllowAllFilter),
        Arc::new(PreferA),
        Arc::new(ReversePicker),
    ));

    let first = router.select(request.context()).unwrap();
    assert_eq!(first.decision.backend_id.as_str(), "b");
    let second = router.select(request.context()).unwrap();
    assert_eq!(second.decision.backend_id.as_str(), "a");
    assert!(matches!(
        router.select(request.context()),
        Err(RouteError::NoCapacity { .. })
    ));
    drop((first, second));
    assert!(router.select(request.context()).is_ok());
}

#[test]
fn built_in_algorithms_have_stable_names_and_distinct_scores() {
    assert_eq!("kv_aware".parse(), Ok(RouterAlgorithm::KvAware));
    assert_eq!("least_loaded".parse(), Ok(RouterAlgorithm::LeastLoaded));
    assert_eq!("round_robin".parse(), Ok(RouterAlgorithm::RoundRobin));
    assert!("unknown".parse::<RouterAlgorithm>().is_err());

    let request = TestRouteContext::new(vec![1]);
    let option = RouteOptionCandidate {
        kind: RouteOptionKind::PrefillDecode,
        components: vec![
            RouteComponentCandidate {
                backend_id: BackendId::new("prefill"),
                role: BackendRole::Prefill,
                domain_id: Some("pd".into()),
                load: 10,
                uses_prefix_locality: true,
            },
            RouteComponentCandidate {
                backend_id: BackendId::new("decode"),
                role: BackendRole::Decode,
                domain_id: Some("pd".into()),
                load: 1,
                uses_prefix_locality: false,
            },
        ],
    };
    let prefix = Arc::new(TestScorer(BTreeMap::from([(BackendId::new("prefill"), 8)])));
    assert_eq!(
        RouterAlgorithm::KvAware
            .policy(prefix.clone())
            .scorer
            .score(&option, request.context()),
        RouteScore {
            topology: 0,
            locality: 8,
            load: -11,
        }
    );
    assert_eq!(
        RouterAlgorithm::LeastLoaded
            .policy(prefix.clone())
            .scorer
            .score(&option, request.context()),
        RouteScore {
            topology: 0,
            locality: 0,
            load: -11,
        }
    );
    assert_eq!(
        RouterAlgorithm::RoundRobin
            .policy(prefix)
            .scorer
            .score(&option, request.context()),
        RouteScore {
            topology: 0,
            locality: 0,
            load: 0,
        }
    );
}

#[test]
fn missing_telemetry_is_classified() {
    let request = TestRouteContext::new(vec![]);
    let router = policy_router(&[], &[]);
    assert!(matches!(
        router.select(request.context()),
        Err(RouteError::TelemetryUnavailable { .. })
    ));
}
