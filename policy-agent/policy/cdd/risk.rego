package cdd.risk

import rego.v1

# METADATA
# title: CDD risk rating
# description: >
#   Deterministic weighted risk banding with policy band floors. The weighted score is computed per
#   dimension and combined; the final band is the stricter of the score band and every floor
#   triggered by a severe single factor, so one severe factor cannot be averaged away.
# custom:
#   owner: financial-crime-policy
#   version: 1.0.0
#   scope: [risk-rating, cdd-level]
#   entrypoint: false
#   review_cadence: quarterly
#   related_controls: [CDD-04, CDD-05]

band_order := ["LOW", "MEDIUM", "HIGH", "VERY_HIGH"]

band_index := {band: i | some i, band in band_order}

permitted_factors := {id | some id, _ in data.config.risk.factors}

# a factor outside the permitted vocabulary is a hard error, never silently ignored
unknown_factors := sort([f | some f, _ in input.factors; not permitted_factors[f]])

default valid_input := false

valid_input if count(unknown_factors) == 0

triggered_set := {id | some id, on in input.factors; on == true}

triggered_sorted := sort([id | triggered_set[id]])

dimension_points[dim] := score if {
	some d in object.keys(data.config.risk.weights)
	dim := d
	score := sum([f.points | some id, f in data.config.risk.factors; triggered_set[id]; f.dimension == d])
}

dimension_score[dim] := score if {
	raw := dimension_points[dim]
	score := min([100, raw])
}

contribution[dim] := value if {
	some d in object.keys(data.config.risk.weights)
	dim := d
	value := dimension_score[d] * data.config.risk.weights[d]
}

weighted_score := score if {
	total := sum([v | some _, v in contribution])
	score := round(total * 100) / 100
}

score_band := band if {
	some band, rng in data.config.risk.bands
	rng.min <= weighted_score
	weighted_score <= rng.max
}

applied_overrides := [o | some o in data.config.risk.band_floors; triggered_set[o.if_factor]]

override_indices := [i | some o in applied_overrides; i := band_index[o.min_band]]

band := band if {
	candidates := array.concat([band_index[score_band]], override_indices)
	strongest := max(candidates)
	band := band_order[strongest]
}

trace := [row |
	some id in triggered_sorted
	f := data.config.risk.factors[id]
	row := {
		"factor_id": id,
		"name": f.name,
		"dimension": f.dimension,
		"points": f.points,
		"weight": data.config.risk.weights[f.dimension],
		"policy_rule_id": f.rule,
	}
]

result := {
	"engine_version": "opa/cdd.risk@1.0.0",
	"policy_version": data.config.policy_version,
	"score": weighted_score,
	"weighted_score_band": score_band,
	"band": band,
	"applied_overrides": applied_overrides,
	"dimension_points": dimension_points,
	"dimension_score": dimension_score,
	"cdd_level": data.config.risk.cdd_level[band],
	"review_cadence": data.config.risk.review_cadence[band],
	"trace": trace,
	"unknown_factors": unknown_factors,
	"valid_input": valid_input,
}
