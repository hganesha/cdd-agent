package agent.authority

import rego.v1

# METADATA
# title: Agent action authority
# description: >
#   Decides whether an agent may perform an action. Capability is split three ways: allowed,
#   approval_required (with a named approver and TTL), and denied. Denials are not negotiable via
#   prompt: no input makes a prohibited action allowed, and unknown actions default to denied.
# custom:
#   owner: platform-security
#   version: 1.0.0
#   scope: [authorization, agent-safety]
#   entrypoint: false
#   related_controls: [SEC-01, SEC-02]

prohibited_actions := {
	"evidence.delete",
	"evidence.mutate",
	"case.close",
	"case.approve",
	"risk.override",
	"sanctions.clear",
	"report.file",
	"account.freeze",
	"network.arbitrary",
	"policy.activate",
	"policy.self_modify",
}

gated_actions := {
	"rfi.issue": {"approver": "CASE_ANALYST", "ttl_hours": 24},
	"escalation.create": {"approver": "COMPLIANCE_OFFICER", "ttl_hours": 8},
	"risk.recommend": {"approver": "CASE_ANALYST", "ttl_hours": 72},
	"policy.propose_change": {"approver": "POLICY_OWNER", "ttl_hours": 168},
	"bundle.stage": {"approver": "PLATFORM_SECURITY", "ttl_hours": 24},
}

allowed_actions := {
	"search.query",
	"search.fetch",
	"document.read",
	"policy.evaluate",
	"evidence.append",
	"case.read",
	"report.compose",
	"bundle.verify",
	"bundle.inspect",
}

requested := object.get(input, "action", "")

default is_prohibited := false

is_prohibited if prohibited_actions[requested]

default is_gated := false

is_gated if gated_actions[requested]

default is_allowed := false

is_allowed if allowed_actions[requested]

decision := "denied" if is_prohibited

decision := "approval_required" if {
	not is_prohibited
	is_gated
}

decision := "allowed" if {
	not is_prohibited
	not is_gated
	is_allowed
}

decision := "denied" if {
	not is_prohibited
	not is_gated
	not is_allowed
}

reason := "prohibited_action" if is_prohibited

reason := "human_approval_required" if {
	not is_prohibited
	is_gated
}

reason := "explicitly_allowed" if {
	not is_prohibited
	not is_gated
	is_allowed
}

reason := "not_an_enumerated_capability" if {
	not is_prohibited
	not is_gated
	not is_allowed
}

default approval := null

approval := gated_actions[requested] if is_gated

result := {
	"rule_version": "opa/agent.authority@1.0.0",
	"action": requested,
	"decision": decision,
	"reason": reason,
	"approval": approval,
}

policy_surface := {
	"prohibited": sort([a | some a in prohibited_actions]),
	"gated": sort([a | some a in gated_actions]),
	"allowed": sort([a | some a in allowed_actions]),
}
