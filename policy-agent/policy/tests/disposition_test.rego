package agent.disposition_test

import rego.v1

import data.agent.disposition

d(band, rec) := r if {
	r := disposition.result with input as {"band": band, "recommendation": rec}
}

# full disposition input (gates + critic status), for the approval-precedence tests
e(input_obj) := r if {
	r := disposition.result with input as input_obj
}

test_proceed_is_admissible_only_for_low_risk if {
	d("LOW", "PROCEED").admissible
	not d("MEDIUM", "PROCEED").admissible
	not d("HIGH", "PROCEED").admissible
	not d("VERY_HIGH", "PROCEED").admissible
}

test_high_risk_admits_refer_mlro_and_rejects_proceed if {
	r := d("HIGH", "REFER_MLRO")
	r.admissible
	r.outcome == "ADMIT"
	not d("HIGH", "PROCEED").admissible
	d("HIGH", "PROCEED").outcome == "REJECT"
	d("HIGH", "PROCEED").reason == "NOT_IN_BAND_MATRIX"
}

test_very_high_risk_cannot_hold_for_rfi if {
	not d("VERY_HIGH", "HOLD_FOR_RFI").admissible
	d("VERY_HIGH", "ESCALATE_EDD").admissible
	d("VERY_HIGH", "REFER_MLRO").admissible
}

test_unknown_band_is_rejected_not_defaulted if {
	r := d("EXTREME", "REFER_MLRO")
	not r.admissible
	r.reason == "UNKNOWN_BAND"
	r.outcome == "REJECT"
}

test_critic_failure_forces_human_approval if {
	r := disposition.result with input as {
		"band": "LOW", "recommendation": "PROCEED",
		"critic_status": "FAIL",
	}
	r.admissible
	r.critic_failed
	r.human_approval_required
	r.approval_reason == "CRITIC_FAILED"
	r.agent_may_finalise == false
}

test_fired_gates_require_approval_and_never_auto_finalise if {
	r := disposition.result with input as {
		"band": "MEDIUM", "recommendation": "PROCEED_WITH_CONDITIONS",
		"required_gates": ["RESIDUAL_HIGH_OR_ABOVE"],
	}
	r.admissible
	r.human_approval_required
	r.agent_may_finalise == false
}

test_agent_may_never_finalise_even_when_everything_is_clean if {
	r := d("LOW", "PROCEED")
	r.human_approval_required
	r.agent_may_finalise == false
}

# A failed critic and fired gates arrive together whenever OPS-VERIFY blocks a case. The
# approval_reason rule must resolve to exactly one value, not raise a conflict.
test_critic_failure_and_gates_do_not_conflict if {
	r := e({"band": "HIGH", "recommendation": "ESCALATE_EDD",
		"required_gates": ["CRITIC_FAIL", "COVERAGE_BELOW_MINIMUM"], "critic_status": "FAIL"})
	r.approval_reason == "CRITIC_FAILED"
	r.critic_failed == true
	r.gates_fired == true
	r.admissible == true
	r.outcome == "ADMIT"
}

test_critic_failure_alone_still_needs_a_human if {
	r := e({"band": "LOW", "recommendation": "PROCEED", "required_gates": [],
		"critic_status": "FAIL"})
	r.approval_reason == "CRITIC_FAILED"
	r.human_approval_required == true
	r.agent_may_finalise == false
}

test_no_gates_no_critic_is_still_human if {
	r := e({"band": "LOW", "recommendation": "PROCEED", "required_gates": [],
		"critic_status": "PASS"})
	r.approval_reason == "HUMAN_APPROVAL_ALWAYS_REQUIRED"
}
