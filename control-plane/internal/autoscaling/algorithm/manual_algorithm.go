// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package algorithm

import "context"

// ManualAlgorithm preserves the capacity compiled from ModelService intent.
type ManualAlgorithm struct{}

func (ManualAlgorithm) Name() string { return "manual" }

func (ManualAlgorithm) Recommend(_ context.Context, snapshot Snapshot) (Recommendation, error) {
	return Recommendation{
		Target:        snapshot.Target,
		SnapshotID:    snapshot.Ref.ID,
		Disposition:   RecommendationApply,
		DesiredGroups: snapshot.Capacity.BaselineGroups,
		Reason:        ReasonManualIntent,
		Message:       "capacity follows ModelService replicas",
	}, nil
}
