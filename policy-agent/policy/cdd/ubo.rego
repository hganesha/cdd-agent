package cdd.ubo

import rego.v1

# METADATA
# title: Beneficial ownership determination
# description: >
#   Computes effective ownership along every path, sums across paths per natural person, applies the
#   policy threshold, and requires a control-prong determination when no natural person reaches it.
# custom:
#   owner: financial-crime-policy
#   version: 1.0.0
#   scope: [beneficial-ownership]
#   entrypoint: false
#   related_controls: [CDD-07, CDD-08, CDD-09]

threshold := data.config.thresholds.ubo_pct

# keyed by path index, not by person: one person may hold through several paths
path_factor[i] := factor if {
	some i, p in input.ownership.paths
	factor := product([y | some x in p.pcts; y := x / 100])
}

path_value[i] := v if {
	some i, p in input.ownership.paths
	f := path_factor[i]
	v := {
		"person": p.person,
		"chain": object.get(p, "chain", []),
		"pcts": p.pcts,
		"effective_pct": round(f * 10000) / 100,
		"basis": object.get(p, "basis", "document"),
	}
}

effective_pct[person] := value if {
	some person, _ in {x.person | some x in path_value}
	value := round(sum([v.effective_pct | some v in path_value; v.person == person]) * 100) / 100
}

beneficial_owners := {person | some person, _ in effective_pct; effective_pct[person] >= threshold}

below_threshold := {person | some person, _ in effective_pct; effective_pct[person] < threshold}

unresolved_links := [u | some u in object.get(input.ownership, "unresolved", [])]

unresolved_pct := sum([u.pct | some u in unresolved_links])

direct_holdings := object.get(input.ownership, "direct_holdings", [])

direct_total := sum([h.pct | some h in direct_holdings])

default sum_check := "UNKNOWN"

sum_check := "OK" if count([1 | abs(direct_total - 100) < 0.01]) == 1

sum_check := "DISCREPANCY" if count([1 | abs(direct_total - 100) >= 0.01]) == 1

# when no natural person reaches the threshold, control must be established on another basis
default control_prong_required := false

control_prong_required if count(beneficial_owners) == 0

control_person := object.get(input.ownership, "control_person", null)

default control_prong_evidenced := false

control_prong_evidenced if {
	control_prong_required
	control_person != null
}

default control_prong_missing := false

control_prong_missing if {
	control_prong_required
	control_person == null
}

default blocks_conclusion := false

# an undisclosed nominee blocks the conclusion, and so does a fragmented structure where nobody
# reaches the threshold and no control person has been identified
blocks_conclusion if unresolved_pct > 0

blocks_conclusion if control_prong_missing

result := {
	"rule_version": "opa/cdd.ubo@1.0.0",
	"threshold_pct": threshold,
	"effective_pct": effective_pct,
	"paths": path_value,
	"beneficial_owners": sort([p | beneficial_owners[p]]),
	"below_threshold": sort([p | below_threshold[p]]),
	"unresolved_links": unresolved_links,
	"unresolved_pct": unresolved_pct,
	"direct_holdings_sum_check": sum_check,
	"control_prong_required": control_prong_required,
	"control_prong_evidenced": control_prong_evidenced,
	"control_prong_missing": control_prong_missing,
	"blocks_conclusion": blocks_conclusion,
}
