// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Contract tests for independent positional and radix locality indexes.
use foretoken_kv_indexer::*;
use foretoken_model_protocol::*;
use std::time::{Duration, Instant};

const KEY: [u8; 32] = [7; 32];

fn source(id: &str, owner: &str, epoch: &str, rank: u32) -> KvEventSourceId {
    KvEventSourceId {
        event_source_id: id.into(),
        model_group_id: owner.into(),
        epoch: epoch.into(),
        dp_rank: rank,
    }
}

fn partition(group_idx: Option<u32>) -> KvPartition {
    KvPartition {
        model_revision: "revision".into(),
        scope_id: "scope".into(),
        hash_format: KvHashFormat::NormalizedKeyedBlake3V1,
        hash_block_size: 2,
        group_idx,
        spec_kind: "full".into(),
        sliding_window: None,
    }
}

fn query(group_idx: Option<u32>) -> KvPrefixQuery<'static> {
    KvPrefixQuery {
        tokens: &[1, 2, 3, 4, 5, 6],
        model_revision: "revision",
        scope_id: "scope",
        hash_format: KvHashFormat::NormalizedKeyedBlake3V1,
        group_idx,
        spec_kind: "full",
        sliding_window: None,
    }
}

fn blocks(group_idx: Option<u32>) -> Vec<KvStoredBlock> {
    let partition = partition(group_idx);
    let mut parent_hash = KvBlockHash(String::new());
    [vec![1_u32, 2], vec![3, 4], vec![5, 6]]
        .into_iter()
        .enumerate()
        .map(|(block_index, tokens)| {
            // This is the protocol hash contract, kept independent from both implementations.
            let mut hasher = blake3::Hasher::new_keyed(&KEY);
            hasher.update(parent_hash.0.as_bytes());
            hasher.update(partition.model_revision.as_bytes());
            hasher.update(partition.scope_id.as_bytes());
            hasher.update(&partition.hash_block_size.to_le_bytes());
            hasher.update(&partition.group_idx.unwrap_or(u32::MAX).to_le_bytes());
            hasher.update(partition.spec_kind.as_bytes());
            hasher.update(&partition.sliding_window.unwrap_or(u32::MAX).to_le_bytes());
            for token in tokens {
                hasher.update(&token.to_le_bytes());
            }
            let block_hash = KvBlockHash(base64::Engine::encode(
                &base64::engine::general_purpose::URL_SAFE_NO_PAD,
                hasher.finalize().as_bytes(),
            ));
            let block = KvStoredBlock {
                partition: partition.clone(),
                block_index: block_index as u64,
                parent_hash: parent_hash.clone(),
                block_hash: block_hash.clone(),
            };
            parent_hash = block_hash;
            block
        })
        .collect()
}

fn placement(tier: KvStorageTier, locality: KvCacheLocality) -> KvPlacement {
    KvPlacement { tier, locality }
}

fn assert_matches(
    index: &mut dyn KvLocalityIndex,
    source: &KvEventSourceId,
    group_idx: Option<u32>,
    now: Instant,
    expected: &[(KvPlacement, usize)],
) {
    let actual = index
        .query(source, &query(group_idx), &KEY, now)
        .into_iter()
        .map(|matched| (matched.placement, matched.matched_tokens))
        .collect::<Vec<_>>();
    assert_eq!(actual, expected);
}

fn run_for_each_index(test: impl Fn(&mut dyn KvLocalityIndex)) {
    let mut positional = PositionalHashIndex::new(Duration::from_secs(1));
    test(&mut positional);
    let mut radix = RadixTreeIndex::new(Duration::from_secs(1));
    test(&mut radix);
}

