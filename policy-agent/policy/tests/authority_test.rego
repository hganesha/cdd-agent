package agent.authority_test

import rego.v1

auth(action) := r if {
	r := data.agent.authority.result with input as {"action": action}
}

test_prohibited_actions_are_never_allowed if {
	surface := data.agent.authority.policy_surface
	count(surface.prohibited) > 0
	every a in surface.prohibited {
		auth(a).decision == "denied"
		auth(a).reason == "prohibited_action"
	}
}

test_agent_cannot_mutate_or_delete_evidence if {
	auth("evidence.delete").decision == "denied"
	auth("evidence.mutate").decision == "denied"
	auth("evidence.append").decision == "allowed"
}

test_agent_cannot_close_or_approve_a_case if {
	auth("case.close").decision == "denied"
	auth("case.approve").decision == "denied"
	auth("case.read").decision == "allowed"
}

test_agent_cannot_override_risk_or_clear_sanctions if {
	auth("risk.override").decision == "denied"
	auth("sanctions.clear").decision == "denied"
	auth("risk.recommend").decision == "approval_required"
}

test_gated_actions_require_named_approver_and_ttl if {
	some action, meta in data.agent.authority.gated_actions
	r := auth(action)
	r.decision == "approval_required"
	r.approval.approver == meta.approver
	r.approval.ttl_hours == meta.ttl_hours
}

test_unknown_action_defaults_to_denied if {
	auth("case.exfiltrate_everything").decision == "denied"
	auth("case.exfiltrate_everything").reason == "not_an_enumerated_capability"
}

test_empty_action_is_denied if {
	auth("").decision == "denied"
}

test_policy_cannot_be_activated_by_an_agent if {
	auth("policy.activate").decision == "denied"
	auth("policy.self_modify").decision == "denied"
}
