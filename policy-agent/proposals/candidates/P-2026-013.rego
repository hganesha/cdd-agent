package jurisdiction.overlay.p2026013

import rego.v1

# Candidate rule: SoW is an enhanced measure, so it attaches to the enhanced tiers.
# Kept as a standalone predicate for review; the accepted change is applied in
# policy/jurisdiction/overlay.rego.

default requires_source_of_wealth := false

requires_source_of_wealth if {
	active_requires
	risk_level == "EDD"
}

requires_source_of_wealth if {
	active_requires
	risk_band == "HIGH"
}

requires_source_of_wealth if {
	active_requires
	risk_band == "VERY_HIGH"
}

active_requires if data.jurisdiction.overlay.active.require_source_of_wealth

risk_level := data.cdd.risk.result.cdd_level

risk_band := data.cdd.risk.result.band
