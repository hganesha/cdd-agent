package jurisdiction.overlay

import rego.v1

# METADATA
# title: Jurisdictional overlay
# description: >
#   Applies jurisdiction-specific tightening on top of the base policy. Overlays may only make the
#   outcome stricter: an overlay that would relax a base requirement is rejected by the test suite.
# custom:
#   owner: financial-crime-policy
#   version: 1.0.0
#   scope: [jurisdiction]
#   entrypoint: false
#   related_controls: [JUR-01, JUR-02]

profile := object.get(input.case, "jurisdiction_profile", "US_FED_FINCEN_CDD")

overlays := {
	"US_FED_FINCEN_CDD": {
		"min_coverage_pct_floor": 95,
		"require_source_of_wealth": false,
		"edd_requires_mlro": false,
		"notes": "31 CFR 1010.230 ownership prong + control prong",
	},
	"EU_AMLD": {
		"min_coverage_pct_floor": 95,
		"require_source_of_wealth": true,
		"edd_requires_mlro": true,
		"notes": "AMLD5/6: SoW required for high-risk, senior management approval for EDD",
	},
	"UK_MLR_2017": {
		"min_coverage_pct_floor": 95,
		"require_source_of_wealth": true,
		"edd_requires_mlro": true,
		"notes": "MLR 2017 reg.33: enhanced measures, senior management approval",
	},
}

active := object.get(overlays, profile, overlays.US_FED_FINCEN_CDD)

min_coverage_floor := active.min_coverage_pct_floor

# The overlay name says *jurisdiction*, but the obligation a jurisdiction imposes is scoped to the
# risk it was written for: AMLD5/6 and MLR 2017 require source of wealth for **high-risk** cases and
# for EDD, not for every low-risk file that happens to sit in an EU/UK perimeter. Applying it
# unconditionally would put a customer question on a low-risk case that needs none, so the trigger
# is the jurisdiction flag *and* (EDD or a HIGH/VERY_HIGH band).
default require_source_of_wealth := false

# The overlay reads the risk its sibling bundle computed, not a level the caller asserts: a caller
# that could hand in "cdd_level: SDD" must not be able to switch off an SoW obligation.
risk_level := data.cdd.risk.result.cdd_level

risk_band := data.cdd.risk.result.band

require_source_of_wealth if {
	active.require_source_of_wealth
	risk_level == "EDD"
}

require_source_of_wealth if {
	active.require_source_of_wealth
	risk_band == "HIGH"
}

require_source_of_wealth if {
	active.require_source_of_wealth
	risk_band == "VERY_HIGH"
}

# an EDD case in an MLRO-approval jurisdiction cannot be finalised below MLRO review
default mlro_gate_required := false

mlro_gate_required if {
	active.edd_requires_mlro
	input.risk.cdd_level == "EDD"
}

result := {
	"rule_version": "opa/jurisdiction.overlay@1.0.0",
	"profile": profile,
	"active": active,
	"min_coverage_floor": min_coverage_floor,
	"require_source_of_wealth": require_source_of_wealth,
	"mlro_gate_required": mlro_gate_required,
}
