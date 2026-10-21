package agent.decision

import rego.v1

# METADATA
# title: Unified CDD policy decision
# description: >
#   The single entrypoint an agent calls. Composes risk, ownership, screening, coverage, jurisdiction
#   and human-gate routing into one envelope: a stable decision_id, the policy version, the reason
#   codes that fired, the obligations that follow, and the gate routing. The outcome is a closed
#   enum, so there is no free text an agent could paraphrase into a permissive result.
# custom:
#   owner: financial-crime-policy
#   version: 1.0.0
#   scope: [decision, agent-integration]
#   entrypoint: true
#   related_controls: [CDD-01, CDD-14, CDD-18]

default invalid_input := false

invalid_input if not data.cdd.risk.result.valid_input

default very_high_risk := false

very_high_risk if data.cdd.risk.result.band == "VERY_HIGH"

default blocking_sanctions := false

blocking_sanctions if data.cdd.screening.result.requires_adjudication

default unresolved_ubo := false

unresolved_ubo if data.cdd.ubo.result.blocks_conclusion

default blocked := false

blocked if invalid_input

blocked if very_high_risk

blocked if blocking_sanctions

blocked if unresolved_ubo

cdd_level := object.get(input.risk, "cdd_level", "CDD")

default requires_edd := false

requires_edd if cdd_level == "EDD"

default rfi_required := false

rfi_required if data.cdd.coverage.result.blocks_decision

obligations := [o |
	some o in [
		{"kind": "ESCALATE_MLRO", "because": "SANCTIONS_HIT_REQUIRES_ADJUDICATION"},
		{"kind": "OBTAIN_SOURCE_OF_WEALTH", "because": "JURISDICTION_REQUIRES_SOW"},
		{"kind": "CLOSE_UBO_GAP", "because": "UNRESOLVED_BENEFICIAL_OWNERSHIP"},
		{"kind": "COMPLETE_SCREENING", "because": "SCREENING_INCOMPLETE"},
		{"kind": "HUMAN_EDD_APPROVAL", "because": "CDD_LEVEL_EDD"},
	]
	fires(o)
]

fires(o) if {
	o.kind == "ESCALATE_MLRO"
	blocking_sanctions
}

fires(o) if {
	o.kind == "OBTAIN_SOURCE_OF_WEALTH"
	data.jurisdiction.overlay.result.require_source_of_wealth
}

fires(o) if {
	o.kind == "CLOSE_UBO_GAP"
	unresolved_ubo
}

fires(o) if {
	o.kind == "COMPLETE_SCREENING"
	not data.cdd.screening.result.screening_complete
}

fires(o) if {
	o.kind == "HUMAN_EDD_APPROVAL"
	requires_edd
}

outcome := "BLOCK" if blocked

outcome := "REQUIRE_EDD" if {
	not blocked
	requires_edd
}

outcome := "REQUIRE_RFI" if {
	not blocked
	not requires_edd
	rfi_required
}

outcome := "PERMIT_WITH_CONDITIONS" if {
	not blocked
	not requires_edd
	not rfi_required
	count(obligations) > 0
}

outcome := "PERMIT" if {
	not blocked
	not requires_edd
	not rfi_required
	count(obligations) == 0
}

default band_high := false

band_high if data.cdd.risk.result.band == "HIGH"

default floor_applied := false

floor_applied if count(data.cdd.risk.result.applied_overrides) > 0

default pep_exposure := false

pep_exposure if data.cdd.screening.result.pep_exposure

default control_prong_missing := false

control_prong_missing if data.cdd.ubo.result.control_prong_missing

default screening_incomplete := false

screening_incomplete if not data.cdd.screening.result.screening_complete

default below_minimum := false

below_minimum if data.cdd.coverage.result.below_minimum

default mlro_gate := false

mlro_gate if data.jurisdiction.overlay.result.mlro_gate_required

reason_conditions := {
	"RISK_BAND_VERY_HIGH": very_high_risk,
	"RISK_BAND_HIGH": band_high,
	"RISK_BAND_FLOOR_APPLIED": floor_applied,
	"SANCTIONS_HIT_REQUIRES_ADJUDICATION": blocking_sanctions,
	"PEP_EXPOSURE": pep_exposure,
	"UNRESOLVED_BENEFICIAL_OWNERSHIP": unresolved_ubo,
	"CONTROL_PRONG_MISSING": control_prong_missing,
	"SCREENING_INCOMPLETE": screening_incomplete,
	"COVERAGE_BELOW_MINIMUM": below_minimum,
	"DECISION_BLOCKING_GAPS": rfi_required,
	"UNKNOWN_RISK_FACTOR": invalid_input,
	"MLRO_GATE_REQUIRED": mlro_gate,
}

reasons := sort([r | some r, fired in reason_conditions; fired])

decision_id := sprintf("%s:%s:%s", [
	object.get(input.case, "case_id", "UNKNOWN"),
	data.config.policy_version,
	object.get(input, "input_hash", "noinputhash"),
])

result := {
	"decision_id": decision_id,
	"policy_version": data.config.policy_version,
	"outcome": outcome,
	"reasons": reasons,
	"obligations": obligations,
	"risk": data.cdd.risk.result,
	"ubo": data.cdd.ubo.result,
	"screening": data.cdd.screening.result,
	"coverage": data.cdd.coverage.result,
	"gates": data.cdd.gates.result,
	"jurisdiction": data.jurisdiction.overlay.result,
	"agent_may_clear_sanctions_hit": data.cdd.screening.result.agent_may_clear_sanctions_hit,
	"evaluated_at": object.get(input, "evaluate_at", "1970-01-01T00:00:00Z"),
}
