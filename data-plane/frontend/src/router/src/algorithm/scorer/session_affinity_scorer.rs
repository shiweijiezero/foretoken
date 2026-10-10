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
    snapshot_version: u64,
}

// A frontend pipeline serves multiple models; each stage keeps model-scoped session bindings.
type Bindings = [BTreeMap<(String, String), Binding>; 3];

#[derive(Clone, Copy)]
struct EvictionSettings {
    ttl: Duration,
    interval: Duration,
}

impl Default for EvictionSettings {
    fn default() -> Self {
        Self {
            ttl: Duration::from_secs(300),
            interval: Duration::from_secs(10),
        }
    }
}

#[derive(Default)]
struct AffinityState {
    bindings: Bindings,
    eviction: EvictionSettings,
    activated_version: Option<u64>,
}

/// Prefers the session's bound target and rank; published pipelines share bindings and one sweeper.
#[derive(Default)]
pub struct SessionAffinityScorer {
    state: Arc<Mutex<AffinityState>>,
    eviction: EvictionSettings,
    // All configurations sharing the bindings keep this single worker alive.
    eviction_wake: Option<mpsc::Sender<()>>,
}

impl SessionAffinityScorer {
    /// Parses eviction settings for both initial construction and immutable replacements.
    fn eviction_settings(parameters: serde_json::Value) -> Result<EvictionSettings, String> {
        #[derive(Default, Deserialize)]
        #[serde(default, rename_all = "camelCase", deny_unknown_fields)]
        struct Parameters {
            eviction_ttl_seconds: f64,
            eviction_sweep_seconds: f64,
        }
        let config: Parameters =
            serde_json::from_value(parameters).map_err(|error| error.to_string())?;
        let defaults = EvictionSettings::default();
        let mut durations = [Duration::ZERO; 2];
        for (duration, (name, value, default)) in durations.iter_mut().zip([
            (
                "evictionTtlSeconds",
                config.eviction_ttl_seconds,
                defaults.ttl,
            ),
            (
                "evictionSweepSeconds",
                config.eviction_sweep_seconds,
                defaults.interval,
            ),
        ]) {
            let value = if value == 0.0 {
                default.as_secs_f64()
            } else {
                value
            };
            *duration = Duration::try_from_secs_f64(value)
                .map_err(|_| format!("{name} must be a positive finite duration"))?;
            if duration.is_zero() {
                return Err(format!("{name} must be at least one nanosecond"));
            }
        }
        let [ttl, interval] = durations;
        Ok(EvictionSettings { ttl, interval })
    }
}

impl RouteScorer for SessionAffinityScorer {
    /// Applies idle eviction parameters and starts the sole worker for a fresh binding history.
    fn configure(&mut self, parameters: serde_json::Value) -> Result<(), String> {
        let eviction = Self::eviction_settings(parameters)?;
        let state = Arc::new(Mutex::new(AffinityState {
            eviction,
            ..AffinityState::default()
        }));
        let weak = Arc::downgrade(&state);
        let (wake, receiver) = mpsc::channel();
        std::thread::Builder::new()
            .name("session-affinity-eviction".into())
            .spawn(move || {
                loop {
                    let interval = {
                        let Some(state) = weak.upgrade() else {
                            break;
                        };
                        let state = state.lock().expect("session binding lock poisoned");
                        state.eviction.interval
                    };
                    match receiver.recv_timeout(interval) {
                        Ok(()) => continue,
                        Err(mpsc::RecvTimeoutError::Disconnected) => break,
                        Err(mpsc::RecvTimeoutError::Timeout) => {}
                    }
                    let Some(state) = weak.upgrade() else {
                        break;
                    };
                    let mut state = state.lock().expect("session binding lock poisoned");
                    // Read the published TTL while holding the binding lock, never a retired TTL.
                    let ttl = state.eviction.ttl;
                    let now = Instant::now();
                    for profile in state.bindings.iter_mut() {
                        profile.retain(|_, binding| now.duration_since(binding.last_seen) <= ttl);
                    }
                }
            })
            .map_err(|error| error.to_string())?;
        self.eviction_wake = Some(wake);
        self.state = state;
        self.eviction = eviction;
        Ok(())
    }

    fn reconfigure(
        &self,
        parameters: serde_json::Value,
    ) -> Option<Result<Arc<dyn RouteScorer>, String>> {
        Some(Self::eviction_settings(parameters).map(|eviction| {
            Arc::new(Self {
                state: self.state.clone(),
                eviction,
                eviction_wake: self.eviction_wake.clone(),
            }) as Arc<dyn RouteScorer>
        }))
    }

    fn activate(&self, snapshot_version: u64) {
        let mut state = self.state.lock().expect("session binding lock poisoned");
        if state
            .activated_version
            .is_some_and(|version| version >= snapshot_version)
        {
            return;
        }
        state.activated_version = Some(snapshot_version);
        let interval_changed = state.eviction.interval != self.eviction.interval;
        state.eviction = self.eviction;
        // Ordinary snapshot publication must not postpone cleanup by resetting its timer.
        if interval_changed && let Some(wake) = &self.eviction_wake {
            wake.send(()).expect("session eviction worker stopped");
        }
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

    /// Captures bound-target eligibility and the snapshot version for the selection commit.
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
        let session_key = (request.model.clone(), session_id.to_owned());
        let bound = self
            .state
            .lock()
            .expect("session binding lock poisoned")
            .bindings[profile]
            .get(&session_key)
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
        let snapshot_version = routing_progress.snapshot_version;
        let state = self.state.clone();
        ScoringOutcome {
            scores,
            on_selected: Some(Box::new(move |candidate| {
                let mut state = state.lock().expect("session binding lock poisoned");
                let fresh = Binding {
                    target: (
                        candidate.route_target_id.clone(),
                        candidate.data_parallel_rank,
                    ),
                    last_seen: Instant::now(),
                    snapshot_version,
                };
                match state.bindings[profile].entry(session_key) {
                    Entry::Vacant(entry) => {
                        entry.insert(fresh);
                    }
                    Entry::Occupied(mut entry) => {
                        let binding = entry.get_mut();
                        // Every selection counts as activity. Retiring snapshots cannot move a
                        // binding established or confirmed by a newer snapshot.
                        binding.last_seen = fresh.last_seen;
                        if snapshot_version >= binding.snapshot_version {
                            binding.snapshot_version = snapshot_version;
                            if present == Some(false) {
                                binding.target = fresh.target;
                            }
                        }
                    }
                }
            })),
        }
    }
}
