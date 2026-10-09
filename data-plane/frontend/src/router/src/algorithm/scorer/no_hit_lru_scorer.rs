// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Cold-request LRU scoring.

use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex};

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;
use serde::Deserialize;

use super::ScoringOutcome;
use crate::{
    RouteCandidate, RouteScore, RouteScorer, RouteTargetId, RouterRequest, RoutingProgress,
    RoutingStage,
};

const DEFAULT_LRU_CAPACITY: usize = 1024;

struct ColdHistory {
    records: VecDeque<(RouteTargetId, u32)>,
    capacity: usize,
    activated_version: Option<u64>,
}

/// Prefers targets least recently selected for cold requests; hot requests leave history unchanged.
/// Published configurations share history and its current capacity across request lifetimes.
pub struct NoHitLruScorer {
    capacity: usize,
    history: Arc<Mutex<ColdHistory>>,
}

impl Default for NoHitLruScorer {
    fn default() -> Self {
        Self {
            capacity: DEFAULT_LRU_CAPACITY,
            history: Arc::new(Mutex::new(ColdHistory {
                records: VecDeque::new(),
                capacity: DEFAULT_LRU_CAPACITY,
                activated_version: None,
            })),
        }
    }
}

impl NoHitLruScorer {
    /// Parses the capacity for both initial construction and immutable replacements.
    fn configured_capacity(parameters: serde_json::Value) -> Result<usize, String> {
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase", deny_unknown_fields)]
        struct Parameters {
            lru_size: Option<i64>,
        }
        let parameters: Parameters =
            serde_json::from_value(parameters).map_err(|error| error.to_string())?;
        parameters
            .lru_size
            .filter(|size| *size > 0)
            .map_or(Ok(DEFAULT_LRU_CAPACITY), |size| {
                usize::try_from(size).map_err(|error| error.to_string())
            })
    }
}

impl RouteScorer for NoHitLruScorer {
    /// Requests the same complete-block prefix observations as the prefix scorer.
    fn needs_kv_prefix(&self) -> bool {
        true
    }

    /// Applies the LRU capacity before serving; nonpositive or null values use 1024.
    fn configure(&mut self, parameters: serde_json::Value) -> Result<(), String> {
        let capacity = Self::configured_capacity(parameters)?;
        *self = Self {
            capacity,
            history: Arc::new(Mutex::new(ColdHistory {
                records: VecDeque::new(),
                capacity,
                activated_version: None,
            })),
        };
        Ok(())
    }

    fn reconfigure(
        &self,
        parameters: serde_json::Value,
    ) -> Option<Result<Arc<dyn RouteScorer>, String>> {
        Some(Self::configured_capacity(parameters).map(|capacity| {
            Arc::new(Self {
                capacity,
                history: self.history.clone(),
            }) as Arc<dyn RouteScorer>
        }))
    }

    fn activate(&self, snapshot_version: u64) {
        let mut history = self
            .history
            .lock()
            .expect("cold-request history lock poisoned");
        if history
            .activated_version
            .is_some_and(|version| version >= snapshot_version)
        {
            return;
        }
        history.activated_version = Some(snapshot_version);
        history.capacity = self.capacity;
        let excess = history.records.len().saturating_sub(history.capacity);
        history.records.drain(..excess);
    }

    /// Returns scores without committing a selection or changing cold-request history.
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

    /// Returns LRU scores and captures a cold-only update for the validated selection.
    /// Ranks candidates against the full history, including targets absent from this round.
    fn score_for_selection(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        routing_progress: &RoutingProgress<'_>,
        _customized_context: &mut (),
    ) -> ScoringOutcome {
        let belongs_to_profile = |candidate: &RouteCandidate| {
            let has_stage_role = match routing_progress.current_stage {
                RoutingStage::Initial => matches!(
                    candidate.role,
                    ModelServerRole::Aggregate | ModelServerRole::Prefill
                ),
                RoutingStage::Prefill => candidate.role == ModelServerRole::Prefill,
                RoutingStage::Decode => candidate.role == ModelServerRole::Decode,
            };
            has_stage_role
                && routing_progress
                    .pipeline_scope_id
                    .is_none_or(|scope| candidate.pipeline_scope_id.as_deref() == Some(scope))
        };
        if candidates
            .iter()
            .filter(|candidate| belongs_to_profile(candidate))
            .any(|candidate| {
                crate::cache::cache_match(request, candidate, kv_prefix_indexer)
                    .is_some_and(|matched| matched.matched_blocks > 0)
            })
        {
            return vec![
                RouteScore {
                    preference: 0.5,
                    ..RouteScore::default()
                };
                candidates.len()
            ]
            .into();
        }

        // Rank against the entire LRU, without compacting ranks to the current candidate set.
        // Retain absent targets until capacity eviction; gaps can clamp used scores to zero.
        let positions: BTreeMap<_, _> = self
            .history
            .lock()
            .expect("cold-request history lock poisoned")
            .records
            .iter()
            .cloned()
            .enumerate()
            .map(|(position, key)| (key, position))
            .collect();
        let keys: Vec<_> = candidates
            .iter()
            .filter(|candidate| belongs_to_profile(candidate))
            .map(|candidate| {
                (
                    candidate.route_target_id.clone(),
                    candidate.data_parallel_rank,
                )
            })
            .collect();
        let never_used = keys
            .iter()
            .filter(|key| !positions.contains_key(*key))
            .count();
        let mut next_unused = 0;
        let mut endpoint_scores = keys.iter().map(|key| {
            let rank = positions.get(key).map_or_else(
                || {
                    let rank = next_unused;
                    next_unused += 1;
                    rank
                },
                |position| never_used + position,
            );
            RouteScore {
                preference: if keys.len() == 1 {
                    1.0
                } else {
                    (1.0 - rank as f64 / (keys.len() - 1) as f64).max(0.0)
                },
                ..RouteScore::default()
            }
        });
        let scores = candidates
            .iter()
            .map(|candidate| {
                if belongs_to_profile(candidate) {
                    endpoint_scores
                        .next()
                        .expect("every routable endpoint has an LRU score")
                } else {
                    RouteScore {
                        preference: 0.5,
                        ..RouteScore::default()
                    }
                }
            })
            .collect();
        let history = self.history.clone();
        ScoringOutcome {
            scores,
            on_selected: Some(Box::new(move |candidate| {
                if candidate.role == ModelServerRole::Encoder {
                    return;
                }
                let key = (
                    candidate.route_target_id.clone(),
                    candidate.data_parallel_rank,
                );
                let mut history = history.lock().expect("cold-request history lock poisoned");
                if let Some(position) = history.records.iter().position(|existing| existing == &key)
                {
                    history.records.remove(position);
                }
                history.records.push_back(key);
                // Retired requests use the published capacity, never their scorer's old setting.
                if history.records.len() > history.capacity {
                    history.records.pop_front();
                }
            })),
        }
    }
}
