// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves model admission overrides and their complete execution-pool targets.

package frontend

import (
	"cmp"
	"fmt"
	"slices"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
)

// effectiveModelAdmission resolves independent whole-block overrides and validates model-local Pool references.
// An empty model validates frontend defaults before they are bound to any model's Pools.
// Canonical list order and durations keep snapshot revisions and same-model comparisons semantic.
func effectiveModelAdmission(frontend inferencev1alpha1.FrontendServiceSpec, model inferencev1alpha1.ModelServiceSpec) (servingconfig.Admission, error) {
	if model.Backend == "vllm-omni" {
		// Video keeps its own execution lifecycle and does not inherit text admission rules.
		_, err := compiler.CompileModelService(model)
		return servingconfig.Admission{}, err
	}
	selected := model.Admission
	if selected == nil {
		selected = frontend.Admission
	}
	if err := selected.Validate(); err != nil {
		return servingconfig.Admission{}, err
	}
	config := servingconfig.Admission{}
	if selected != nil {
		copied := selected.DeepCopy()
		config.MaxWaitingRequests = copied.MaxWaitingRequests
		config.QueueTimeout = copied.QueueTimeout
	}
	if config.MaxWaitingRequests != nil && *config.MaxWaitingRequests == 0 {
		return servingconfig.Admission{}, fmt.Errorf("maxWaitingRequests must be positive")
	}
	if config.QueueTimeout != "" {
		timeout, err := time.ParseDuration(string(config.QueueTimeout))
		if err != nil || timeout <= 0 {
			return servingconfig.Admission{}, fmt.Errorf("queueTimeout must be a positive duration")
		}
		config.QueueTimeout = inferencev1alpha1.Duration(timeout.String())
	}
	rules := model.RoleRules
	if rules == nil {
		rules = frontend.RoleRules
	}
	if rules == nil || len(*rules) == 0 {
		return config, nil
	}
	if config.MaxWaitingRequests == nil {
		return servingconfig.Admission{}, fmt.Errorf("roleRules requires maxWaitingRequests")
	}

	// Pool names come from the same compiler that owns the shorthand's default Pool identity.
	var pools []compiler.ModelPool
	if model.Model != "" && slices.ContainsFunc(*rules, func(rule inferencev1alpha1.RoleRule) bool { return len(rule.AllowedPools) > 0 }) {
		var err error
		pools, err = compiler.CompileModelService(model)
		if err != nil {
			return servingconfig.Admission{}, fmt.Errorf("resolve allowedPools: %w", err)
		}
	}
	config.RoleRules = slices.Clone(*rules)
	for index := range config.RoleRules {
		rule := &config.RoleRules[index]
		if rule.Role == "" {
			return servingconfig.Admission{}, fmt.Errorf("roleRules role must not be empty")
		}
		if rule.PerCaller.MaxWaitingRequests == 0 || rule.PerCaller.MaxConcurrentRequests == 0 {
			return servingconfig.Admission{}, fmt.Errorf("roleRules %q perCaller capacities must be positive", rule.Role)
		}
		var allowedRoles []inferencev1alpha1.ModelRole
		for _, name := range rule.AllowedPools {
			if model.Model == "" {
				continue
			}
			poolIndex := slices.IndexFunc(pools, func(pool compiler.ModelPool) bool { return pool.Name == name })
			if poolIndex < 0 {
				return servingconfig.Admission{}, fmt.Errorf("roleRules %q allowedPools references unknown Pool %q", rule.Role, name)
			}
			allowedRoles = append(allowedRoles, pools[poolIndex].Template.Role)
		}
		if len(rule.AllowedPools) == 0 {
			rule.AllowedPools = nil
		} else {
			// The compiler has validated the topology; restrictions must retain every required stage.
			// Replica counts and readiness do not change whether an execution path is configured.
			for _, pool := range pools {
				if !slices.Contains(allowedRoles, pool.Template.Role) {
					return servingconfig.Admission{}, fmt.Errorf("roleRules %q allowedPools has no complete execution path: missing %s Pool", rule.Role, pool.Template.Role)
				}
			}
			rule.AllowedPools = slices.Clone(rule.AllowedPools)
			slices.Sort(rule.AllowedPools)
			rule.AllowedPools = slices.Compact(rule.AllowedPools)
		}
	}
	slices.SortFunc(config.RoleRules, func(left, right inferencev1alpha1.RoleRule) int {
		return cmp.Compare(left.Role, right.Role)
	})
	for index := 1; index < len(config.RoleRules); index++ {
		if config.RoleRules[index-1].Role == config.RoleRules[index].Role {
			return servingconfig.Admission{}, fmt.Errorf("roleRules contains duplicate role %q", config.RoleRules[index].Role)
		}
	}
	return config, nil
}

// admissionTargetSetsForService selects autoscaling targets that admit each service request.
func admissionTargetSetsForService(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool) [][]servingconfig.ScalingTarget {
	targets := make([]servingconfig.ScalingTarget, 0, len(pools))
	for _, pool := range pools {
		role := pool.Spec.Template.Role
		if role != inferencev1alpha1.ModelRoleAggregate && role != inferencev1alpha1.ModelRoleEncoder && role != inferencev1alpha1.ModelRolePrefill && role != inferencev1alpha1.ModelRoleDecode {
			continue
		}
		targets = append(targets, servingconfig.ScalingTarget{ServiceUID: string(service.UID), Name: pool.Spec.PoolName, UID: string(pool.UID), Kind: string(core.TargetPool)})
	}
	slices.SortFunc(targets, func(left, right servingconfig.ScalingTarget) int {
		return cmp.Compare(left.UID, right.UID)
	})
	if servingconfig.PoolsHaveEPD(pools) {
		if !servingconfig.PoolsContainCompleteEPD(pools) {
			return nil
		}
		return [][]servingconfig.ScalingTarget{targets}
	}
	if servingconfig.PoolsHavePD(pools) || len(pools) == 0 && servingconfig.ServiceDeclaresPD(service) {
		hasPrefill := slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
			return pool.Spec.Template.Role == inferencev1alpha1.ModelRolePrefill
		})
		hasDecode := slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
			return pool.Spec.Template.Role == inferencev1alpha1.ModelRoleDecode
		})
		if !hasPrefill || !hasDecode {
			return nil
		}
		return [][]servingconfig.ScalingTarget{targets}
	}
	sets := make([][]servingconfig.ScalingTarget, len(targets))
	for index, target := range targets {
		sets[index] = []servingconfig.ScalingTarget{target}
	}
	return sets
}
