// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Session-ID affinity with selection-time binding and periodic idle eviction.

use std::collections::{BTreeMap, btree_map::Entry};
use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

use foretoken_kv_indexer::KvPrefixIndexer;
use serde::Deserialize;

use super::ScoringOutcome;
use crate::{
    RouteCandidate, RouteScore, RouteScorer, RouteTargetId, RouterRequest, RoutingProgress,
    RoutingStage,
};

struct Binding {
    target: (RouteTargetId, u32),
    last_seen: Instant,
}

// Initial, Prefill, and Decode have independent bindings, as separate scheduling profiles do.
type Bindings = [BTreeMap<String, Binding>; 3];

/// Prefers the session's bound target and rank; a pipeline owns bindings across request lifetimes.
#[derive(Default)]
pub struct SessionAffinityScorer {
    bindings: Arc<Mutex<Bindings>>,
    // Dropping or replacing the scorer disconnects the sweeper; the worker retains only a Weak.
    eviction_stop: Option<mpsc::Sender<()>>,
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Strategy {
    #[default]
    SessionId,
}

#[derive(Default, Deserialize)]
#[serde(default, rename_all = "camelCase", deny_unknown_fields)]
struct SessionIdConfig {
    eviction_ttl_seconds: f64,
    eviction_sweep_seconds: f64,
}

impl RouteScorer for SessionAffinityScorer {
    /// Validates the session-ID strategy and starts eviction for this pipeline's lifetime.
    fn configure(&mut self, parameters: serde_json::Value) -> Result<(), String> {
        #[derive(Default, Deserialize)]
        #[serde(default, rename_all = "camelCase", deny_unknown_fields)]
        struct Parameters {
            #[serde(rename = "strategy")]
            _strategy: Strategy,
            session_id_config: SessionIdConfig,
        }
        let parameters: Parameters =
            serde_json::from_value(parameters).map_err(|error| error.to_string())?;
        let config = parameters.session_id_config;
        let mut durations = [Duration::ZERO; 2];
        for (duration, (name, value, default)) in durations.iter_mut().zip([
            ("evictionTtlSeconds", config.eviction_ttl_seconds, 300.0),
            ("evictionSweepSeconds", config.eviction_sweep_seconds, 10.0),
        ]) {
            let value = if value == 0.0 { default } else { value };
            *duration = Duration::try_from_secs_f64(value)
                .map_err(|_| format!("{name} must be a positive finite duration"))?;
            if duration.is_zero() {
                return Err(format!("{name} must be at least one nanosecond"));
            }
        }
        let [ttl, interval] = durations;
        let bindings: Arc<Mutex<Bindings>> = Arc::default();
        let weak = Arc::downgrade(&bindings);
        let (stop, receiver) = mpsc::channel();
        std::thread::Builder::new()
            .name("session-affinity-eviction".into())
            .spawn(move || {
                while matches!(
                    receiver.recv_timeout(interval),
                    Err(mpsc::RecvTimeoutError::Timeout)
                ) {
                    let Some(bindings) = weak.upgrade() else {
                        break;
                    };
                    let mut bindings = bindings.lock().expect("session binding lock poisoned");
                    let now = Instant::now();
                    for profile in bindings.iter_mut() {
                        profile.retain(|_, binding| now.duration_since(binding.last_seen) <= ttl);
                    }
                }
            })
            .map_err(|error| error.to_string())?;
        self.eviction_stop = Some(stop);
        self.bindings = bindings;
        Ok(())
    }

    /// Returns affinity preferences without refreshing or committing a session binding.
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut (),
    ) -> Vec<RouteScore> {
        self.score_for_selection(
            request,
            candidates,
            kv_prefix_indexer,
            routing_progress,
            customized_context,
        )
        .scores
    }

    /// Captures bound-target presence for the commit callback; only confirmed absence permits migration.
    fn score_for_selection(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        _kv_prefix_indexer: &dyn KvPrefixIndexer,
        routing_progress: &RoutingProgress<'_>,
        _customized_context: &mut (),
    ) -> ScoringOutcome {
        let mut scores = vec![RouteScore::default(); candidates.len()];
        let Some(session_id) = request
            .generate_request
            .as_ref()
            .and_then(|request| request.session_id.as_deref())
            .map(str::trim)
            .filter(|id| !id.is_empty())
        else {
            return scores.into();
        };
        let profile = match routing_progress.current_stage {
            RoutingStage::Initial => 0,
            RoutingStage::Prefill => 1,
            RoutingStage::Decode => 2,
        };
        let bound = self.bindings.lock().expect("session binding lock poisoned")[profile]
            .get(session_id)
            .map(|binding| binding.target.clone());
        let target = bound.as_ref().and_then(|(id, rank)| {
            candidates.iter().rposition(|candidate| {
                candidate.stage_eligible
                    && &candidate.route_target_id == id
                    && candidate.data_parallel_rank == *rank
            })
        });
        if let Some(row) = target {
            scores[row].preference = 1.0;
        }
        let present = bound.map(|_| target.is_some());
        let session_id = session_id.to_owned();
        let bindings = self.bindings.clone();
        ScoringOutcome {
            scores,
            on_selected: Some(Box::new(move |candidate| {
                let mut bindings = bindings.lock().expect("session binding lock poisoned");
                let fresh = Binding {
                    target: (
                        candidate.route_target_id.clone(),
                        candidate.data_parallel_rank,
                    ),
                    last_seen: Instant::now(),
                };
                match bindings[profile].entry(session_id) {
                    Entry::Vacant(entry) => {
                        entry.insert(fresh);
                    }
                    Entry::Occupied(mut entry) => {
                        if entry.get().target == fresh.target || present == Some(false) {
                            entry.insert(fresh);
                        }
                    }
                }
            })),
        }
    }
}
