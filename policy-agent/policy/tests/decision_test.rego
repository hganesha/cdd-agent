package agent.decision_test

import rego.v1

# ── fixtures ─────────────────────────────────────────────────────────────────
clean_case := {
	"case": {"case_id": "CASE-1", "cdd_level_requested": "CDD", "jurisdiction_profile": "US_FED_FINCEN_CDD"},
	"input_hash": "h1",
	"evaluate_at": "2026-10-21T00:00:00Z",
	"factors": {},
	"risk": {"cdd_level": "SDD", "band": "LOW"},
	"screening": {
		"hits": [],
		# derived from policy config: adding a required list must not spuriously break every
		# fixture, but it must change the fixtures' screening completeness the same way production does
		"queries": [{"list": l} | some l in data.config.screening.required_lists],
		"lists_unavailable": [],
	},
	"ownership": {
		"paths": [{"person": "P1", "pcts": [60, 45], "chain": ["Interco"]}, {"person": "P1", "pcts": [10], "chain": ["Trust"]}],
		"unresolved": [],
		"direct_holdings": [{"holder": "Interco", "pct": 90}, {"holder": "Trust", "pct": 10}],
	},
	"coverage": {"elements": [{"rde_id": "RDE-01", "status": "SATISFIED"}]},
	"critic_status": "PASS",
	"injection_attempt": false,
}

decide(input_obj) := r if {
	r := data.agent.decision.result with input as input_obj
}

rs(input_obj) := r if {
	r := data.cdd.risk.result with input as input_obj
}

covr(input_obj) := r if {
	r := data.cdd.coverage.result with input as input_obj
}

us(input_obj) := r if {
	r := data.cdd.ubo.result with input as input_obj
}

# ── the invariant that matters most: no input produces PERMIT with a blocking sanctions hit ──
test_sanctions_true_hit_never_permits if {
	some strength in ["TRUE", "STRONG"]
	some cdd in ["SDD", "CDD", "EDD"]
	c := object.union(clean_case, {
		"risk": {"cdd_level": cdd, "band": "LOW"},
		"screening": object.union(clean_case.screening, {
			"hits": [{"kind": "SANCTIONS", "strength": strength, "list": "OFAC_SDN"}],
		}),
	})
	d := decide(c)
	d.outcome == "BLOCK"
	d.reasons[_] == "SANCTIONS_HIT_REQUIRES_ADJUDICATION"
}

test_agent_can_never_clear_sanctions if {
	some strength in ["TRUE", "STRONG", "WEAK", "FALSE"]
	c := object.union(clean_case, {
		"screening": object.union(clean_case.screening, {
			"hits": [{"kind": "SANCTIONS", "strength": strength, "list": "EU_CFSP"}],
		}),
	})
	d := decide(c)
	d.agent_may_clear_sanctions_hit == false
}

test_weak_sanctions_lookalike_does_not_block if {
	c := object.union(clean_case, {
		"screening": object.union(clean_case.screening, {
			"hits": [{"kind": "SANCTIONS", "strength": "WEAK", "list": "OFAC_SDN"}],
		}),
	})
	d := decide(c)
	d.outcome != "BLOCK"
	d.screening.requires_adjudication == false
}

# ── band floors: a single severe factor cannot be averaged away ──
test_pep_alone_lifts_band_to_high if {
	c := object.union(clean_case, {"factors": {"F-CUST-01": true}})
	r := rs(c)
	r.weighted_score_band == "LOW"
	r.band == "HIGH"
	r.cdd_level == "EDD"
	count(r.applied_overrides) == 1
}

test_sanctions_proximity_floor_is_very_high if {
	c := object.union(clean_case, {"factors": {"F-CUST-03": true}})
	r := rs(c)
	r.band == "VERY_HIGH"
}

test_no_floor_applies_on_a_low_factor if {
	c := object.union(clean_case, {"factors": {"F-PROD-02": true}})
	r := rs(c)
	r.band == r.weighted_score_band
	count(r.applied_overrides) == 0
}

