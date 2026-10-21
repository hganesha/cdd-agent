package agent.disposition

import rego.v1

# METADATA
# title: Disposition admissibility
# description: >
#   Decides whether a proposed case disposition is one the policy permits for the case's risk band.
#   The decision agent proposes; this package decides. A recommendation outside
#   data.config.decision_matrix[band].allowed_recommendations is REJECTED, and a case whose critic
#   failed or whose gates fired can never be finalised, whatever the recommendation.
# custom:
#   owner: financial-crime-policy
#   version: 1.0.0
#   scope: [disposition, agent-integration]
#   entrypoint: true
#   related_controls: [CDD-19, CDD-20]

band := object.get(input, "band", "")

recommendation := object.get(input, "recommendation", "")

allowed_recommendations := object.get(object.get(data.config.decision_matrix, band, {}), "allowed_recommendations", [])

default known_band := false

known_band if data.config.decision_matrix[band]

default admissible := false

admissible if {
	known_band
	some r in allowed_recommendations
	r == recommendation
}

critic_failed := object.get(input, "critic_status", "PASS") == "FAIL"

gates_fired := count(object.get(input, "required_gates", [])) > 0

# Every disposition needs a human, always. Gates and critic failures are recorded because they
# change *who* must approve and what they must look at.
human_approval_required := true

# Precedence, not three independent rules: a complete rule that produces two outputs is an
# eval_conflict_error, and a bundle that fails its critic *and* fires gates is the common case.
approval_reason := "CRITIC_FAILED" if critic_failed

approval_reason := "GATES_FIRED" if {
	not critic_failed
	gates_fired
}

approval_reason := "HUMAN_APPROVAL_ALWAYS_REQUIRED" if {
	not critic_failed
	not gates_fired
}

outcome := "ADMIT" if admissible

outcome := "REJECT" if not admissible

reason := "ADMISSIBLE_FOR_BAND" if admissible

reason := "NOT_IN_BAND_MATRIX" if {
	not admissible
	known_band
}

reason := "UNKNOWN_BAND" if not known_band

result := {
	"rule_version": "opa/agent.disposition@1.0.0",
	"band": band,
	"recommendation": recommendation,
	"allowed_recommendations": allowed_recommendations,
	"admissible": admissible,
	"outcome": outcome,
	"reason": reason,
	"human_approval_required": human_approval_required,
	"approval_reason": approval_reason,
	"critic_failed": critic_failed,
	"gates_fired": gates_fired,
	"agent_may_finalise": false,
}
