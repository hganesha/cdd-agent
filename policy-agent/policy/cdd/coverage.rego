package cdd.coverage

import rego.v1

# METADATA
# title: Required-data-element coverage
# description: >
#   Computes coverage of the required data elements for the requested CDD level and decides whether
#   the gaps block a decision. NOT_APPLICABLE elements are excluded from the denominator; blocking
#   gaps are defined by policy, not by whoever is reviewing.
# custom:
#   owner: financial-crime-policy
#   version: 1.0.0
#   scope: [coverage, gaps]
#   entrypoint: false
#   related_controls: [CDD-11]

cdd_level := object.get(input.case, "cdd_level_requested", "CDD")

min_coverage_pct := data.config.thresholds.min_coverage_pct[cdd_level]

elements := object.get(input.coverage, "elements", [])

applicable := [e | some e in elements; e.status != "NOT_APPLICABLE"]

satisfied := [e | some e in applicable; e.status == "SATISFIED"]

coverage_pct := pct if {
	pct := round((count(satisfied) / count(applicable)) * 1000) / 10
} else := 0.0

# an element blocks the decision when it is not satisfied AND policy marks it decision-critical
decision_blocking := [e |
	some e in applicable
	e.status != "SATISFIED"
	object.get(e, "blocks_decision", false) == true
]

blocking_rde_ids := sort([e.rde_id | some e in decision_blocking])

default below_minimum := false

below_minimum if coverage_pct < min_coverage_pct

blocks_decision if {
	is_array(decision_blocking)
	count(decision_blocking) > 0
} else := false

result := {
	"rule_version": "opa/cdd.coverage@1.0.0",
	"cdd_level": cdd_level,
	"min_coverage_pct": min_coverage_pct,
	"coverage_pct": coverage_pct,
	"applicable": count(applicable),
	"satisfied": count(satisfied),
	"decision_blocking": decision_blocking,
	"blocking_rde_ids": blocking_rde_ids,
	"below_minimum": below_minimum,
	"blocks_decision": blocks_decision,
}