# ── vocabulary discipline ──
test_unknown_factor_is_rejected_not_ignored if {
	c := object.union(clean_case, {"factors": {"F-MADE-UP-99": true}})
	r := rs(c)
	r.valid_input == false
	r.unknown_factors == ["F-MADE-UP-99"]
}

test_unknown_factor_blocks_the_decision if {
	c := object.union(clean_case, {"factors": {"F-ETHNICITY-01": true}})
	d := decide(c)
	d.outcome == "BLOCK"
	d.reasons[_] == "UNKNOWN_RISK_FACTOR"
}

# ── ownership arithmetic ──
test_effective_ownership_sums_across_paths if {
	c := object.union(clean_case, {"factors": {}})
	u := us(c)
	u.effective_pct.P1 == 37
	u.beneficial_owners == ["P1"]
}

test_multiplying_is_not_adding if {
	c := object.union(clean_case, {
		"ownership": {
			"paths": [{"person": "P2", "pcts": [50, 50], "chain": ["A", "B"]}],
			"unresolved": [],
			"direct_holdings": [{"holder": "A", "pct": 100}],
		},
	})
	u := us(c)
	u.effective_pct.P2 == 25
	u.beneficial_owners == ["P2"]
}

test_control_prong_required_when_fragmented if {
	c := object.union(clean_case, {
		"ownership": {
			"paths": [{"person": "P3", "pcts": [10]}],
			"unresolved": [],
			"direct_holdings": [{"holder": "X", "pct": 10}],
		},
	})
	u := us(c)
	u.beneficial_owners == []
	u.control_prong_required == true
	u.control_prong_evidenced == false
	u.control_prong_missing == true
}

test_unresolved_nominee_blocks_conclusion if {
	c := object.union(clean_case, {
		"ownership": object.union(clean_case.ownership, {
			"unresolved": [{"holder": "Sterling Nominees Ltd", "pct": 45}],
		}),
	})
	d := decide(c)
	d.outcome == "BLOCK"
	d.reasons[_] == "UNRESOLVED_BENEFICIAL_OWNERSHIP"
	d.obligations[_].kind == "CLOSE_UBO_GAP"
}

test_direct_holdings_sum_discrepancy_is_flagged if {
	c := object.union(clean_case, {
		"ownership": object.union(clean_case.ownership, {
			"direct_holdings": [{"holder": "A", "pct": 45}, {"holder": "B", "pct": 40}],
		}),
	})
	u := us(c)
	u.direct_holdings_sum_check == "DISCREPANCY"
}

# ── coverage ──
test_not_applicable_elements_are_not_counted if {
	c := object.union(clean_case, {
		"coverage": {"elements": [
			{"rde_id": "RDE-01", "status": "SATISFIED"},
			{"rde_id": "RDE-24", "status": "NOT_APPLICABLE"},
		]},
	})
	cv := covr(c)
	cv.coverage_pct == 100
	cv.applicable == 1
}

test_blocking_gap_requires_rfi_not_permit if {
	c := object.union(clean_case, {
		"coverage": {"elements": [
			{"rde_id": "RDE-01", "status": "SATISFIED"},
			{"rde_id": "RDE-07", "status": "PARTIAL", "blocks_decision": true},
			{"rde_id": "RDE-08", "status": "SATISFIED"},
		]},
	})
	d := decide(c)
	d.outcome == "REQUIRE_RFI"
	d.coverage.blocking_rde_ids == ["RDE-07"]
}

test_non_blocking_gap_does_not_change_outcome if {
	c := object.union(clean_case, {
		"coverage": {"elements": [
			{"rde_id": "RDE-01", "status": "SATISFIED"},
			{"rde_id": "RDE-18", "status": "PARTIAL"},
		]},
	})
	d := decide(c)
	d.outcome == "PERMIT"
}

# ── screening completeness ──
test_unqueried_required_list_is_incomplete if {
	c := object.union(clean_case, {
		"screening": object.union(clean_case.screening, {"queries": [{"list": "OFAC_SDN"}]}),
	})
	d := decide(c)
	d.screening.screening_complete == false
	d.reasons[_] == "SCREENING_INCOMPLETE"
	d.obligations[_].kind == "COMPLETE_SCREENING"
}

