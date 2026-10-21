package cdd.screening

import rego.v1

# METADATA
# title: Screening disposition constraints
# description: >
#   Encodes what no agent may do: clear a sanctions hit. The prohibition lives in policy, not in a
#   prompt, so it cannot be talked out of an agent.
# custom:
#   owner: financial-crime-policy
#   version: 1.0.0
#   scope: [screening, escalation]
#   entrypoint: false

blocking_strengths := {s | some s in data.config.screening.blocking_strengths}

sanctions_hits := [h | some h in object.get(input.screening, "hits", []); h.kind == "SANCTIONS"]

pep_hits := [h | some h in object.get(input.screening, "hits", []); h.kind == "PEP"]

default pep_exposure := false

pep_exposure if count(pep_hits) > 0

other_hits := [h | some h in object.get(input.screening, "hits", []); h.kind != "SANCTIONS"; h.kind != "PEP"]

blocking_hits := [h | some h in sanctions_hits; blocking_strengths[h.strength]]

default requires_adjudication := false

requires_adjudication if count(blocking_hits) > 0

# constant by policy design — there is no input that makes this true
agent_may_clear_sanctions_hit := false

unavailable_lists := object.get(input.screening, "lists_unavailable", [])

required_lists := {l | some l in data.config.screening.required_lists}

queried_lists := {l | some q in object.get(input.screening, "queries", []); l := q.list}

unqueried_lists := sort([l | some l in required_lists; not queried_lists[l]])

default screening_complete := false

screening_complete if {
	count(unqueried_lists) == 0
	count(unavailable_lists) == 0
}

result := {
	"rule_version": "opa/cdd.screening@1.0.0",
	"requires_adjudication": requires_adjudication,
	"blocking_hits": blocking_hits,
	"pep_exposure": pep_exposure,
	"other_hit_count": count(other_hits),
	"agent_may_clear_sanctions_hit": agent_may_clear_sanctions_hit,
	"unavailable_lists": unavailable_lists,
	"unqueried_lists": unqueried_lists,
	"screening_complete": screening_complete,
}
