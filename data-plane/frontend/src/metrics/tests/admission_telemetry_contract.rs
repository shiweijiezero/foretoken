// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use foretoken_metrics::{QueueGuard, autoscaling_telemetry};
use foretoken_router::{RouteTargetSet, ScalingTarget, ScalingTargetKind};

#[test]
fn queue_guard_releases_every_target_exactly_once() {
    let targets = RouteTargetSet::new(vec![
        ScalingTarget {
            service_uid: "metrics-test".into(),
            name: "prefill".into(),
            uid: "prefill-uid".into(),
            kind: ScalingTargetKind::Pool,
        },
        ScalingTarget {
            service_uid: "metrics-test".into(),
            name: "decode".into(),
            uid: "decode-uid".into(),
            kind: ScalingTargetKind::Pool,
        },
    ]);
    {
        let _guard = QueueGuard::new(&targets);
        assert!(
            autoscaling_telemetry()
                .targets
                .iter()
                .filter(|value| value.target.service_uid == "metrics-test")
                .all(|value| value.queued_requests == 1)
        );
    }
    assert!(
        autoscaling_telemetry()
            .targets
            .iter()
            .filter(|value| value.target.service_uid == "metrics-test")
            .all(|value| value.queued_requests == 0)
    );
}