test_unavailable_connector_is_not_a_clearance if {
	c := object.union(clean_case, {
		"screening": object.union(clean_case.screening, {"lists_unavailable": ["LOCAL_REGULATOR"]}),
	})
	d := decide(c)
	d.screening.screening_complete == false
	d.outcome != "PERMIT"
}

# ── jurisdiction overlay tightens only ──
# AMLD5/6 wants source of wealth for high-risk files and for EDD — not for every EU file.
test_eu_profile_requires_source_of_wealth_for_high_risk if {
	c := object.union(clean_case, {
		"case": {"case_id": "CASE-2", "cdd_level_requested": "CDD", "jurisdiction_profile": "EU_AMLD"},
		"factors": {"F-CUST-01": true},
	})
	d := decide(c)
	d.risk.band == "HIGH"
	d.jurisdiction.require_source_of_wealth == true
	d.obligations[_].kind == "OBTAIN_SOURCE_OF_WEALTH"
}

test_eu_profile_spares_a_low_risk_cdd_file_the_sow_question if {
	c := object.union(clean_case, {
		"case": {"case_id": "CASE-2B", "cdd_level_requested": "CDD", "jurisdiction_profile": "EU_AMLD"},
	})
	d := decide(c)
	d.jurisdiction.require_source_of_wealth == false
	kinds := [k | some o in d.obligations; k := o.kind]
	not "OBTAIN_SOURCE_OF_WEALTH" in kinds
}

test_us_profile_never_requires_source_of_wealth if {
	c := object.union(clean_case, {
		"case": {"case_id": "CASE-2C", "cdd_level_requested": "EDD", "jurisdiction_profile": "US_FED_FINCEN_CDD"},
	})
	d := decide(c)
	d.jurisdiction.require_source_of_wealth == false
	kinds := [k | some o in d.obligations; k := o.kind]
	not "OBTAIN_SOURCE_OF_WEALTH" in kinds
}

test_eu_overlay_never_lowers_a_floor if {
	c := object.union(clean_case, {
		"case": {"case_id": "CASE-3", "cdd_level_requested": "CDD", "jurisdiction_profile": "EU_AMLD"},
		"factors": {"F-CUST-01": true},
	})
	d := decide(c)
	d.risk.band == "HIGH"
	d.jurisdiction.min_coverage_floor >= 95
}

# ── determinism / idempotency ──
test_same_input_same_decision_id_and_outcome if {
	a := decide(clean_case)
	b := decide(clean_case)
	a.decision_id == b.decision_id
	a.outcome == b.outcome
	a.reasons == b.reasons
}

test_decision_id_changes_with_input_hash if {
	c := object.union(clean_case, {"input_hash": "different"})
	decide(clean_case).decision_id != decide(c).decision_id
}

# ── reason vocabulary is closed ──
test_reasons_are_from_the_declared_vocabulary if {
	c := object.union(clean_case, {"factors": {"F-CUST-01": true, "F-CUST-04": true}})
	d := decide(c)
	allowed := {r | some r, _ in data.agent.decision.reason_conditions}
	count([r | some r in d.reasons; not allowed[r]]) == 0
}

test_high_risk_routes_to_edd_unit if {
	c := object.union(clean_case, {"factors": {"F-CUST-01": true}})
	d := decide(c)
	d.gates.route_to == "EDD_UNIT"
}

test_fragmented_ownership_without_control_person_blocks if {
	c := object.union(clean_case, {
		"ownership": {
			"paths": [{"person": "P9", "pcts": [22]}],
			"unresolved": [],
			"direct_holdings": [{"holder": "P9", "pct": 22}],
		},
	})
	d := decide(c)
	d.outcome == "BLOCK"
	d.reasons[_] == "CONTROL_PRONG_MISSING"
}

# ── policy pin: changing a documented threshold must be a deliberate, reviewed act ──
# If this fails, the change is not "wrong" — it means the author must also update this test and the
# SOP reference in the same proposal, so the change cannot land silently.
test_ubo_threshold_matches_sop_pin if {
	data.config.thresholds.ubo_pct == 25
}

test_required_screening_lists_pin if {
	count(data.config.screening.required_lists) >= 5
}