#[test]
fn placement_group_and_source_identity_are_exact() {
    let now = Instant::now();
    let owner = source("events-a", "group-a", "epoch-a", 0);
    let same_event_new_epoch = source("events-a", "group-a", "epoch-b", 0);
    let same_event_other_rank = source("events-a", "group-a", "epoch-a", 1);
    let other_event_source = source("events-b", "group-a", "epoch-a", 0);
    let other_model_group = source("events-a", "group-b", "epoch-a", 0);
    let device_local = placement(KvStorageTier::Device, KvCacheLocality::Local);
    let device_remote = placement(KvStorageTier::Device, KvCacheLocality::Remote);
    let host_pinned = placement(KvStorageTier::HostPinned, KvCacheLocality::Remote);
    let disk_local = placement(KvStorageTier::Disk, KvCacheLocality::Local);
    let external = placement(KvStorageTier::External, KvCacheLocality::Remote);

    run_for_each_index(|index| {
        for (group, location) in [
            (None, device_local),
            (Some(4), device_remote),
            (Some(4), host_pinned),
            (Some(4), disk_local),
            (Some(4), external),
        ] {
            index.apply(
                owner.clone(),
                KvIndexEvent::BlockStored {
                    blocks: blocks(group),
                    placement: location,
                },
                now,
            );
        }

        assert_matches(index, &owner, None, now, &[(device_local, 6)]);
        assert_matches(
            index,
            &owner,
            Some(4),
            now,
            &[
                (device_remote, 6),
                (host_pinned, 6),
                (disk_local, 6),
                (external, 6),
            ],
        );
        // The selector-free protocol clear is scoped by the complete response envelope only.
        index.apply(
            same_event_new_epoch.clone(),
            KvIndexEvent::BlockStored {
                blocks: blocks(None),
                placement: device_local,
            },
            now,
        );
        index.apply(owner.clone(), KvIndexEvent::AllBlocksCleared, now);
        assert_matches(index, &owner, None, now, &[]);
        assert_matches(index, &owner, Some(4), now, &[]);
        assert_matches(
            index,
            &same_event_new_epoch,
            None,
            now,
            &[(device_local, 6)],
        );
        assert_matches(index, &same_event_other_rank, None, now, &[]);
        assert_matches(index, &other_event_source, None, now, &[]);
        assert_matches(index, &other_model_group, None, now, &[]);
    });
}

#[test]
fn hash_remove_recursively_erases_only_its_placement_branch() {
    let now = Instant::now();
    let owner = source("events-a", "group-a", "epoch-a", 0);
    let device = placement(KvStorageTier::Device, KvCacheLocality::Local);
    let disk = placement(KvStorageTier::Disk, KvCacheLocality::Remote);

    run_for_each_index(|index| {
        for location in [device, disk] {
            index.apply(
                owner.clone(),
                KvIndexEvent::BlockStored {
                    blocks: blocks(None),
                    placement: location,
                },
                now,
            );
        }
        index.apply(
            owner.clone(),
            KvIndexEvent::BlockRemoved {
                block_hashes: vec![blocks(None)[1].block_hash.clone()],
                placement: device,
                group_idx: None,
            },
            now,
        );

        assert_matches(index, &owner, None, now, &[(device, 2), (disk, 6)]);
    });
}

#[test]
fn unknown_hash_removal_fails_closed_in_its_exact_group_scope() {
    let now = Instant::now();
    let owner = source("events-a", "group-a", "epoch-a", 0);
    let device = placement(KvStorageTier::Device, KvCacheLocality::Local);
    let disk = placement(KvStorageTier::Disk, KvCacheLocality::Remote);

    run_for_each_index(|index| {
        for (group, location) in [(None, device), (Some(8), device), (Some(8), disk)] {
            index.apply(
                owner.clone(),
                KvIndexEvent::BlockStored {
                    blocks: blocks(group),
                    placement: location,
                },
                now,
            );
        }
        index.apply(
            owner.clone(),
            KvIndexEvent::BlockRemoved {
                block_hashes: vec![KvBlockHash("not-in-reverse-map".into())],
                placement: device,
                group_idx: Some(8),
            },
            now,
        );

        assert_matches(index, &owner, None, now, &[(device, 6)]);
        assert_matches(index, &owner, Some(8), now, &[(disk, 6)]);
    });
}

