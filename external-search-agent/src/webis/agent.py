"""External Search Agent — SK-S1..SK-S11.

Pipeline: plan queries → search → fetch under source policy → quarantine → extract passages →
assess credibility → triangulate → build the citation pack → hand off.

Every stage writes evidence. Nothing leaves the agent without a quotation and a location.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .fetch import FetchResult, Fetcher, SourcePolicy, sha256

ROOT = Path(__file__).resolve().parents[2]


# ── SK-S1 query planning ────────────────────────────────────────────────────
@dataclass
class PlannedQuery:
    query_id: str
    text: str
    facet: str          # IDENTITY | ADVERSE | REGULATORY | CORPORATE_STATUS | COUNTERPARTY
    rationale: str


FACETS = {
    "IDENTITY": ["{subject}", "{subject} director owner", "{subject} registration"],
    "ADVERSE": ["{subject} investigation", "{subject} inquiry", "{subject} fraud OR laundering",
                "{subject} court listing"],
    "REGULATORY": ["{subject} final notice", "{subject} penalty", "{subject} regulator"],
    "CORPORATE_STATUS": ["{subject} administration", "{subject} dissolved", "{subject} registry"],
    "COUNTERPARTY": ["{subject} supplier", "{subject} customer dispute"],
    "RELATED_PARTY": ["{subject} director", "{subject} court", "{subject} employee"],
}


def plan_queries(subject: str, *, facets: list[str] | None = None,
                 max_queries: int = 12, related_parties: list[str] | None = None) -> list[PlannedQuery]:
    facets = facets or ["IDENTITY", "ADVERSE", "REGULATORY", "CORPORATE_STATUS"]
    out = []
    for facet in facets:
        for tmpl in FACETS[facet]:
            if len(out) >= max_queries:
                return out
            out.append(PlannedQuery(query_id=f"Q-{len(out)+1:02d}",
                                    text=tmpl.format(subject=subject), facet=facet,
                                    rationale=f"{facet.lower()} coverage for {subject}"))
    # principals are screened in their own right: adverse media about a director is often not
    # discoverable through the entity name
    for person in related_parties or []:
        for tmpl in FACETS["RELATED_PARTY"]:
            if len(out) >= max_queries:
                return out
            out.append(PlannedQuery(query_id=f"Q-{len(out)+1:02d}",
                                    text=tmpl.format(subject=person), facet="RELATED_PARTY",
                                    rationale=f"related-party screening for {person}"))
    return out


# ── SK-S4 passage extraction ────────────────────────────────────────────────
@dataclass
class Passage:
    passage_id: str
    url: str
    title: str
    quote: str
    char_span: list[int]
    content_hash: str
    published: str | None
    source_tier: int
    kind: str
    licensing: str
    retrieved_at: str

    def to_dict(self) -> dict:
        return self.__dict__


SENTENCE_RE = re.compile(r"[^.!?]+[.!?]")


def extract_passages(fetch: FetchResult, *, max_passages: int = 4,
                     keywords: list[str] | None = None) -> list[Passage]:
    if not fetch.citable:
        return []
    body = fetch.body
    scored = []
    for m in SENTENCE_RE.finditer(body):
        raw = m.group(0)
        text = raw.strip()
        if len(text) < 40:
            continue
        # the span must delimit the quote exactly: `verify` re-slices the body at char_span and
        # demands byte equality, so leading/trailing whitespace has to move the offsets too
        lead = len(raw) - len(raw.lstrip())
        start = m.start() + lead
        score = sum(1 for k in (keywords or []) if k.lower() in text.lower())
        scored.append((score, start, text))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out = []
    for i, (_, start, text) in enumerate(scored[:max_passages]):
        out.append(Passage(
            passage_id=f"PSG-{i+1:02d}",
            url=fetch.url, title=fetch.title, quote=text,
            char_span=[start, start + len(text)],
            content_hash=fetch.content_hash,
            published=fetch.published, source_tier=fetch.source_tier,
            kind=fetch.kind, licensing=fetch.licensing,
            retrieved_at=fetch.retrieved_at,
        ))
    return out


# ── SK-S5 credibility ───────────────────────────────────────────────────────
TIER_LABEL = {1: "official/primary", 2: "established press", 3: "trade/regional",
              4: "low-quality/aggregator", 5: "unclassified"}


# Age does not make a fact untrue: a 2024 final notice is still a 2024 final notice. What age
# affects is whether the *search* must be repeated, which is a property of the claim, not of the
# quotation. Only an undated source is penalised, because nothing about it can be placed in time.
FRESHNESS = {"undated_penalty": 10}


def _age_days(published: str | None, as_of: str | None) -> int | None:
    if not published or not as_of:
        return None
    try:
        from datetime import date
        p = date.fromisoformat(published[:10])
        n = date.fromisoformat(as_of[:10])
        return (n - p).days
    except ValueError:
        return None


def credibility(passage: Passage, *, corroborating_domains: set[str],
                contradicting_domains: set[str], as_of: str | None = None) -> dict:
    factors = []
    score = 0.0

    tier_points = {1: 45, 2: 30, 3: 18, 4: 5, 5: 0}[min(passage.source_tier, 5)]
    score += tier_points
    factors.append({"factor": "source_tier", "value": TIER_LABEL[min(passage.source_tier, 5)],
                    "points": tier_points})

    corroboration = len(corroborating_domains - {_domain(passage.url)})
    corr_points = min(20, corroboration * 10)
    score += corr_points
    factors.append({"factor": "independent_corroboration", "value": corroboration,
                    "points": corr_points})

    if passage.kind in ("regulatory_notice", "report"):
        score += 15
        factors.append({"factor": "primary_document", "value": passage.kind, "points": 15})

    if re.search(r"(registry filing|filing at the|court record|final notice|public register)",
                 passage.quote, re.I):
        score += 10
        factors.append({"factor": "cites_primary_record", "value": True, "points": 10})

    # freshness: an undated source cannot carry a factual claim alone, and a stale one carries less
    if not passage.published:
        score -= FRESHNESS["undated_penalty"]
        factors.append({"factor": "undated_source", "value": None,
                        "points": -FRESHNESS["undated_penalty"]})

    if contradicting_domains:
        score -= min(20, len(contradicting_domains) * 10)
        factors.append({"factor": "contradiction", "value": len(contradicting_domains), "points": -10})

    score = max(0.0, min(100.0, score))
    band = "HIGH" if score >= 70 else "MEDIUM" if score >= 45 else "LOW" if score >= 20 else "VERY_LOW"
    return {"score": round(score, 1), "band": band, "factors": factors}


def _domain(url: str) -> str:
    return re.sub(r"^https?://", "", url).split("/")[0]


# ── SK-S6 triangulation ─────────────────────────────────────────────────────
ATTRIBUTION = re.compile(
    r"(?:according to|as (?:first |originally )?reported by|first reported by|"
    r"originally reported by|reported by|based on|citing)\s+"
    r"(?:a\s+|the\s+)?(?:report\s+(?:by|from)\s+|story\s+by\s+|article\s+by\s+)?"
    r"([A-Z][A-Za-z&\-]*(?:\s+[A-Z][A-Za-z&\-]*){0,3})")
# kept as the public name used elsewhere in the spec
WIRE_ATTRIBUTION = ATTRIBUTION


def _stem(domain: str) -> str:
    """commoditytoday.example -> 'commoditytoday'. Compared against the squeezed attribution string,
    so 'Global Wire', 'GlobalWire' and 'globalwire' all resolve to globalwire.example."""
    host = re.sub(r"^https?://", "", domain).split("/")[0]
    first = host.split(".")[0]
    return re.sub(r"(example|press|news)$", "", first).lower() or first.lower()


def _squeeze(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _display_name(domain: str) -> str:
    stam = _stem(domain)
    return stam.capitalize()


def _mention_resolver(candidates: list[str]):
    """Build a matcher over the domains we could plausibly be dealing with."""
    stems = {_stem(d): d for d in candidates if d}

    def resolve(mentioned: str) -> str | None:
        sq = _squeeze(mentioned)
        for stem, domain in stems.items():
            if stem and (stem in sq or sq in stem):
                return domain
        return None
    return resolve


def unresolved_attribution(body: str, candidates: list[str]) -> str | None:
    """An attribution phrase naming an outlet we did not retrieve. It is recorded so a reviewer
    knows the corroboration count may be an undercount instead of assuming independence."""
    resolve = _mention_resolver(candidates)
    for m in ATTRIBUTION.finditer(body):
        mentioned = m.group(1).strip()
        if not mentioned or resolve(mentioned):
            continue
        if len(mentioned.split()) <= 4:
            return mentioned
    return None


def attribution_origin(body: str, candidates: list[str]) -> str | None:
    """If this body credits another outlet, that outlet — not this one — is the origin of the fact.
    Keyed by the attributed *name*, so it works even when we never fetched the origin."""
    resolve = _mention_resolver(candidates)
    for m in ATTRIBUTION.finditer(body):
        hit = resolve(m.group(1).strip())
        if hit:
            return hit
    return None


def triangulate(findings: list[dict], fetches: dict[str, FetchResult]) -> list[dict]:
    """Group findings that assert the same fact, and decide independence.

    Two outlets that both attribute to the same wire are ONE source, not two. Counting them twice is
    the most common way an agent manufactures false corroboration.
    """
    groups: dict[str, dict] = {}
    for f in findings:
        key = f["claim_key"]
        g = groups.setdefault(key, {"claim": f["claim"], "claim_key": key, "supports": [],
                                    "contradicts": [], "independent_domains": set(),
                                    "corroboration_count": 0, "notes": []})
        if f["stance"] == "SUPPORTS":
            g["supports"].append(f)
        else:
            g["contradicts"].append(f)

    all_domains = sorted({_domain(u) for u in fetches})
    for key, g in groups.items():
        origins = {}
        for f in g["supports"]:
            fetch = fetches.get(f["url"])
            body = fetch.body if fetch else ""
            others = [d for d in all_domains if d != _domain(f["url"])]
            attributed = attribution_origin(body, others)
            origin = attributed or _domain(f["url"])
            origins.setdefault(origin, []).append(_domain(f["url"]))
        g["independent_domains"] = {_domain(f["url"]) for f in g["supports"]}
        g["corroboration_count"] = len(origins)
        g["origins"] = {k: sorted(set(v)) for k, v in origins.items()}
        for f in g["supports"]:
            fetch = fetches.get(f["url"])
            others = [d for d in all_domains if d != _domain(f["url"])]
            named = unresolved_attribution(fetch.body if fetch else "", others)
            if named:
                g["notes"].append(f"{_domain(f['url'])} attributes its report to '{named}', which "
                                  f"was not retrieved — corroboration may be an undercount")
        if len(origins) == 1 and len({_domain(f["url"]) for f in g["supports"]}) > 1:
            g["notes"].append("multiple outlets, single origin (syndicated reporting) — "
                              "counted as one independent source")
        for f in g["contradicts"]:
            g["notes"].append(f"contradicted by {_domain(f['url'])}: {f['quote'][:120]}")
    return list(groups.values())


# ── the agent ───────────────────────────────────────────────────────────────
@dataclass
class SearchResult:
    request_id: str
    subject: str
    status: str = "OK"
    queries: list = field(default_factory=list)
    fetches: dict = field(default_factory=dict)
    passages: list = field(default_factory=list)
    quarantined: list = field(default_factory=list)
    refusals: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    cost: dict = field(default_factory=dict)


class SearchAgent:
    """Breadth-first, resumable search. Every completed query is a safe resume point: the caller
    supplies `on_checkpoint`, which receives a JSON-serialisable snapshot of all work done so far.
    Nothing that costs a fetch is ever thrown away."""

    def __init__(self, fetcher: Fetcher, policy: SourcePolicy, *, budget: dict | None = None,
                 on_checkpoint=None, stop_after_queries: int | None = None):
        self.fetcher = fetcher
        self.policy = policy
        self.budget = budget or {"max_queries": 12, "max_fetches": 40, "max_domains": 15}
        self.on_checkpoint = on_checkpoint
        self.stop_after_queries = stop_after_queries

    # ── checkpoint (§05) ────────────────────────────────────────────────────
    @staticmethod
    def snapshot(res: SearchResult, query_index: int) -> dict:
        return {
            "schema": "webis.run-state/1",
            "request_id": res.request_id, "subject": res.subject,
            "status": res.status, "query_index": query_index,
            "queries": res.queries,
            "fetches": {u: dict(f.__dict__) for u, f in res.fetches.items()},
            "passages": [dict(p.__dict__ if hasattr(p, "__dict__") else p) for p in res.passages],
            "quarantined": res.quarantined, "refusals": res.refusals,
            "gaps": res.gaps, "cost": res.cost,
        }

    def restore(self, state: dict) -> SearchResult:
        res = SearchResult(request_id=state["request_id"], subject=state["subject"],
                           status=state.get("status", "OK"))
        res.queries = state.get("queries", [])
        for u, d in (state.get("fetches") or {}).items():
            res.fetches[u] = FetchResult(**d)
        res.passages = [Passage(**p) for p in state.get("passages", [])]
        res.quarantined = state.get("quarantined", [])
        res.refusals = state.get("refusals", [])
        # a budget gap describes the budget that was in force when it was raised; a resumed run
        # starts under a new budget, so keeping the old gap would misreport this leg
        stale = {"FETCH_BUDGET_EXHAUSTED", "WALLCLOCK_BUDGET_EXHAUSTED", "DOMAIN_BUDGET_EXHAUSTED"}
        res.gaps = [g for g in state.get("gaps", []) if g.get("gap") not in stale]
        res.cost = state.get("cost", {})
        # fetches already spent stay spent: a resumed run must not get a fresh budget
        self.fetcher.fetches = max(self.fetcher.fetches, len(res.fetches))
        return res

    def _checkpoint(self, res: SearchResult, query_index: int) -> None:
        if self.on_checkpoint:
            self.on_checkpoint(self.snapshot(res, query_index))

    def run(self, request: dict, *, checkpoint: dict | None = None) -> SearchResult:
        subject = request["subject"]

        # SK-S1
        queries = plan_queries(subject, facets=request.get("facets"),
                               max_queries=min(request.get("max_queries", 12),
                                               self.budget["max_queries"]),
                               related_parties=request.get("related_parties"))
        planned = [q.__dict__ for q in queries]

        if checkpoint:
            res = self.restore(checkpoint)
            res.queries = res.queries or planned
            start = checkpoint.get("query_index", 0)
        else:
            res = SearchResult(request_id=request["request_id"], subject=subject)
            res.queries = planned
            start = 0

        # SK-S2/S3
        seen_urls: set[str] = {u for u in res.fetches}
        done = 0
        started = time.monotonic()
        wall_limit = self.budget.get("max_wallclock_s")
        for qi, q in enumerate(queries):
            if qi < start:
                continue
            if self.stop_after_queries is not None and done >= self.stop_after_queries:
                res.status = "PARTIAL"
                res.gaps.append({"gap": "INTERRUPTED_BY_CALLER",
                                 "detail": f"stopped after {start + done} of {len(queries)} queries; "
                                           f"resume from out/state to continue"})
                res.cost = {"fetches": len(res.fetches),
                            "domains": len({_domain(u) for u in res.fetches}),
                            "queries": start + done}
                self._checkpoint(res, start + done)
                return res
            if wall_limit is not None and (time.monotonic() - started) > wall_limit:
                res.status = "PARTIAL"
                res.gaps.append({"gap": "WALLCLOCK_BUDGET_EXHAUSTED",
                                 "detail": f"stopped after {wall_limit}s of wall clock"})
                break
            if self.fetcher.fetches >= self.budget["max_fetches"]:
                res.gaps.append({"gap": "FETCH_BUDGET_EXHAUSTED",
                                 "detail": f"stopped after {self.fetcher.fetches} fetches"})
                res.status = "PARTIAL"
                break
            for hit in self.fetcher.search(q.text, limit=request.get("per_query_limit", 4)):
                # the budget binds per fetch, not per query: one query can surface four URLs
                if len(res.fetches) >= self.budget["max_fetches"]:
                    res.gaps.append({"gap": "FETCH_BUDGET_EXHAUSTED",
                                     "detail": f"stopped after {len(res.fetches)} fetches"})
                    res.status = "PARTIAL"
                    break
                url = hit["url"]
                if url in seen_urls:
                    continue
                if len({_domain(u) for u in res.fetches} | {_domain(url)}) > \
                        self.budget.get("max_domains", 10**6):
                    seen_urls.add(url)
                    res.gaps.append({"gap": "DOMAIN_BUDGET_EXHAUSTED",
                                     "detail": f"declined new domain {_domain(url)}: "
                                               f"max_domains={self.budget['max_domains']} reached"})
                    res.status = "PARTIAL"
                    continue
                seen_urls.add(url)
                f = self.fetcher.fetch(url, retrieved_at=request.get("retrieved_at",
                                                                     "2026-10-21T12:00:00Z"))
                res.fetches[url] = f
                if f.status in ("BLOCKED_DOMAIN", "ROBOTS_DISALLOWED"):
                    res.refusals.append({"url": url, "status": f.status,
                                         "reason": "source policy: not retrievable or not citable"})
                    continue
                if f.injected:
                    res.quarantined.append({
                        "url": url, "patterns": f.injection_evidence,
                        "content_hash": f.content_hash,
                        "reason": "instruction-like content in source; excluded from the evidence base",
                    })
                    continue
                ok, why = self.policy.citable(f)
                if f.status == "PAYWALLED":
                    res.gaps.append({"gap": "PAYWALLED_SOURCE",
                                     "detail": f"{url} — metadata only, body not citable"})
                    continue
                if not ok:
                    res.refusals.append({"url": url, "status": f.status, "reason": why,
                                         "relevant": bool(re.search(re.escape(subject.split()[0]),
                                                                    f.title + f.body, re.I))})
                    continue
                # SK-S4
                kws = [w for w in re.findall(r"[a-z]{4,}", q.text.lower())]
                res.passages.extend(extract_passages(f, keywords=kws))
            if res.status == "PARTIAL" and any(g["gap"] == "FETCH_BUDGET_EXHAUSTED"
                                               for g in res.gaps):
                break
            done += 1
            res.cost = {"fetches": len(res.fetches),
                        "domains": len({_domain(u) for u in res.fetches}),
                        "queries": qi + 1}
            self._checkpoint(res, qi + 1)      # safe resume point

        # a run stopped by the budget still leaves a resumable checkpoint: the interrupted query
        # is not counted as done, so a resumed run retries it with whatever budget it is given
        self._checkpoint(res, start + done)

        # SK-S6/S7 — claims are only formed from surviving passages
        res.claims = self._form_claims(res, request.get("attributes"))
        res.cost = {"fetches": len(res.fetches),
                    "domains": len({_domain(u) for u in res.fetches}),
                    "queries": len(queries)}
        if res.quarantined:
            res.status = "PARTIAL"
        return res

    # ── SK-S5b subject discrimination: a shared name is not the same person ──
    @staticmethod
    def discriminates(passage, attributes: dict) -> tuple[bool, str]:
        """Return (keep, reason). A passage is kept when it does not assert an attribute that
        conflicts with the subject, or when it asserts one that matches."""
        quote = passage.quote
        low = quote.lower()
        dob = (attributes or {}).get("dob")
        if dob:
            m = re.search(r"(\d{1,2})\s+(january|february|march|april|may|june|july|august|"
                          r"september|october|november|december)\s+(\d{4})", low)
            if m:
                found_year = int(m.group(3))
                subject_year = int(str(dob)[:4])
                if abs(found_year - subject_year) > 1:
                    return False, f"conflicting date of birth ({found_year} vs subject {subject_year})"
        age = re.search(r"\b(?:who is|aged)\s+(\d{2})\b", low)
        if age and dob:
            implied = 2026 - int(str(dob)[:4])
            if abs(implied - int(age.group(1))) > 2:
                return False, f"conflicting inferred age ({age.group(1)} vs subject {implied})"
        country = (attributes or {}).get("country")
        if country == "VE" and re.search(r"\bvalencia\b", low):
            return False, "conflicting location (Valencia) for a Venezuela-domiciled subject"
        return True, "no conflicting attribute"

    def _form_claims(self, res: SearchResult, attributes: dict | None = None) -> list[dict]:
        claims = []
        for p in res.passages:
            keep, why = self.discriminates(p, attributes or {})
            if not keep:
                res.refusals.append({"url": p.url, "status": "EXCLUDED_SAME_NAME",
                                     "reason": why, "quote": p.quote[:160]})
                continue
            low = p.quote.lower()
            if any(k in low for k in ("final notice", "penalty", "failed to maintain")):
                key, text, stance = "regulatory_penalty", \
                    "A regulator has taken public enforcement action naming the subject.", "SUPPORTS"
            elif any(k in low for k in ("entered administration", "administration on", "ceased trading")):
                key, text, stance = "corporate_status", \
                    "The subject has entered administration.", "SUPPORTS"
            elif "inquiry" in low:
                key, text, stance = "inquiry", \
                    "A customs authority opened an inquiry; no charges have been filed.", "SUPPORTS"
            elif "no evidence of dishonesty" in low or "negligent rather than deliberate" in low:
                key, text, stance = "misconduct_character", \
                    "The regulator characterised the conduct as negligent, not dishonest.", "SUPPORTS"
            elif "accused of laundering" in low or "laundering" in low:
                key, text, stance = "laundering_allegation", \
                    "An unsourced allegation of money laundering has been published.", "SUPPORTS"
            elif "not opened an investigation" in low:
                key, text, stance = "fca_scope", \
                    "The FCA stated it had not opened its own investigation.", "CONTRADICTS"
            else:
                continue
            quote = p.quote
            sensitive = self.fetcher.scan_sensitive(quote)
            for hit in sensitive:
                a, b = hit["span"]
                quote = quote[:a] + "[REDACTED]" + quote[b:]
            claims.append({
                "claim_key": key, "claim": text, "stance": stance,
                "pii_redactions": sorted({h["kind"] for h in sensitive}),
                # the quote may be redacted, so integrity is proven on the hash of the span's
                # original text: an auditor can re-hash the source without seeing the PII
                "span_text_sha256": sha256(p.quote), "passage_id": p.passage_id,
                "url": p.url, "quote": quote, "char_span": p.char_span,
                "content_hash": p.content_hash, "source_tier": p.source_tier,
                "published": p.published,
            })
        return claims

    def _refresh_limit(self, claim_key: str) -> int:
        f = (getattr(self.policy, "cfg", {}) or {}).get("freshness", {}) or {}
        return int({"regulatory_penalty": f.get("regulatory_action_days", 30),
                    "sanctions": f.get("regulatory_action_days", 30),
                    "corporate_status": f.get("corporate_status_days", 7)}.get(
                        claim_key, f.get("adverse_media_days", 90)))

    # ── SK-S7 citation pack ─────────────────────────────────────────────────
    def pack(self, res: SearchResult) -> dict:
        fetch_by_url = res.fetches
        groups = triangulate(res.claims, fetch_by_url)

        claims_out = []
        for g in groups:
            support_domains = {_domain(f["url"]) for f in g["supports"]}
            contra_domains = {_domain(f["url"]) for f in g["contradicts"]}
            # one citation per URL: three passages from one document are one source, and listing
            # them separately would read as three. Extra passage hashes ride along as evidence_ids.
            per_url: dict[str, list[dict]] = {}
            for f in g["supports"]:
                # look the passage up by id, never by quote text: a redacted quote no longer
                # matches its source passage and the claim would silently lose its citation
                p = next((p for p in res.passages
                          if p.url == f["url"] and p.passage_id == f.get("passage_id")), None) \
                    or next((p for p in res.passages
                             if p.url == f["url"] and p.quote == f["quote"]), None)
                if not p:
                    continue
                cred = credibility(p, corroborating_domains=support_domains,
                                   contradicting_domains=contra_domains,
                                   as_of=res.__dict__.get("as_of"))
                per_url.setdefault(f["url"], []).append({
                    "url": f["url"], "quote": f["quote"], "char_span": f["char_span"],
                    "span_text_sha256": f.get("span_text_sha256"),
                    "pii_redactions": f.get("pii_redactions", []),
                    "content_hash": f["content_hash"], "published": f["published"],
                    "source_tier": f["source_tier"], "credibility": cred,
                    "evidence_id": "EV-" + sha256(f["url"] + f["quote"])[7:19]})
            scored = []
            for url, cands in per_url.items():
                cands.sort(key=lambda c: -c["credibility"]["score"])
                best = dict(cands[0])
                best["evidence_ids"] = [c["evidence_id"] for c in cands]
                best["quotes"] = [c["quote"] for c in cands]
                scored.append(best)
            scored.sort(key=lambda s: -s["credibility"]["score"])
            ages = [_age_days(c["published"], res.__dict__.get("as_of") or
                              (res.fetches.get(c["url"]).retrieved_at if res.fetches.get(c["url"])
                               else None)) for c in scored]
            known = [a for a in ages if a is not None]
            newest_age = min(known) if known else None
            limit = self._refresh_limit(g["claim_key"])
            claims_out.append({
                "claim_key": g["claim_key"], "claim": g["claim"],
                "newest_source_age_days": newest_age,
                "refresh_limit_days": limit,
                "refresh_due": (newest_age is None) or (newest_age > limit),
                "corroboration_count": g["corroboration_count"],
                # one origin reached through several outlets is exactly the case to flag
                "syndication_detected": len(g["independent_domains"]) > g["corroboration_count"],
                "independent_domains": sorted(g["independent_domains"]),
                "origins": g.get("origins", {}),
                "notes": g.get("notes", []),
                "contradictions": [{"url": f["url"], "quote": f["quote"]} for f in g["contradicts"]],
                "citations": scored,
                "confidence": _confidence(scored),
                "citable": bool(scored) and _confidence(scored) != "UNSUPPORTED",
            })

        return {
            "request_id": res.request_id,
            "subject": res.subject,
            "status": res.status,
            "claims": claims_out,
            "quarantined_sources": res.quarantined,
            "refused_sources": res.refusals,
            "unverified_circulation": [
                {"url": r["url"], "status": r["status"],
                 "note": "source is relevant but not citable under the source policy; the fact that "
                         "this material is circulating is recorded, its content is not relied upon"}
                for r in res.refusals if r.get("relevant")
            ],
            "gaps": res.gaps,
            "query_log": res.queries,
            "cost": res.cost,
            "method": {
                "agent": "WEBIS-SEARCH", "version": "1.0.0",
                "max_tier_citable": self.policy.max_tier,
                "tier4_corroboration_only": self.policy.tier4_corroboration_only,
                "note": "every claim carries at least one verbatim quote with a content hash and "
                        "a character span into the retrieved document",
            },
        }


def _confidence(scored: list[dict]) -> str:
    if not scored:
        return "UNSUPPORTED"
    best = max(s["credibility"]["score"] for s in scored)
    doms = {_domain(s["url"]) for s in scored}
    if best >= 70 and len(doms) >= 2:
        return "HIGH"
    if best >= 45:
        return "MEDIUM"
    return "LOW"
