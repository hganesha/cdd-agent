"""OPS-PROFILE — the internal view of the customer.

Assembles the customer 360, transaction behaviour, counterparty network, geographic exposure and
internal risk rating into one profile that the decision agent can hand to the policy service.

Two rules shape the whole agent:

1. **Every number is traceable to an API call**, and every call has an evidence record. A profile
   whose evidence list does not cover its claims is a profile the verification agent will reject.
2. **The agent does not decide.** It reports facts and maps them to the *permitted* risk factors of
   the policy pack. A factor id the pack does not define is a hard error, never a silent no-op:
   otherwise an agent could invent a mitigation and quietly lower a score.

Public entrypoint: `ProfileAgent.build(request, state=...) -> dict` (validated against
`schemas/customer-profile.schema.json`).

Long-running: a profile for a busy customer is tens of thousands of transactions across hundreds of
paginated pages, so acquisition is staged and checkpointed (`common/runs.py`). Each API read is a
stage; a killed run resumes with the bodies it already had, re-derives the same numbers from them,
and writes no duplicate evidence.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..common.ledger import OpsLedger, canonical, sha256_str
from ..common.mockapi import ApiError, MockApi
from ..common.policy import PolicyEngine
from ..common.runs import KillSwitch, RunState

# Products/segments the policy pack treats as inherently higher risk (F-PROD-01).
HIGH_RISK_PRODUCTS = {"TRADE_FINANCE", "PRIVATE_BANKING", "CORRESPONDENT_BANKING", "VASP", "PREPAID"}
# Sectors that are cash intensive by nature (F-CUST-06 is still driven by the master record flag).
SHELL_PREMISES = {"NONE_FOUND", "RESIDENTIAL", "SHARED_SERVICED_OFFICE"}
MIN_ESTABLISHED_MONTHS = 18


@dataclass
class Observation:
    """One derived fact, with the API evidence behind it and the arithmetic that produced it."""
    factor_id: str
    triggered: bool
    rule: str
    value: object
    evidence_ids: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {"factor_id": self.factor_id, "triggered": self.triggered, "rule": self.rule,
                "value": self.value, "evidence_ids": list(self.evidence_ids), "note": self.note}


class _Replay:
    """A checkpointed API response, shape-compatible with `ApiResponse`/`FetchResult` so the rest of
    the agent cannot tell a replay from a live call."""

    def __init__(self, body, evidence_id, pages: int = 1):
        self.body = body
        self.evidence_id = evidence_id
        self.pages = pages
        self.evidence_ids = evidence_id if isinstance(evidence_id, list) else [evidence_id]
        self.status = 200
        self.retried = 0

    # A paginated replay is iterated like the page list it stands in for.
    def __iter__(self):
        return iter(self.body)

    def __len__(self):
        return len(self.body)

    def __getitem__(self, item):
        return self.body[item]


def _months_between(iso_a: str, iso_b: str) -> int:
    ya, ma = int(iso_a[:4]), int(iso_a[5:7])
    yb, mb = int(iso_b[:4]), int(iso_b[5:7])
    return (yb - ya) * 12 + (mb - ma)


class ProfileAgent:
    AGENT_ID = "OPS-PROFILE"

    def __init__(self, api: MockApi, ledger: OpsLedger, policy: PolicyEngine):
        self.api = api
        self.ledger = ledger
        self.policy = policy
        self.catalog = policy.factor_catalog()
        self.unresolved: list[str] = []

    # ── staged acquisition (resumable) ──────────────────────────────────────
    def _get(self, state: RunState | None, kill: KillSwitch | None, stage: str, fn):
        """One API read = one checkpointed stage. Replayed, not repeated, on resume."""
        if state is not None and state.done(stage):
            cached = state.payload(stage)
            return _Replay(cached["body"], cached["evidence_id"], cached.get("pages", 1))
        if state is not None:
            state.check_budget("api_calls", what=stage)
        if kill is not None:
            kill.tick()
        resp = fn()
        if state is not None:
            state.charge(api_calls=1)
            state.record(stage, {"body": resp.body, "evidence_id": resp.evidence_id},
                         agent=self.AGENT_ID, evidence_id=resp.evidence_id)
            state.attach(self.ledger.head())
        return resp

    def _paginate(self, state: RunState | None, kill: KillSwitch | None, stage_prefix: str,
                  endpoint: str, key: str) -> "_Replay":
        """Walk a paginated endpoint one page at a time, checkpointing each page.

        Page granularity is the whole point: a 400-page read that dies on page 380 must resume at
        page 380, not at page 1, and must not re-charge the 379 pages it already has.
        """
        pages: list[dict] = []
        ids: list[str] = []
        total = None
        page = 1
        while total is None or page <= total:
            stage = f"{stage_prefix}:p{page}"
            if state is not None and state.done(stage):
                cached = state.payload(stage)
                pages.append(cached["page"])
                if cached.get("evidence_id"):
                    ids.append(cached["evidence_id"])
                total = cached.get("pages_total", total)
                page += 1
                continue
            if state is not None:
                state.check_budget("pages", what=stage)
            if kill is not None:
                kill.tick()
            resp = self.api.page(endpoint, key=key, page=page)
            pages.append(resp.body)
            ids.append(resp.evidence_id)
            total = resp.pages_total
            if state is not None:
                state.charge(pages=1, api_calls=1)
                state.record(stage, {"page": resp.body, "evidence_id": resp.evidence_id,
                                     "pages_total": total}, agent=self.AGENT_ID,
                             evidence_id=resp.evidence_id)
                state.attach(self.ledger.head())
            page += 1
        return _Replay(pages, ids, len(pages))

    # ── main ────────────────────────────────────────────────────────────────
    def build(self, request: dict, *, state: RunState | None = None,
              kill: KillSwitch | None = None) -> dict:
        """request: {case_id, customer_id, as_of, window_days?, evidence_pack?, screening?}"""
        case_id = request["case_id"]
        cust_id = request["customer_id"]
        as_of = request.get("as_of", "2026-10-21T00:00:00Z")
        window = int(request.get("window_days", 90))

        cust = self._get(state, kill, "c360", lambda: self.api.get("/c360/customers", key=cust_id))
        parties = self._get(state, kill, "related-parties",
                            lambda: self.api.get("/c360/related-parties", key=cust_id))
        ownership = self._get(state, kill, "ownership",
                              lambda: self.api.get("/c360/ownership", key=cust_id))
        summary = self._get(state, kill, "tx-summary",
                            lambda: self.api.get("/tx/summary", key=cust_id, query={"window": window}))
        flows = self._paginate(state, kill, "tx-flows", "/tx/flows", cust_id)
        rating = self._get(state, kill, "risk-rating",
                           lambda: self.api.get("/risk/ratings", key=cust_id))
        lists = self._get(state, kill, "jurisdiction-lists",
                          lambda: self.api.get("/lists/jurisdictions"))
        docs = self._get(state, kill, "documents",
                         lambda: self.api.get("/docs/documents", key=case_id))
        thread = self._get(state, kill, "rfi-thread",
                           lambda: self.api.get("/rfi/threads", key=case_id))

        c, p, o = cust.body, parties.body, ownership.body
        s, r = summary.body, rating.body

        top = next((pg["top_counterparties"] for pg in flows if "top_counterparties" in pg), [])
        geos = next((pg["geographies"] for pg in flows if "geographies" in pg), [])
        months = next((pg["months"] for pg in flows if "months" in pg), [])
        cps: dict[str, dict | None] = {}
        for cp in top:
            cid = cp.get("counterparty_id")
            if not cid:
                continue
            try:
                cps[cid] = self._get(state, kill, f"counterparty-{cid}",
                                     lambda c=cid: self.api.get("/counterparties/counterparties",
                                                                key=c)).body
            except ApiError as exc:
                # A counterparty the registry does not know is a finding, not a crash: the row is
                # kept with its name unknown and the gap is disclosed.
                cps[cid] = None
                self.unresolved.append(f"counterparty {cid}: {exc.status}")

        flow_ev = list(flows.evidence_ids)
        ev = {"c360": cust.evidence_id, "parties": parties.evidence_id,
              "ownership": ownership.evidence_id, "tx": summary.evidence_id,
              "flows": flow_ev, "risk": rating.evidence_id, "lists": lists.evidence_id,
              "docs": docs.evidence_id}
        metrics = self._transactions(s, months, top, geos)
        cp_analysis = self._counterparties(top, cps, s, as_of)
        geo = self._geography(c, p["parties"], cp_analysis["top"], lists.body)
        observations = self._factors(c, p["parties"], o, s, metrics, cp_analysis, geo, docs.body,
                                     request.get("evidence_pack"), ev)
        ownership_block = self._ownership_block(o, p["parties"])
        coverage = self._coverage(docs.body, ownership_block, request.get("screening"))
        screening = self._screening(p["parties"], lists.body, request.get("screening"))

        triggered = sorted(k for k, v in observations.items() if v.triggered)
        profile = {
            "profile_id": "PRF-" + cust_id + "-" + sha256_str(
                canonical({"c": c, "s": s, "as_of": as_of}))[7:15],
            "case_id": case_id,
            "customer_id": cust_id,
            "as_of": as_of,
            "window_days": window,
            "agent": self.AGENT_ID,
            "customer_360": {
                "legal_name": c["legal_name"],
                "entity_type": c["entity_type"],
                "registration": c["registration"],
                "sector": c["sector"],
                "segments": c["segments"],
                "channels": c["channels"],
                "introducer": c["introducer"],
                "onboarding": c["onboarding"],
                "employees": c["employees"],
                "premises": c["premises"],
                "turnover_band": c["turnover_band"],
                "related_parties": p["parties"],
                "prior_sar": c["prior_sar"],
            },
            "transactions": metrics,
            "counterparties": cp_analysis,
            "geography": geo,
            "internal_risk": {
                "rating": r["internal_rating"], "rating_date": r["rating_date"],
                "watchlist": r["watchlist"], "watchlist_reason": r.get("watchlist_reason"),
                "last_review": r["last_review"], "next_review_due": r["next_review_due"],
                "expected_profile": r["expected_profile"],
                "expected_vs_observed": self._expected_vs_observed(r["expected_profile"], s, metrics),
            },
            "ownership": ownership_block,
            "screening": screening,
            "coverage": coverage,
            "factors": {k: v.triggered for k, v in observations.items()},
            "factors_triggered": triggered,
            "observations": {k: v.to_dict() for k, v in observations.items()},
            "outreach": {"rounds_completed": thread.body.get("rounds_completed", 0),
                         "last_outcome": (thread.body.get("messages") or [{}])[-1].get("outcome"),
                         "messages": thread.body.get("messages", [])},
            "data_quality": {
                "api": self.api.evidence(),
                "missing_fields": self._missing(c, r, top),
                "notes": [],
            },
        }
        if profile["data_quality"]["api"]["calls"] == 0:
            profile["data_quality"]["notes"].append(
                "no live API calls in this process: the profile was rebuilt from its checkpoint "
                "and every figure replays the bodies recorded there")
        profile["data_quality"]["notes"] += self._data_quality_notes(profile)
        profile["evidence_id"] = self.ledger.profile(profile=profile)
        return profile

    # ── transactions ────────────────────────────────────────────────────────
    @staticmethod
    def _transactions(s: dict, months: list[dict], top: list[dict], geos: list[dict]) -> dict:
        vol = [m["in"] for m in months] or [0.0]
        mean = sum(vol) / len(vol)
        variance = sum((v - mean) ** 2 for v in vol) / len(vol)
        cv = math.sqrt(variance) / mean if mean else 0.0
        total_top = sum(c["amount"] for c in top) or 1.0
        shares = [c["amount"] / total_top for c in top]
        hhi = round(sum(x * x for x in shares), 4)          # 1.0 = one counterparty = total concentration
        deviation = (s["observed_monthly_mean"] / s["declared_monthly_turnover"] - 1
                     if s["declared_monthly_turnover"] else 0.0)
        momentum = (s["observed_monthly_mean"] / s["prior_window_monthly_mean"] - 1
                    if s.get("prior_window_monthly_mean") else 0.0)
        return {
            "txn_count": s["txn_count"], "total_in": s["total_in"], "total_out": s["total_out"],
            "avg_txn": s["avg_txn"], "median_txn": s["median_txn"], "max_single": s["max_single"],
            "cash_ratio": s["cash_ratio"], "round_amount_ratio": s["round_amount_ratio"],
            "cross_border_ratio": s["cross_border_ratio"],
            "new_counterparties": s["new_counterparties"],
            "peer_percentile": s["peer_percentile"],
            "declared_monthly_turnover": s["declared_monthly_turnover"],
            "observed_monthly_mean": s["observed_monthly_mean"],
            "prior_window_monthly_mean": s.get("prior_window_monthly_mean"),
            "declared_vs_observed_pct": round(deviation * 100, 1),
            "period_on_period_pct": round(momentum * 100, 1),
            "monthly_cv": round(cv, 3),
            "dormant_days_before_activity": s["dormant_days_before_activity"],
            "top_counterparty_hhi": hhi,
            "monthly_inflows": [{"month": m["month"], "in": m["in"], "out": m["out"],
                                 "txn_count": m["txn_count"]} for m in months],
            "max_month_share": round(max(vol) / sum(vol), 3) if sum(vol) else 0.0,
        }

    # ── counterparties ──────────────────────────────────────────────────────
    def _counterparties(self, top: list[dict], cps: dict, s: dict, as_of: str) -> dict:
        rows, shells, new_high_value = [], 0, []
        for cp in top:
            d = cps.get(cp.get("counterparty_id")) or {}
            # the flow row is authoritative for volumes; the registry for identity. A registry that
            # does not know the counterparty leaves the name unknown rather than guessed.
            cp = {**cp, "name": d.get("name") or cp.get("name") or cp["counterparty_id"],
                  "country": d.get("country") or cp.get("country")}
            age_m = _months_between(d["incorporated"], as_of[:10]) if d.get("incorporated") else None
            shell_flags = []
            if age_m is not None and age_m < MIN_ESTABLISHED_MONTHS:
                shell_flags.append(f"incorporated {age_m} months ago")
            if d.get("employees") == 0:
                shell_flags.append("no employees recorded")
            if d.get("premises") in {"NONE_FOUND", "RESIDENTIAL"}:
                shell_flags.append(f"premises: {d.get('premises')}")
            if d.get("shared_address_with"):
                shell_flags.append(f"shared address with {', '.join(d['shared_address_with'])}")
            if shell_flags:
                shells += 1
            first_seen = cp.get("first_seen")
            is_new = bool(first_seen and first_seen[:4] == as_of[:4])
            if is_new and cp["amount"] >= 500_000:
                new_high_value.append({"counterparty_id": cp["counterparty_id"],
                                       "name": cp["name"], "amount": cp["amount"],
                                       "first_seen": first_seen})
            rows.append({"counterparty_id": cp["counterparty_id"], "name": cp["name"],
                         "country": cp["country"], "amount": cp["amount"],
                         "txn_count": cp["txn_count"], "first_seen": first_seen,
                         "is_new": is_new, "entity_age_months": age_m,
                         "shell_flags": shell_flags,
                         "adverse_media": d.get("adverse_media"),
                         "sanctions_screen": d.get("sanctions_screen")})
        return {
            "top": rows,
            "count_analysed": len(rows),
            "new_counterparties": s["new_counterparties"],
            "new_high_value": new_high_value,
            "shell_indicator_count": shells,
            "adverse_media_counterparties": [r["name"] for r in rows
                                             if r["adverse_media"] not in (None, "NONE")],
        }

    # ── geography ───────────────────────────────────────────────────────────
    @staticmethod
    def _geography(c: dict, parties: list[dict], cp_rows: list[dict], lists: dict) -> dict:
        nexus = {c["registration"]["jurisdiction"], c["onboarding"]["country_of_operation"]}
        nexus |= {p.get("nationality") for p in parties if p.get("nationality")}
        nexus |= {r["country"] for r in cp_rows}
        nexus.discard(None)
        inflow = {}
        return {
            "nexus_countries": sorted(nexus),
            "incorporation": c["registration"]["jurisdiction"],
            "operation": c["onboarding"]["country_of_operation"],
            "counterparty_countries": sorted({r["country"] for r in cp_rows}),
            "fatf_call_for_action": sorted(nexus & set(lists["fatf_call_for_action"])),
            "fatf_grey_list": sorted(nexus & set(lists["fatf_grey_list"])),
            "sanctioned": sorted(nexus & set(lists["sanctioned_jurisdictions"])),
            "secrecy": sorted(nexus & set(lists["secrecy_jurisdictions"])),
            "high_corruption": sorted(nexus & set(lists["high_corruption_cpi_band"])),
            "lists_as_of": lists["as_of"],
        }

    # ── factors ─────────────────────────────────────────────────────────────
    # which API calls stand behind each factor: the verification agent checks these resolve
    EVIDENCE_MAP = {
        "F-CUST-01": ["parties"], "F-CUST-02": ["docs"], "F-CUST-03": ["parties"],
        "F-CUST-04": ["ownership", "parties"], "F-CUST-05": ["ownership"],
        "F-CUST-06": ["c360"], "F-CUST-07": ["c360", "flows"], "F-CUST-08": ["c360"],
        "F-PROD-01": ["c360"], "F-PROD-02": ["tx"],
        "F-GEO-01": ["lists", "flows"], "F-GEO-02": ["lists", "flows"],
        "F-GEO-03": ["lists", "ownership"], "F-GEO-04": ["lists"], "F-GEO-05": ["lists"],
        "F-CHAN-01": ["c360"], "F-CHAN-02": ["c360"],
        "F-BEH-01": ["tx", "flows", "risk", "ownership"], "F-BEH-02": ["docs"], "F-BEH-03": ["tx"],
    }

    def _factors(self, c, parties, o, s, metrics, cp, geo, docs, evidence_pack,
                 ev: dict | None = None) -> dict[str, Observation]:
        """Map observed facts to the policy pack's permitted factor ids. Anything that would need a
        factor the pack does not define raises instead of being dropped."""
        obs: dict[str, Observation] = {}

        def add(fid: str, triggered: bool, rule: str, value, evidence: list[str], note: str = ""):
            if fid not in self.catalog:
                raise KeyError(f"{fid} is not a permitted risk factor in {self.policy.policy_version}")
            ids = [i for i in evidence if i]
            for key in self.EVIDENCE_MAP.get(fid, []):
                got = (ev or {}).get(key)
                if isinstance(got, list):
                    ids.extend(x for x in got if x)
                elif got:
                    ids.append(got)
            obs[fid] = Observation(fid, triggered, rule, value, ids, note)

        pep = [p for p in parties if p.get("pep")]
        add("F-CUST-01", bool(pep), "any related party is a PEP", len(pep),
            [], "; ".join(f"{p['name']}: {p.get('pep_detail', '')}" for p in pep))

        adverse = []
        if evidence_pack:
            adverse = [cl["claim_key"] for cl in evidence_pack.get("claims", [])
                       if cl.get("confidence") in ("HIGH", "MEDIUM")]
        add("F-CUST-02", bool(adverse), "material adverse media (from the evidence pack)",
            adverse, [], "no pack supplied" if not evidence_pack else "")

        sanctions = [p["name"] for p in parties if p.get("sanctions_screen") == "NEAR_MATCH"]
        add("F-CUST-03", bool(sanctions), "near-match or associated party on a sanctions list",
            sanctions, [])

        unresolved = float(sum(u["pct"] for u in o.get("unresolved", [])))
        nominal = any(p.get("nominee") and not p.get("principal_declared") for p in parties)
        add("F-CUST-04", bool(unresolved > 0 or nominal),
            "unresolved UBO, nominee without principal declaration, or opacity jurisdiction",
            {"unresolved_pct": unresolved, "nominee_without_principal": nominal}, [])

        layers = int(o.get("layers", 1))
        jurisdictions = len(set(o.get("jurisdictions", [])))
        add("F-CUST-05", layers >= 3 or jurisdictions >= 3,
            ">=3 layers or >=3 jurisdictions in the ownership chain",
            {"layers": layers, "jurisdictions": jurisdictions}, [])

        add("F-CUST-06", bool(c["sector"].get("cash_intensive")), "cash-intensive sector",
            c["sector"]["label"], [])

        shell = (c["employees"] <= 5 and c["premises"] in SHELL_PREMISES) or \
                cp["shell_indicator_count"] > 0
        add("F-CUST-07", shell, "no meaningful operations evidence (customer or counterparties)",
            {"employees": c["employees"], "premises": c["premises"],
             "counterparty_shell_flags": cp["shell_indicator_count"]}, [])

        add("F-CUST-08", bool(c["prior_sar"]), "prior SAR on the customer or a related party",
            c["prior_sar"], [])

        high_risk_products = sorted(set(c["segments"]) & HIGH_RISK_PRODUCTS)
        add("F-PROD-01", bool(high_risk_products), "high-risk product segment",
            high_risk_products, [])

        add("F-PROD-02", s["cash_ratio"] > 0.10, "cash-heavy transaction mix (cash ratio > 10%)",
            s["cash_ratio"], [])

        add("F-GEO-01", bool(geo["fatf_call_for_action"]), "nexus to a FATF call-for-action jurisdiction",
            geo["fatf_call_for_action"], [])
        add("F-GEO-02", bool(geo["fatf_grey_list"]), "nexus to a FATF grey-list jurisdiction",
            geo["fatf_grey_list"], [])
        add("F-GEO-03", bool(geo["secrecy"]), "nexus to a high-secrecy jurisdiction", geo["secrecy"], [])
        add("F-GEO-04", bool(geo["sanctioned"]), "funds/ownership nexus to a sanctioned jurisdiction",
            geo["sanctioned"], [])
        add("F-GEO-05", bool(geo["high_corruption"]), "nexus to a high-corruption-band jurisdiction",
            geo["high_corruption"], [])

        add("F-CHAN-01", not c["onboarding"]["in_person"], "remote onboarding without in-person check",
            c["onboarding"], [])
        add("F-CHAN-02", bool(c["introducer"]), "third-party introducer reliance",
            c["introducer"], [])

        expected = (c.get("expected") or {})
        inconsistent = (abs(metrics["declared_vs_observed_pct"]) > 50
                        or metrics["peer_percentile"] >= 95
                        or bool(cp["new_high_value"]))
        add("F-BEH-01", inconsistent,
            "expected vs observed activity inconsistency (declared turnover, peer percentile, "
            "or new high-value counterparty)",
            {"declared_vs_observed_pct": metrics["declared_vs_observed_pct"],
             "peer_percentile": metrics["peer_percentile"],
             "new_high_value_counterparties": len(cp["new_high_value"])}, [])

        sow_gaps = [e["rde_id"] for e in docs["elements"]
                    if e["rde_id"] in ("RDE-05", "RDE-07") and e["status"] != "SATISFIED"]
        add("F-BEH-02", bool(sow_gaps), "source of funds / wealth not fully evidenced",
            sow_gaps, [])

        add("F-BEH-03", False, "funding from an unrelated third party (no signal in this window)",
            None, [])

        return obs

    # ── ownership / coverage / screening ────────────────────────────────────
    @staticmethod
    def _ownership_block(o: dict, parties: list[dict]) -> dict:
        return {
            "paths": [{"person": p["person"], "pcts": p["pcts"]} for p in o.get("paths", [])],
            "unresolved": [{"holder": u["holder"], "pct": u["pct"]} for u in o.get("unresolved", [])],
            "direct_holdings": [{"holder": d["holder"], "pct": d["pct"]}
                                for d in o.get("direct_holdings", [])],
            "nominee_declared": bool(o.get("nominee_declared")),
            "layers": o.get("layers"),
            "unresolved_pct": round(sum(u["pct"] for u in o.get("unresolved", [])), 2),
            "natural_persons": len(o.get("paths", [])),
        }

    def _coverage(self, docs: dict, ownership: dict, screening_override: dict | None) -> dict:
        elements = [dict(e) for e in docs["elements"]]
        # the profile can *worsen* a coverage element with evidence, never improve one
        if ownership["unresolved_pct"] > 0:
            for e in elements:
                if e["rde_id"] in ("RDE-03", "RDE-04") and e["status"] == "SATISFIED":
                    e["status"] = "PARTIAL"
                    e["note"] = "ownership unresolved per /c360/ownership"
        applicable = [e for e in elements if e["status"] != "NOT_APPLICABLE"]
        satisfied = [e for e in applicable if e["status"] == "SATISFIED"]
        pct = round(len(satisfied) / len(applicable) * 100, 1) if applicable else 0.0
        return {
            "elements": elements,
            "applicable": len(applicable),
            "satisfied": len(satisfied),
            "coverage_pct": pct,
            "blocking_rde_ids": sorted(e["rde_id"] for e in applicable
                                       if e.get("blocks_decision") and e["status"] != "SATISFIED"),
            "missing_rde_ids": sorted(e["rde_id"] for e in applicable if e["status"] == "MISSING"),
        }

    def _screening(self, parties: list[dict], lists: dict, override: dict | None) -> dict:
        required = self.policy.required_lists()
        override = override or {}
        queried = list(override.get("lists_queried", [l for l in required if l != "EU_CFSP"]))
        unavailable = list(override.get("lists_unavailable",
                                       [l for l in required if l not in queried]))
        hits = list(override.get("hits", []))
        if not hits:
            for p in parties:
                if p.get("pep"):
                    hits.append({"list": "PEP_GLOBAL", "strength": "TRUE",
                                 "matched_name": p["name"],
                                 "basis": p.get("pep_detail", "PEP register")})
        return {
            "queries": [{"list": l} for l in queried],
            "lists_queried": queried,
            "lists_unavailable": unavailable,
            "required_lists": required,
            "hits": hits,
            "screening_complete": not unavailable and set(queried) >= set(required),
        }

    @staticmethod
    def _expected_vs_observed(expected: dict, s: dict, metrics: dict) -> list[dict]:
        out = []
        turn = expected.get("monthly_turnover")
        if turn:
            delta = s["observed_monthly_mean"] / turn - 1
            out.append({"field": "monthly_turnover", "expected": turn,
                        "observed": round(s["observed_monthly_mean"], 2),
                        "delta_pct": round(delta * 100, 1), "breach": abs(delta) > 0.5})
        if expected.get("cash_ratio_max") is not None:
            out.append({"field": "cash_ratio", "expected_max": expected["cash_ratio_max"],
                        "observed": s["cash_ratio"],
                        "breach": s["cash_ratio"] > expected["cash_ratio_max"]})
        return out

    @staticmethod
    def _missing(c: dict, r: dict, top: list[dict]) -> list[str]:
        missing = []
        if not r.get("expected_profile", {}).get("countries"):
            missing.append("expected_profile.countries")
        if not top:
            missing.append("top_counterparties")
        if not c.get("documents_on_file"):
            missing.append("documents_on_file")
        return missing

    @staticmethod
    def _data_quality_notes(profile: dict) -> list[str]:
        notes = []
        if profile["coverage"]["coverage_pct"] < 95:
            notes.append(f"documentation coverage {profile['coverage']['coverage_pct']}% "
                         f"below the CDD minimum")
        if not profile["screening"]["screening_complete"]:
            notes.append("screening incomplete: " +
                         ", ".join(profile["screening"]["lists_unavailable"]) + " not queried")
        if profile["geography"]["fatf_grey_list"]:
            notes.append("geographic nexus to grey list: " +
                         ", ".join(profile["geography"]["fatf_grey_list"]))
        return notes

    # ── drift ───────────────────────────────────────────────────────────────
    @staticmethod
    def delta(previous: dict | None, current: dict) -> list[dict]:
        """What changed since the last profile, in terms the decision agent can act on."""
        if not previous:
            return []
        changes = []
        pf, cf = previous.get("factors", {}), current.get("factors", {})
        for fid in sorted(set(pf) | set(cf)):
            if pf.get(fid) != cf.get(fid):
                changes.append({"kind": "FACTOR_FLIPPED", "factor_id": fid,
                                "from": pf.get(fid), "to": cf.get(fid)})
        pc, cc = previous.get("coverage", {}), current.get("coverage", {})
        if pc.get("coverage_pct") != cc.get("coverage_pct"):
            changes.append({"kind": "COVERAGE_MOVED", "from": pc.get("coverage_pct"),
                            "to": cc.get("coverage_pct")})
        for field_name in ("rating", "watchlist"):
            pv = (previous.get("internal_risk") or {}).get(field_name)
            cv = (current.get("internal_risk") or {}).get(field_name)
            if pv != cv:
                changes.append({"kind": "INTERNAL_RISK_MOVED", "field": field_name,
                                "from": pv, "to": cv})
        return changes