#[test]
fn expired_parent_prunes_refreshed_descendants_and_epoch_rollover_isolated() {
    let now = Instant::now();
    let old_epoch = source("events-a", "group-a", "epoch-a", 0);
    let new_epoch = source("events-a", "group-a", "epoch-b", 0);
    let device = placement(KvStorageTier::Device, KvCacheLocality::Local);

    run_for_each_index(|index| {
        index.apply(
            old_epoch.clone(),
            KvIndexEvent::BlockStored {
                blocks: blocks(None),
                placement: device,
            },
            now,
        );
        assert_matches(index, &new_epoch, None, now, &[]);

        index.clear_source(&old_epoch);
        index.apply(
            new_epoch.clone(),
            KvIndexEvent::BlockStored {
                blocks: blocks(None),
                placement: device,
            },
            now,
        );
        index.apply(
            new_epoch.clone(),
            KvIndexEvent::BlockStored {
                blocks: vec![blocks(None)[2].clone()],
                placement: device,
            },
            now + Duration::from_millis(500),
        );

        assert_matches(index, &new_epoch, None, now + Duration::from_secs(2), &[]);

        let chain = blocks(None);
        let orphan_time = now + Duration::from_secs(3);
        index.apply(
            new_epoch.clone(),
            KvIndexEvent::BlockStored {
                blocks: vec![chain[2].clone()],
                placement: device,
            },
            orphan_time,
        );
        assert_matches(index, &new_epoch, None, orphan_time, &[]);
        index.apply(
            new_epoch.clone(),
            KvIndexEvent::BlockStored {
                blocks: vec![chain[0].clone()],
                placement: device,
            },
            orphan_time,
        );
        assert_matches(index, &new_epoch, None, orphan_time, &[(device, 2)]);
        index.apply(
            new_epoch.clone(),
            KvIndexEvent::BlockStored {
                blocks: vec![chain[1].clone()],
                placement: device,
            },
            orphan_time,
        );
        assert_matches(index, &new_epoch, None, orphan_time, &[(device, 6)]);

        index.apply(
            new_epoch.clone(),
            KvIndexEvent::BlockStored {
                blocks: vec![chain[0].clone(), chain[2].clone()],
                placement: device,
            },
            orphan_time + Duration::from_millis(500),
        );
        assert_matches(
            index,
            &new_epoch,
            None,
            orphan_time + Duration::from_millis(1250),
            &[(device, 2)],
        );
        index.apply(
            new_epoch.clone(),
            KvIndexEvent::BlockRemoved {
                block_hashes: vec![chain[0].block_hash.clone()],
                placement: device,
                group_idx: None,
            },
            orphan_time + Duration::from_millis(1250),
        );
        assert_matches(
            index,
            &new_epoch,
            None,
            orphan_time + Duration::from_millis(1250),
            &[],
        );
    });
}

#[test]
fn unspecified_locality_is_not_a_cache_hit() {
    let now = Instant::now();
    let owner = source("events-a", "group-a", "epoch-a", 0);
    let unspecified = placement(KvStorageTier::Device, KvCacheLocality::Unspecified);
    run_for_each_index(|index| {
        index.apply(
            owner.clone(),
            KvIndexEvent::BlockStored {
                blocks: blocks(None),
                placement: unspecified,
            },
            now,
        );
        assert_matches(index, &owner, None, now, &[]);
    });
}

#[test]
fn radix_pending_orphans_expire_deduplicate_and_fail_closed_on_cycles() {
    let now = Instant::now();
    let owner = source("events-a", "group-a", "epoch-a", 0);
    let device = placement(KvStorageTier::Device, KvCacheLocality::Local);
    let chain = blocks(None);
    let mut index = RadixTreeIndex::new(Duration::from_secs(1));

    index.apply(
        owner.clone(),
        KvIndexEvent::BlockStored {
            blocks: vec![chain[2].clone()],
            placement: device,
        },
        now,
    );
    index.apply(
        owner.clone(),
        KvIndexEvent::BlockStored {
            blocks: vec![chain[2].clone()],
            placement: device,
        },
        now + Duration::from_millis(500),
    );
    index.apply(
        owner.clone(),
        KvIndexEvent::BlockStored {
            blocks: vec![chain[0].clone(), chain[1].clone()],
            placement: device,
        },
        now + Duration::from_millis(750),
    );
    assert_matches(
        &mut index,
        &owner,
        None,
        now + Duration::from_millis(1250),
        &[(device, 6)],
    );
    assert_matches(
        &mut index,
        &owner,
        None,
        now + Duration::from_millis(1750),
        &[(device, 4)],
    );

    index.clear_source(&owner);
    index.apply(
        owner.clone(),
        KvIndexEvent::BlockStored {
            blocks: vec![chain[2].clone()],
            placement: device,
        },
        now,
    );
    assert_matches(&mut index, &owner, None, now + Duration::from_secs(2), &[]);

    let mut self_parent = chain[0].clone();
    self_parent.parent_hash = self_parent.block_hash.clone();
    index.apply(
        owner.clone(),
        KvIndexEvent::BlockStored {
            blocks: vec![self_parent],
            placement: device,
        },
        now,
    );
    assert_matches(&mut index, &owner, None, now, &[]);

    let mut a = chain[0].clone();
    let mut b = chain[1].clone();
    a.parent_hash = b.block_hash.clone();
    b.parent_hash = a.block_hash.clone();
    index.apply(
        owner.clone(),
        KvIndexEvent::BlockStored {
            blocks: vec![a],
            placement: device,
        },
        now,
    );
    index.apply(
        owner.clone(),
        KvIndexEvent::BlockStored {
            blocks: vec![b],
            placement: device,
        },
        now,
    );
    assert_matches(&mut index, &owner, None, now, &[]);
}
