package cdd.gates

import rego.v1

# METADATA
# title: Human gate routing
# description: >
#   Maps case conditions to the human gate that must be cleared before a dossier can be treated as
#   final, and to the queue that owns the decision. Routing is a policy outcome: the agent cannot
#   choose its reviewer or waive a gate. This package is the composition layer: it reads the
#   computed results of the other cdd packages rather than re-reading raw input.
# custom:
#   owner: financial-crime-policy
#   version: 1.0.0
#   scope: [escalation, human-gates]
#   entrypoint: false
#   related_controls: [CDD-14, CDD-15]

default residual_high_or_above := false

residual_high_or_above if data.cdd.risk.result.band == "HIGH"

residual_high_or_above if data.cdd.risk.result.band == "VERY_HIGH"

default screening_incomplete := false

screening_incomplete if not data.cdd.screening.result.screening_complete

conditions := {
	"RESIDUAL_HIGH_OR_ABOVE": residual_high_or_above,
	"UNRESOLVED_UBO": data.cdd.ubo.result.blocks_conclusion,
	"SANCTIONS_BLOCKING_HIT": data.cdd.screening.result.requires_adjudication,
	"PEP_EXPOSURE": data.cdd.screening.result.pep_exposure,
	"COVERAGE_BELOW_MINIMUM": data.cdd.coverage.result.below_minimum,
	"DECISION_BLOCKING_GAPS": data.cdd.coverage.result.blocks_decision,
	"SCREENING_INCOMPLETE": screening_incomplete,
	"CRITIC_FAIL": object.get(input, "critic_status", "PASS") == "FAIL",
	"INJECTION_ATTEMPT": object.get(input, "injection_attempt", false),
}

forced_by_policy := {g | some g in data.config.gates.force}

required_gates := {g | some g in forced_by_policy; conditions[g]}

default blocking_hit := false

blocking_hit if conditions.SANCTIONS_BLOCKING_HIT

default elevated := false

elevated if {
	not blocking_hit
	count([1 | some c in ["PEP_EXPOSURE", "UNRESOLVED_UBO", "RESIDUAL_HIGH_OR_ABOVE"]; conditions[c]]) > 0
}

route := "MLRO" if blocking_hit

route := "EDD_UNIT" if {
	not blocking_hit
	elevated
}

route := "COMPLIANCE_OFFICER" if {
	not blocking_hit
	not elevated
}

result := {
	"rule_version": "opa/cdd.gates@1.0.0",
	"conditions": conditions,
	"required_gates": sort([g | required_gates[g]]),
	"route_to": route,
	"blocks_final_status": count(required_gates) > 0,
}
