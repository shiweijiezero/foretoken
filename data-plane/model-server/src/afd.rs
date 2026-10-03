// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Private controller-projected AFD configuration and connector constraints.

use serde::Deserialize;

/// Role of one execution group in the paired Attention/FFN topology.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AfdRole {
    Attention,
    Ffn,
}

/// Controller-owned inputs shared by an AFD pair, with a role for this group.
///
/// The host identifies the shared rendezvous, not this group's local leader.
/// An absent block disables AFD; populated blocks must be complete.
#[derive(Debug, Clone, PartialEq, Eq, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AfdPlan {
    #[serde(default)]
    role: Option<AfdRole>,
    #[serde(default)]
    connector: String,
    #[serde(default, rename = "rendezvousHost")]
    rendezvous_host: String,
    #[serde(default, rename = "numAttentionRanks")]
    num_attention_ranks: u32,
    #[serde(default, rename = "numFfnRanks")]
    num_ffn_ranks: u32,
    #[serde(default, rename = "connectorPort")]
    connector_port: u16,
}

impl AfdPlan {
    /// Reports whether a validated launch plan configures an AFD role.
    pub fn enabled(&self) -> bool {
        self.role.is_some()
    }

    /// Rejects incomplete controller projections before launch-plan validation continues.
    pub(crate) fn validate(&self) -> Result<(), String> {
        if self == &Self::default() {
            return Ok(());
        }
        if self.role.is_none() {
            return Err("AFD role is required".into());
        }
        if self.connector.is_empty() {
            return Err("AFD connector is required".into());
        }
        if self.rendezvous_host.is_empty() {
            return Err("AFD rendezvousHost is required".into());
        }
        if self.num_attention_ranks == 0 {
            return Err("AFD numAttentionRanks must be positive".into());
        }
        if self.num_ffn_ranks == 0 {
            return Err("AFD numFfnRanks must be positive".into());
        }
        if self.connector_port == 0 {
            return Err("AFD connectorPort must be non-zero".into());
        }
        if self.connector != "P2pNcclAFDConnector" {
            return Err("AFD connector must be P2pNcclAFDConnector".into());
        }
        // The pinned P2P connector assigns an equal number of Attention ranks to each FFN rank.
        if !self.num_attention_ranks.is_multiple_of(self.num_ffn_ranks) {
            return Err("AFD numAttentionRanks must be a multiple of numFfnRanks".into());
        }
        // Each FFN subgroup opens base + subgroup_index + 1 in addition to the base rendezvous.
        if self.num_ffn_ranks > u32::from(u16::MAX - self.connector_port) {
            return Err("AFD connector and subgroup ports must fit in 1..65535".into());
        }
        Ok(())
    }

    /// Checks a runtime owner's consecutive ports against the validated connector range.
    ///
    /// Launch and bootstrap callers supply their actual ports; this check allocates no listener
    /// and leaves the plan unchanged. Disabled AFD reserves no ports.
    pub fn validate_port_range(
        &self,
        first_port: u16,
        count: usize,
        purpose: &str,
    ) -> Result<(), String> {
        if !self.enabled() || count == 0 {
            return Ok(());
        }
        let first = u32::from(self.connector_port);
        let last = first + self.num_ffn_ranks;
        let runtime_first = u32::from(first_port);
        let overlaps = if first <= runtime_first {
            runtime_first <= last
        } else {
            ((first - runtime_first) as usize) < count
        };
        if overlaps {
            return Err(format!("AFD connector ports overlap {purpose}"));
        }
        Ok(())
    }
}
