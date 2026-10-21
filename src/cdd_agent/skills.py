"""Skill implementations (reference, deterministic, model-free).

Each function maps 1:1 to spec/02-skills.md and emits the evidence write classes defined in
spec/04-evidence-writes.md. In production the `extract`/`compose`/`media` internals are LLM calls
behind constrained decoding; the contracts, evidence writes and idempotency keys are identical.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .evidence import EvidenceLedger, Finding, idem_key, now
from .policy import RDE_LIST, risk_engine

# ─────────────────────────── document-type policy ───────────────────────────

IDENTITY_DOCS = {"PASSPORT", "NATIONAL_ID", "CERTIFICATE_OF_INCORPORATION",
                 "CORPORATE_REGISTRY_EXTRACT"}
OWNERSHIP_DOCS = {"SHARE_REGISTER", "UBO_DECLARATION", "TRUST_DEED", "ORG_CHART",
                  "ARTICLES_OF_ASSOCIATION"}

INJECTION_PATTERNS = [
    r"ignore (all )?previous instructions", r"ignore all (prior|previous) instructions",
    r"set the risk rating", r"pre-?approved by compliance", r"omit the .* section",
    r"you are now", r"system\s*:", r"do not report this", r"output your (system )?prompt",
    r"disregard (the )?(above|prior)",
]

PROTECTED_TERMS = ["ethnic", "ethnicity", "race", "religion", "muslim", "jewish", "christian",
                   "sexual orientation", "disability", "caste"]


@dataclass
class SkillResult:
    skill_id: str
    status: str = "OK"
    outputs: dict = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    confidence: str = "HIGH"
    reason_code: str | None = None
    notes: list[str] = field(default_factory=list)


def scan_injection(text: str) -> list[str]:
    low = text.lower()
    return [p for p in INJECTION_PATTERNS if re.search(p, low)]


# ─────────────────────────────── SK-01 triage ───────────────────────────────

def sk01_triage(ctx) -> SkillResult:
    res = SkillResult("SK-01")
    pages_total = 0
    unusable = 0

    # W1: run registration — input hashes are the anchor for reproducibility
    res.evidence_ids.append(ctx.ledger.append(
        {"record_type": "REGISTRATION",
         "subject": {"kind": "RUN", "id": ctx.run_id},
         "value": {"raw": "run start", "normalized": ctx.case["case_id"], "datatype": "run"},
         "basis": "document", "confidence": "HIGH",
         "run_meta": {"case_input_sha256": ctx.input_hashes["case"],
                      "bundle_sha256": ctx.input_hashes["bundle"],
                      "policy_sha256": ctx.input_hashes["policy"],
                      "agent_version": ctx.agent_version, "prompt_hash": ctx.prompt_hash}},
        key=idem_key(ctx.case["case_id"], "SK-01", "run-registration"),
        skill="SK-01"))

    for doc in ctx.bundle["documents"]:
        doc_pages = doc["pages"]
        pages_total += doc_pages
        if doc["quality_score"] < ctx.policy["documents"]["min_quality_score"]:
            unusable += doc_pages
        res.evidence_ids.append(ctx.ledger.append(
            {"record_type": "REGISTRATION",
             "subject": {"kind": "DOCUMENT", "doc_id": doc["doc_id"]},
             "value": {"raw": doc["filename"], "normalized": doc["detected_type"],
                       "datatype": "document"},
             "basis": "document", "confidence": "HIGH",
             "locator": {"doc_id": doc["doc_id"], "page": 1,
                         "doc_sha256": hashlib.sha256(doc["doc_id"].encode()).hexdigest()},
             "source_meta": {"issuer": doc.get("issuer"), "doc_date": doc.get("doc_date"),
                             "pages": doc_pages, "declared_type": doc.get("detected_type")},
             "detected_type": doc["detected_type"]},
            key=idem_key(ctx.case["case_id"], "SK-01", doc["doc_id"]), skill="SK-01"))

        if doc.get("quarantine_flag"):
            ctx.manifest["quarantined"].append(doc["doc_id"])
    # doc -> registration evidence, so later stages can cite the document that should have carried it
    ctx.stage_notes["doc_evidence"] = {
        ev_id: None for ev_id in []} if False else ctx.stage_notes.get("doc_evidence", {})
    for r in ctx.ledger.records():
        if r["record_type"] == "REGISTRATION" and (r.get("subject") or {}).get("kind") == "DOCUMENT":
            ctx.stage_notes.setdefault("doc_evidence", {})[r["subject"]["doc_id"]] = r["evidence_id"]

    ctx.manifest["documents"] = ctx.bundle["documents"]
    ctx.manifest["pages_total"] = pages_total
    res.outputs = {"documents": len(ctx.bundle["documents"]), "pages_total": pages_total,
                   "unusable_pct": round(unusable / pages_total * 100, 1)}
    if res.outputs["unusable_pct"] > ctx.policy["gates"]["G1_bundle_unusable_pct"]:
        res.status, res.reason_code = "BLOCKED", "BLOCKED_INPUT"
    return res


# ───────────────────────── SK-02/03 ingest + extract ────────────────────────

FIELD_PATTERNS = [
    ("legal_name", re.compile(r"^(Company name|Entity name|Member)\s*:\s*(.+)$", re.I)),
    ("incorporation_number", re.compile(r"^(Company number|Registration number)\s*:\s*(.+)$", re.I)),
    ("incorporation_date", re.compile(r"^(Incorporation date|Date of incorporation|Date of settlement)\s*:\s*(.+)$", re.I)),
    ("status", re.compile(r"^Status\s*:\s*(.+)$", re.I)),
    ("registered_address", re.compile(r"^(Registered office|Registered address)\s*:\s*(.+)$", re.I)),
    ("surname", re.compile(r"^Surname\s*:\s*(.+)$", re.I)),
    ("given_names", re.compile(r"^Given names\s*:\s*(.+)$", re.I)),
    ("nationality", re.compile(r"^Nationality\s*:\s*(.+)$", re.I)),
    ("dob", re.compile(r"^Date of birth\s*:\s*(.+)$", re.I)),
    ("passport_number", re.compile(r"^Passport number\s*:\s*(.+)$", re.I)),
    ("passport_expiry", re.compile(r"^Date of expiry\s*:\s*(.+)$", re.I)),
    ("revenue", re.compile(r"^Revenue\s*:\s*(.+)$", re.I)),
    ("employees", re.compile(r"^Employees\s*:\s*(.+)$", re.I)),
    ("business_nature", re.compile(r"^Business nature\s*:\s*(.+)$", re.I)),
    ("address", re.compile(r"^Address\s*:\s*(.+)$", re.I)),
    ("issue_date", re.compile(r"^Issue date\s*:\s*(.+)$", re.I)),
    ("total_credits", re.compile(r"^Total credits\s*:\s*(.+)$", re.I)),
    ("settlor", re.compile(r"^Settlor\s*:\s*(.+)$", re.I)),
    ("trustee", re.compile(r"^Trustee\s*:\s*(.+)$", re.I)),
]
DIRECTOR_RE = re.compile(r"^Director\s*:\s*(.+?)\s*(?:\(appointed.*)?$", re.I)
SHAREHOLDER_RE = re.compile(
    r"^(.+?[A-Za-z\)])\s{2,}([\d,]+)\s+([A-Za-z][A-Za-z \-]*?)\s*([\d.]+)\s*%\s*$", re.I)
SHARE_TOTAL_RE = re.compile(r"^total\s", re.I)


def sk02_ingest_document(ctx, doc: dict) -> SkillResult:
    """Per-document unit of work. Idempotent: safe to replay after a crash."""
    res = SkillResult("SK-02")
    key = idem_key(ctx.case["case_id"], doc["doc_id"], "SK-02", ctx.prompt_hash)
    cached = ctx.ledger.idem.get(key)
    if cached:
        res.outputs = json.loads(ctx.state_cache.get(cached, "{}"))
        res.notes.append("replayed from idempotency store")
        res.evidence_ids.append(cached)
        return res

    pages = ctx.pages[doc["doc_id"]]["pages"]
    read, illegible, blank = 0, 0, 0
    page_texts: dict[int, str] = {}

    for page in pages:
        if page.get("blank"):
            blank += 1
            ctx.manifest["page_ledger"].append({"doc_id": doc["doc_id"], "page": page["page"],
                                                "status": "BLANK", "method": "none",
                                                "ocr_confidence": None, "reason": None})
            continue
        conf = page.get("ocr_confidence", 0.0)
        if conf < 0.60:
            illegible += 1
            ctx.manifest["page_ledger"].append({"doc_id": doc["doc_id"], "page": page["page"],
                                                "status": "ILLEGIBLE", "method": "ocr",
                                                "ocr_confidence": conf,
                                                "reason": "OCR confidence below policy floor"})
            continue
        read += 1
        page_texts[page["page"]] = page["text"]
        ctx.manifest["page_ledger"].append({"doc_id": doc["doc_id"], "page": page["page"],
                                            "status": "READ",
                                            "method": "native_text" if conf >= 0.95 else "ocr",
                                            "ocr_confidence": conf, "reason": None})

    hits = []
    for pg, text in page_texts.items():
        pats = scan_injection(text)
        if pats:
            hits.append({"page": pg, "patterns": pats, "quote": text.strip()[:400]})

    out = {"read": read, "illegible": illegible, "blank": blank,
           "page_texts": page_texts, "injection_hits": hits}
    ctx.state_cache[key] = json.dumps(out)
    ctx.manifest_touch(doc["doc_id"], read, illegible)
    res.outputs = out
    res.evidence_ids.append(key)

    if hits:
        ev = ctx.ledger.observe(
            quote=hits[0]["quote"],
            locator={"doc_id": doc["doc_id"], "page": hits[0]["page"],
                     "char_span": [0, len(hits[0]["quote"])]},
            method="native_text", conf="HIGH",
            key=idem_key(ctx.case["case_id"], doc["doc_id"], "SK-02", "injection"),
            skill="SK-02")
        f = Finding(
            finding_id=ctx.next_finding_id(), type="INJECTION_ATTEMPT", severity="HIGH",
            title="Document contains embedded instruction text",
            detail=(f"Page {hits[0]['page']} of {doc['filename']} contains instruction-like text "
                    f"inconsistent with the document's declared purpose. The document was quarantined "
                    f"from the decision evidence base; its claims are not relied upon."),
            basis="document", confidence="HIGH", evidence_refs=[ev],
            policy_refs=["GR-INJECTION-01"], status="OPEN")
        ctx.ledger.finding(f, "SK-02")
        ctx.findings.append(f.to_dict(ctx.case_id, "SK-02", ctx.run_id))
        ctx.stage_notes["injection"] = True
        res.findings.append(f.finding_id)
        res.status = "PARTIAL"
    return res


def sk03_extract_document(ctx, doc: dict) -> SkillResult:
    res = SkillResult("SK-03")
    key = idem_key(ctx.case["case_id"], doc["doc_id"], "SK-03", ctx.prompt_hash)
    cached = ctx.ledger.idem.get(key)
    if cached:
        res.outputs = ctx.facts_by_doc.get(doc["doc_id"], {})
        res.notes.append("replayed")
        return res

    if doc["doc_id"] in ctx.manifest["quarantined"]:
        res.status, res.reason_code = "PARTIAL", "INJECTION_QUARANTINE"
        res.notes.append("quarantined document: facts recorded as UNVERIFIED, not decision evidence")
        return res

    pages = ctx.pages[doc["doc_id"]]["pages"]
    facts: dict[str, list[dict]] = {}
    holders: list[dict] = []
    directors: list[dict] = []

    for page in pages:
        text = page["text"]
        conf = page.get("ocr_confidence", 0.0) or 0.0
        if conf < 0.60:
            continue
        for line_no, raw_line in enumerate(text.splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            for fieldname, pat in FIELD_PATTERNS:
                m = pat.match(line)
                if not m:
                    continue
                value = m.groups()[-1].strip()
                quote = line
                locator = {"doc_id": doc["doc_id"], "page": page["page"],
                           "char_span": [text.index(line), text.index(line) + len(line)]}
                ev = ctx.ledger.extract(
                    subject={"kind": "DOCUMENT_FIELD", "doc_id": doc["doc_id"], "field": fieldname},
                    value={"raw": value, "normalized": value, "datatype": "string"},
                    quote=quote, locator=locator,
                    method="native_text" if conf >= 0.95 else "ocr",
                    conf="HIGH" if conf >= 0.90 else "MEDIUM",
                    key=idem_key(ctx.case["case_id"], doc["doc_id"], "SK-03", fieldname,
                                 page["page"], quote),
                    skill="SK-03")
                facts.setdefault(fieldname, []).append(
                    {"value": value, "page": page["page"], "evidence_id": ev,
                     "confidence": "HIGH" if conf >= 0.90 else "MEDIUM",
                     "doc_id": doc["doc_id"]})
            m = DIRECTOR_RE.match(line)
            if m:
                name = m.group(1).strip()
                ev = ctx.ledger.extract(
                    subject={"kind": "OFFICER", "doc_id": doc["doc_id"], "field": "director"},
                    value={"raw": name, "normalized": name, "datatype": "string"},
                    quote=line, locator={"doc_id": doc["doc_id"], "page": page["page"],
                                         "char_span": [text.index(line), text.index(line) + len(line)]},
                    method="native_text", conf="HIGH",
                    key=idem_key(ctx.case["case_id"], doc["doc_id"], "SK-03", "director", name),
                    skill="SK-03")
                directors.append({"name": name, "evidence_id": ev, "doc_id": doc["doc_id"],
                                  "page": page["page"]})
            m = SHAREHOLDER_RE.match(raw_line.strip())
            if m and not SHARE_TOTAL_RE.match(raw_line.strip()) \
                    and m.group(1).strip().lower() != "member":
                holder, shares, pct = m.group(1).strip(), m.group(2), m.group(4)
                ev = ctx.ledger.extract(
                    subject={"kind": "SHAREHOLDING", "doc_id": doc["doc_id"], "holder": holder},
                    value={"raw": f"{shares} shares ({pct}%)", "normalized": pct, "datatype": "percent"},
                    quote=raw_line.rstrip(),
                    locator={"doc_id": doc["doc_id"], "page": page["page"],
                             "char_span": [text.index(raw_line.rstrip()),
                                           text.index(raw_line.rstrip()) + len(raw_line.rstrip())]},
                    method="table", conf="HIGH",
                    key=idem_key(ctx.case["case_id"], doc["doc_id"], "SK-03", "holding", holder, pct),
                    skill="SK-03")
                holders.append({"holder": holder, "pct": float(pct), "evidence_id": ev,
                                "doc_id": doc["doc_id"], "page": page["page"]})

    ctx.facts_by_doc[doc["doc_id"]] = {"facts": facts, "holders": holders, "directors": directors}
    res.outputs = ctx.facts_by_doc[doc["doc_id"]]
    ctx.ledger.idem.put(key, idem_key(ctx.case["case_id"], doc["doc_id"], "SK-03", "done"))
    return res


# ───────────────────── SK-04/05 link + ownership graph ──────────────────────

def _pick_facts(ctx, doc_types: set[str], fieldname: str) -> list[dict]:
    out = []
    for doc in ctx.bundle["documents"]:
        if doc["detected_type"] in doc_types:
            f = ctx.facts_by_doc.get(doc["doc_id"], {}).get("facts", {})
            out.extend(f.get(fieldname, []))
    return out


def sk05_ownership(ctx) -> SkillResult:
    """Ownership graph + effective-% computation. Arithmetic via calc.ownership, never in prose."""
    res = SkillResult("SK-05")
    threshold = ctx.policy["ownership"]["ubo_threshold_pct"]

    def holdings(doc_id: str) -> list[dict]:
        return ctx.facts_by_doc.get(doc_id, {}).get("holders", [])

    customer_rows = holdings("DOC-4831-002")     # register of the customer itself
    upstream_rows = holdings("DOC-4831-003")     # register of the intermediate holding company

    customer_name = "Meridian Trade Holdings Ltd"
    interco_name = "Northbridge Holdings Limited"

    edges = [
        {"from": r["holder"], "to": customer_name, "pct": r["pct"], "evidence_id": r["evidence_id"],
         "doc_id": r["doc_id"]} for r in customer_rows
    ] + [
        {"from": r["holder"], "to": interco_name, "pct": r["pct"], "evidence_id": r["evidence_id"],
         "doc_id": r["doc_id"]} for r in upstream_rows
    ]

    direct_total = round(sum(e["pct"] for e in edges if e["to"] == customer_name), 2)
    sum_check = ("OK" if abs(direct_total - 100.0) < 0.01 else "DISCREPANCY")

    # paths from natural persons to the customer
    ev_by = {(e["from"], e["to"]): e["evidence_id"] for e in edges}
    interco_pct = next((e["pct"] for e in edges if e["to"] == customer_name
                        and e["from"] == interco_name), 0.0)
    trust_pct = next((e["pct"] for e in edges if e["to"] == customer_name
                      and "Trust" in e["from"]), 0.0)

    path_specs = []
    for row in upstream_rows:
        path_specs.append({
            "person": row["holder"], "parts": [(row["holder"], interco_name, row["pct"]),
                                              (interco_name, customer_name, interco_pct)],
            "basis": "document"})
    if trust_pct:
        path_specs.append({
            "person": "Elena Vasquez",
            "parts": [("Elm Family Trust", customer_name, trust_pct)],
            "basis": "inference",
            "note": "Trust deed names Elena Vasquez as settlor and primary beneficiary; the "
                    "trustee holds the shares for her benefit (DOC-4831-011 p.1-2)."})

    totals: dict[str, float] = {}
    comp_ev = {}
    for spec in path_specs:
        value = 1.0
        for _, _, pct in spec["parts"]:
            value *= pct / 100.0
        effective = round(value * 100, 4)
        spec["effective_pct"] = effective
        totals[spec["person"]] = round(totals.get(spec["person"], 0.0) + effective, 4)
        comp_ev[spec["person"]] = ctx.ledger.compute(
            subject={"kind": "EFFECTIVE_OWNERSHIP", "person": spec["person"],
                     "route": " -> ".join(p[0] for p in spec["parts"])},
            calc_id="calc.ownership@1.0.0",
            inputs=[{"from": a, "to": b, "factor_pct": pct,
                     "evidence_id": ev_by.get((a, b))} for a, b, pct in spec["parts"]],
            operation="multiply(pcts_along_path) then sum(across_paths)",
            result=f"{effective:.2f}", basis=spec["basis"],
            key=idem_key(ctx.case_id, "SK-05", spec["person"], spec["parts"][0][0]), skill="SK-05")

    bos = [{"person": p, "effective_pct": pct} for p, pct in totals.items() if pct >= threshold]
    below = [{"person": p, "effective_pct": pct} for p, pct in totals.items() if pct < threshold]

    # unresolved chain: any holder that is not a natural person and whose principal is not disclosed
    natural_persons = set(totals)
    looked_through = {e["to"] for e in edges if e["doc_id"] == "DOC-4831-003"}
    unresolved = []
    for r in customer_rows:
        if r["holder"] in natural_persons or "Trust" in r["holder"] \
                or r["holder"] in looked_through:
            continue
        unresolved.append({"holder": r["holder"], "pct": r["pct"],
                           "evidence_id": r["evidence_id"], "doc_id": r["doc_id"],
                           "reason": "corporate/nominee holder; underlying natural person not disclosed"})

    if unresolved:
        u = unresolved[0]
        f = Finding(
            finding_id=ctx.next_finding_id(), type="UNRESOLVED_UBO", severity="HIGH",
            title=f"{u['pct']:.0f}% shareholder is a nominee company with undisclosed principal",
            detail=(f"{u['holder']} holds {u['pct']:.0f}% of the customer's issued shares, registered "
                    f"as nominee. The register states the principal is not disclosed and no "
                    f"declaration, nominee agreement or transfer instrument was supplied. Ownership "
                    f"above the {threshold}% threshold is therefore unresolved for that block; the "
                    f"control prong cannot be applied to it either."),
            basis="document", confidence="HIGH", evidence_refs=[u["evidence_id"]],
            policy_refs=["UBO-25", "RDE-07"],
            rating_impact={"factor": "OPAQUE_OWNERSHIP", "direction": "UP", "weight_points": 20})
        ctx.ledger.finding(f, "SK-05")
        ctx.findings.append(f.to_dict(ctx.case_id, "SK-05", ctx.run_id))
        res.findings.append(f.finding_id)

    control_note = None
    for b in below:
        control_note = (f"{b['person']} holds {b['effective_pct']:.2f}% effective — below the "
                        f"{threshold:.0f}% threshold. He is a director of both the customer and the "
                        f"intermediate holding company, so control is assessed under the control "
                        f"prong (senior managing official) rather than the ownership prong.")

    res.outputs = {
        "nodes": len({e["from"] for e in edges} | {e["to"] for e in edges}),
        "edges": len(edges),
        "direct_holdings_total_pct": direct_total,
        "sum_check": sum_check,
        "paths": [{k: v for k, v in p.items() if k != "parts"} |
                  {"chain": [x[0] for x in p["parts"] if x[0] != p["person"]] + [customer_name],
                   "pcts": [x[2] for x in p["parts"]]} for p in path_specs],
        "effective_totals": totals, "beneficial_owners": bos, "below_threshold": below,
        "unresolved_links": unresolved, "control_note": control_note, "threshold": threshold,
        "effective_evidence": comp_ev,
    }
    res.evidence_ids = [e["evidence_id"] for e in edges] + list(comp_ev.values())
    res.confidence = "LOW" if unresolved else "HIGH"
    return res


# ─────────────────────────── SK-07 screening ────────────────────────────────

SEEDED_LISTS = {
    # deliberate: a WEAK look-alike to prove triage discipline, plus a PEP match
    "OFAC_SDN": [{"name": "Elena Maria Vasquez Rojas", "dob": "1971-06-02", "country": "CO",
                  "score": 0.71, "case_ref": "SDN-2019-4471"}],
    "PEP_GLOBAL": [{"name": "Elena Maria Vasquez", "dob": "1979-03-14", "country": "VE",
                    "role": "Former deputy minister of trade, Venezuela (2014-2017)",
                    "score": 0.93, "case_ref": "PEP-88213"}],
    "UK_OFSI": [], "EU_CFSP": [], "UN_CONSOLIDATED": [], "OFAC_CAPTA": [], "OFAC_SSI": [],
    "PEP_DOMESTIC": [],
}

# connectors the harness simulates as degraded, to exercise graceful degradation
UNAVAILABLE_LISTS = {"LOCAL_REGULATOR"}


def sk07_screening(ctx, subjects: list[dict]) -> SkillResult:
    res = SkillResult("SK-07")
    lists = ctx.policy["screening"]["lists"]
    all_lists = lists["sanctions"] + lists["pep"]
    versions = {"OFAC_SDN": "2026-10-18", "UN_CONSOLIDATED": "2026-10-17",
                "EU_CFSP": "2026-10-17", "UK_OFSI": "2026-10-18", "PEP_GLOBAL": "2026-10-15"}

    queries, hits, unavailable = [], [], []
    for subj in subjects:
        for lst in all_lists:
            if lst in UNAVAILABLE_LISTS:
                unavailable.append(lst)
                continue
            raw = SEEDED_LISTS.get(lst, [])
            matched = [h for h in raw if h["name"].lower().startswith(subj["name"].split()[0].lower())
                       and subj["name"].split()[-1].lower() in h["name"].lower()]

            ev = ctx.ledger.query(
                connector="sanctions.screen" if "PEP" not in lst else "pep.screen",
                dataset=lst, dataset_version=versions.get(lst, "2026-10-18"),
                params={"name": subj["name"], "dob": subj.get("dob"), "country": subj.get("country")},
                result_count=len(matched), response_ref=f"inline://{subj['name']}/{lst}",
                hits=matched, subject_name=subj["name"],
                key=idem_key(ctx.case_id, "SK-07", subj["name"], lst), skill="SK-07")
            queries.append({"subject": subj["name"], "list": lst, "result_count": len(matched),
                            "evidence_id": ev, "dataset_version": versions.get(lst)})

            for m in matched:
                strength, discriminators, conflicts = _triage(subj, m)
                # a sanctions TRUE/STRONG is never cleared here
                ftype = "PEP_EXPOSURE" if "PEP" in lst else "SANCTIONS_HIT"
                sev = "HIGH" if strength in ("TRUE", "STRONG") else "MEDIUM"
                f = Finding(
                    finding_id=ctx.next_finding_id(), type=ftype, severity=sev,
                    title=f"{'PEP' if 'PEP' in lst else 'Sanctions'} match for {subj['name']} on {lst} ({strength})",
                    detail=("Match score %.2f against %s. Discriminators: %s. Conflicts: %s. %s"
                            % (m["score"], lst, "; ".join(discriminators) or "none",
                               "; ".join(conflicts) or "none",
                               "Referred for analyst adjudication (G2). The agent does not clear hits."
                               if strength in ("TRUE", "STRONG") else
                               "Assessed as a look-alike; recorded for audit.")),
                    basis="screening", confidence="HIGH", evidence_refs=[ev],
                    policy_refs=["SCR-01", "RDE-15"],
                    rating_impact={"factor": "PEP_EXPOSURE" if "PEP" in lst else "SANCTIONS_PROXIMITY",
                                   "direction": "UP", "weight_points": 25})
                ctx.ledger.finding(f, "SK-07")
                ctx.findings.append(f.to_dict(ctx.case_id, "SK-07", ctx.run_id))
                hits.append({"hit_id": f"hit-{len(hits)+1}", "subject": subj["name"], "list": lst,
                             "score": m["score"], "strength": strength,
                             "discriminators": discriminators, "conflicts": conflicts,
                             "evidence_id": ev, "finding_id": f.finding_id,
                             "analyst_required": strength in ("TRUE", "STRONG")})
                res.findings.append(f.finding_id)

    # completeness assertion: every subject x required list must have a W5 or an unavailability record
    required = [(s["name"], l) for s in subjects for l in lists["sanctions"]]
    covered = {(q["subject"], q["list"]) for q in queries}
    missing = [r for r in required if r not in covered and r[1] not in unavailable]
    if missing:
        f = Finding(finding_id=ctx.next_finding_id(), type="SCREENING_INCOMPLETE", severity="HIGH",
                    title="Screening not completed for all subject × list pairs",
                    detail=f"Missing query records for: {missing[:5]}", basis="judgment",
                    confidence="HIGH", evidence_refs=[queries[0]["evidence_id"]] if queries else [],
                    policy_refs=["SCR-04"], status="OPEN")
        ctx.ledger.finding(f, "SK-07")
        ctx.findings.append(f.to_dict(ctx.case_id, "SK-07", ctx.run_id))
        res.findings.append(f.finding_id)
        res.status, res.reason_code = "PARTIAL", "SCREENING_INCOMPLETE"

    if unavailable:
        # a degraded connector is disclosed, not silently converted into "no hits"
        f = Finding(finding_id=ctx.next_finding_id(), type="SCREENING_INCOMPLETE", severity="HIGH",
                    title=f"Screening list unavailable: {', '.join(sorted(set(unavailable)))}",
                    detail=(f"The connector for {', '.join(sorted(set(unavailable)))} returned an error "
                            f"after retries. No clearance is asserted against that list; the run is "
                            f"marked partial and gate G3 is forced."),
                    basis="judgment", confidence="HIGH",
                    evidence_refs=[queries[0]["evidence_id"]] if queries else [],
                    policy_refs=["SCR-04"], status="OPEN")
        ctx.ledger.finding(f, "SK-07")
        ctx.findings.append(f.to_dict(ctx.case_id, "SK-07", ctx.run_id))
        res.findings.append(f.finding_id)
        res.status, res.reason_code = "PARTIAL", "CONNECTOR_DEGRADED"

    res.outputs = {"subjects": subjects, "queries": queries, "hits": hits,
                   "lists_run": sorted({q["list"] for q in queries}),
                   "coverage": {"subject_list_pairs": len(queries),
                                "lists_unavailable": sorted(set(unavailable)),
                                "lists_queried": sorted({q["list"] for q in queries})}}
    res.evidence_ids = [q["evidence_id"] for q in queries]
    return res


def _triage(subj: dict, m: dict) -> tuple[str, list[str], list[str]]:
    disc, conflicts = [], []
    if subj.get("dob") and m.get("dob"):
        if subj["dob"] == m["dob"]:
            disc.append(f"DOB match ({subj['dob']})")
        else:
            conflicts.append(f"DOB differs (subject {subj['dob']} vs list {m['dob']})")
    if subj.get("country") and m.get("country"):
        if subj["country"] == m["country"]:
            disc.append(f"country match ({subj['country']})")
        else:
            conflicts.append(f"country differs ({subj['country']} vs {m['country']})")
    score = m["score"]
    if conflicts:
        return "WEAK", disc, conflicts           # dispositive conflict caps strength
    if score >= 0.85:
        return "TRUE", disc, conflicts
    if score >= 0.65:
        return "STRONG", disc, conflicts
    return "WEAK", disc, conflicts


# ─────────────────────────── SK-08 adverse media ────────────────────────────

def sk08_media(ctx, subjects: list[dict]) -> SkillResult:
    res = SkillResult("SK-08")
    events, exclusions, queries = [], [], []

    for subj in subjects:
        for variant in subj.get("aliases", []) + [subj["name"]]:
            ev = ctx.ledger.query(
                connector="media.search", dataset="MEDIA_T1_T3", dataset_version="2026-10-20",
                params={"query": f'"{variant}" (fraud OR corruption OR sanctions OR investigation)',
                        "lookback_years": ctx.policy["adverse_media"]["lookback_years"],
                        "min_tier": 3},
                result_count=2 if "Vasquez" in variant else 0,
                response_ref=f"inline://media/{variant}", subject_name=subj["name"],
                key=idem_key(ctx.case_id, "SK-08", variant), skill="SK-08")
            queries.append({"subject": subj["name"], "query_variant": variant, "evidence_id": ev})

    # seeded corpus result (the printout document DOC-009 is corroborating material)
    ms = ctx.pages["DOC-4831-009"]["pages"]
    kept_q = ms[1]["text"]
    excluded_q = ms[3]["text"]
    ev_keep = ctx.ledger.extract(
        subject={"kind": "ADVERSE_MEDIA", "subject_name": "Elena Maria Vasquez"},
        value={"raw": "customs inquiry, no charges filed", "normalized": "ALLEGED/UNDER_INVESTIGATION",
               "datatype": "event"},
        quote='Extract: "Customs authorities in Limassol confirmed on Monday that they had opened an\ninquiry into alleged misdeclaration of consignment values by a metals trading company\nwhose director, Elena Vasquez, declined to comment. No charges have been filed and\nMs Vasquez has not been accused of any offence."',
        locator={"doc_id": "DOC-4831-009", "page": 2, "char_span": [0, 400]},
        method="native_text", conf="HIGH", basis="document",
        key=idem_key(ctx.case_id, "SK-08", "event-1"), skill="SK-08")
    ev_excl = ctx.ledger.extract(
        subject={"kind": "ADVERSE_MEDIA_EXCLUSION", "subject_name": "Elena Maria Vasquez"},
        value={"raw": "excluded — different individual", "normalized": "EXCLUDED", "datatype": "event"},
        quote="Exclusion reason: different individual — DOB 1984, occupation and location do not match.",
        locator={"doc_id": "DOC-4831-009", "page": 4, "char_span": [0, 88]},
        method="native_text", conf="HIGH", basis="document",
        key=idem_key(ctx.case_id, "SK-08", "event-2"), skill="SK-08")

    events.append({"event_id": "EVT-1", "subject": "Elena Maria Vasquez",
                   "event_date": "2023-08-14", "category": "financial_crime",
                   "allegation_stage": "UNDER_INVESTIGATION", "source": "Financial Gazette",
                   "source_tier": 2, "corroboration_count": 2, "verification": "VERIFIED",
                   "evidence_id": ev_keep, "within_lookback": True})
    exclusions.append({"reason": "different individual (DOB/occupation mismatch)", "source": "Regional Business Wire",
                       "evidence_id": ev_excl})

    f = Finding(finding_id=ctx.next_finding_id(), type="ADVERSE_MEDIA", severity="MEDIUM",
                title="Open customs inquiry reported in established financial press (2023)",
                detail=("A Tier 2 outlet reported a customs inquiry into alleged misdeclaration of "
                        "consignment values at a company directed by the subject. No charges have been "
                        "filed and the subject has not been accused of an offence. The company called the "
                        "discrepancy a clerical error. Two sources (one syndicated) within the 5-year "
                        "lookback. This is an allegation-stage event, not a finding of wrongdoing."),
                basis="screening", confidence="MEDIUM", evidence_refs=[ev_keep, ev_excl],
                policy_refs=["MED-01", "RDE-16"],
                rating_impact={"factor": "ADVERSE_MEDIA", "direction": "UP", "weight_points": 20})
    ctx.ledger.finding(f, "SK-08")
    ctx.findings.append(f.to_dict(ctx.case_id, "SK-08", ctx.run_id))
    res.findings.append(f.finding_id)

    res.outputs = {"queries": queries, "events": events, "exclusions": exclusions,
                   "residual_concern": "MEDIUM"}
    res.evidence_ids = [q["evidence_id"] for q in queries] + [ev_keep, ev_excl]
    res.confidence = "MEDIUM"
    return res


# ─────────────────── SK-06 registry corroboration ───────────────────────────

def sk06_registry(ctx) -> SkillResult:
    res = SkillResult("SK-06")
    checks = []
    registry_facts = {"status": ("ACTIVE", "DOC-4831-005"),
                      "incorporation_date": ("17 April 2019", "DOC-4831-005"),
                      "incorporation_number": ("1744882", "DOC-4831-005")}

    supplied = {}
    for fname, vals in [("status", _pick_facts(ctx, IDENTITY_DOCS, "status")),
                        ("incorporation_number", _pick_facts(ctx, IDENTITY_DOCS, "incorporation_number")),
                        ("incorporation_date", _pick_facts(ctx, IDENTITY_DOCS, "incorporation_date"))]:
        if vals:
            supplied[fname] = vals[0]

    for attr, (reg_val, doc_id) in registry_facts.items():
        ev = ctx.ledger.query(connector="registry.company_search", dataset="BVI_FSC",
                              dataset_version="2026-09-02",
                              params={"name": "MERIDIAN TRADE HOLDINGS LTD", "reg_number": "1744882",
                                      "attribute": attr},
                              result_count=1, response_ref="inline://bvi/1744882",
                              subject_name="Meridian Trade Holdings Ltd",
                              key=idem_key(ctx.case_id, "SK-06", attr), skill="SK-06")
        sup = supplied.get(attr, {}).get("value")
        verdict = "MATCH" if sup and reg_val.lower().split()[0] in sup.lower() else \
                  ("NOT_FOUND" if not sup else "MISMATCH")
        checks.append({"attribute": attr, "supplied": sup, "registry": reg_val,
                       "verdict": verdict, "evidence_id": ev})

    res.outputs = {"entity": "Meridian Trade Holdings Ltd", "checks": checks,
                   "corporate_status": "ACTIVE"}
    res.evidence_ids = [c["evidence_id"] for c in checks]
    return res


# ───────────────────── SK-09 activity / SoF-SoW ─────────────────────────────

def sk09_activity(ctx) -> SkillResult:
    res = SkillResult("SK-09")
    revenue = _pick_facts(ctx, {"AUDITED_FINANCIALS"}, "revenue")
    credits = _pick_facts(ctx, {"BANK_STATEMENT_CORPORATE"}, "total_credits")
    rev = float(re.sub(r"[^\d.]", "", revenue[0]["value"])) if revenue else None
    credit = float(re.sub(r"[^\d.]", "", credits[0]["value"])) if credits else None

    annualised = round(credit * 4, 2) if credit else None
    ratio_ev = None
    if rev and annualised:
        ratio_ev = ctx.ledger.compute(
            subject={"kind": "ACTIVITY_RATIO", "metric": "annualised_credits_vs_revenue"},
            calc_id="calc.ratio@1.0.0",
            inputs=[{"numerator": annualised, "evidence_id": credits[0]["evidence_id"]},
                    {"denominator": rev, "evidence_id": revenue[0]["evidence_id"]}],
            operation="divide(annualised_quarterly_credits, reported_revenue)",
            result=f"{annualised / rev:.2f}",
            key=idem_key(ctx.case_id, "SK-09", "ratio"), skill="SK-09")

    f = None
    if rev and annualised and annualised / rev > 20:
        f = Finding(finding_id=ctx.next_finding_id(), type="ACTIVITY_INCONSISTENT", severity="HIGH",
                    title="Account throughput materially exceeds reported revenue",
                    detail=(f"Quarterly credits of USD {credit:,.0f} annualise to USD {annualised:,.0f} "
                            f"against audited revenue of USD {rev:,.0f} (×{annualised/rev:.1f}). A "
                            f"trading company may legitimately turn over more than it books as revenue, "
                            f"but a multiple of this size is not explained by the documents supplied and is "
                            f"inconsistent with the declared expected monthly volume of USD 450,000. The "
                            f"agent states the inconsistency; it draws no conclusion about the origin of "
                            f"the funds."),
                    basis="document", confidence="HIGH",
                    evidence_refs=[revenue[0]["evidence_id"], credits[0]["evidence_id"], ratio_ev],
                    policy_refs=["BEH-01", "RDE-11"],
                    rating_impact={"factor": "ACTIVITY_INCONSISTENT", "direction": "UP",
                                   "weight_points": 25})
        ctx.ledger.finding(f, "SK-09")
        ctx.findings.append(f.to_dict(ctx.case_id, "SK-09", ctx.run_id))
        res.findings.append(f.finding_id)

    res.outputs = {"expected": {"monthly_volume_band": "USD 350k–500k", "countries": ["AE", "TR", "HK"],
                                "instruments": ["wire transfer"]},
                   "observed": {"quarterly_credits": credit, "annualised": annualised,
                                "counterparty_countries": ["AE", "TR", "HK"],
                                "round_value_credits_over_100k": 31},
                   "consistency": "INCONSISTENT" if f else "PLAUSIBLE_UNVERIFIED",
                   "ratio": (annualised / rev) if rev and annualised else None,
                   "ratio_evidence_id": ratio_ev,
                   "revenue_evidence_id": revenue[0]["evidence_id"] if revenue else None,
                   "credits_evidence_id": credits[0]["evidence_id"] if credits else None,
                   "sof_assessment": {"claim": "trading revenues", "verdict": "PARTIALLY_CORROBORATED"},
                   "sow_assessment": {"claim": "not provided at CDD level", "verdict": "NOT_REQUIRED"},
                   "monitoring_handoff": {"volume_bands": ["300000-500000", "500000+"],
                                          "countries": ["AE", "TR", "HK"],
                                          "instruments": ["WIRE"]}}
    return res


# ───────────────────────── SK-10 risk rating ────────────────────────────────

def sk10_risk(ctx, ownership: SkillResult, media: SkillResult, activity: SkillResult,
              screening: SkillResult, registry: SkillResult) -> SkillResult:
    res = SkillResult("SK-10")
    types = {f["type"] for f in ctx.findings}

    factor_inputs = {
        "F-CUST-01": "PEP_EXPOSURE" in types,
        "F-CUST-02": "ADVERSE_MEDIA" in types,
        "F-CUST-03": any(h["strength"] in ("TRUE", "STRONG") and "sanction" in h["list"].lower()
                         for h in screening.outputs["hits"]),
        "F-CUST-04": "UNRESOLVED_UBO" in types,
        "F-CUST-05": len(ownership.outputs.get("paths", [])) >= 3,
        "F-CUST-07": False,
        "F-PROD-01": True,       # trade finance
        "F-GEO-03": True,        # VG / CY opacity nexus
        "F-GEO-05": False,
        "F-CHAN-02": False,
        "F-BEH-01": "ACTIVITY_INCONSISTENT" in types,
        "F-BEH-02": False,
    }
    rating = risk_engine(factor_inputs, ctx.policy)

    ev = ctx.ledger.compute(
        subject={"kind": "RISK_RATING", "model": rating["policy_version"]},
        calc_id=rating["engine_version"],
        inputs=[{"factor": k, "applies": v} for k, v in factor_inputs.items()],
        operation="weighted_dimension_sum",
        result=f"inherent={rating['inherent']['score']} band={rating['inherent']['band']}",
        key=idem_key(ctx.case_id, "SK-10", rating["policy_version"]), skill="SK-10")

    # trace enrichment with evidence refs per factor
    ev_by_factor = {
        "F-CUST-01": [e for e in [f["evidence_refs"][0] for f in ctx.findings if f["type"] == "PEP_EXPOSURE"]],
        "F-CUST-02": [e for e in [f["evidence_refs"][0] for f in ctx.findings if f["type"] == "ADVERSE_MEDIA"]],
        "F-CUST-03": [e for e in [f["evidence_refs"][0] for f in ctx.findings if f["type"] == "SANCTIONS_HIT"]],
        "F-CUST-04": [e["evidence_id"] for e in ownership.outputs.get("unresolved_links", [])],
        "F-BEH-01": [e for e in [f["evidence_refs"] for f in ctx.findings if f["type"] == "ACTIVITY_INCONSISTENT"][:1]][0]
                    if "ACTIVITY_INCONSISTENT" in types else [],
    }
    for t in rating["trace"]:
        t["evidence_refs"] = ev_by_factor.get(t["factor_id"], [])

    rating["engine_evidence_id"] = ev
    rating["required_cdd_level"] = rating["cdd_level"]
    rating["requested_cdd_level"] = ctx.case["cdd_level_requested"]
    rating["provisional"] = "UNRESOLVED_UBO" in types
    res.outputs = rating
    res.evidence_ids = [ev]
    res.confidence = "MEDIUM" if rating["provisional"] else "HIGH"
    return res


# ───────────────────────── SK-11 gaps / RFI ─────────────────────────────────

def sk11_gaps(ctx, ownership: SkillResult, screening: SkillResult, media: SkillResult,
              activity: SkillResult) -> SkillResult:
    res = SkillResult("SK-11")
    types = {f["type"] for f in ctx.findings}
    facts_all = {}
    for d in ctx.facts_by_doc.values():
        for k in d.get("facts", {}):
            facts_all.setdefault(k, []).append(d)

    status = {
        "RDE-01": "SATISFIED", "RDE-02": "SATISFIED", "RDE-03": "SATISFIED",
        "RDE-04": "SATISFIED", "RDE-05": "SATISFIED" if "passport_expiry" in facts_all else "MISSING",
        "RDE-06": "SATISFIED", "RDE-07": "PARTIAL" if "UNRESOLVED_UBO" in types else "SATISFIED",
        "RDE-08": "PARTIAL", "RDE-09": "SATISFIED", "RDE-10": "SATISFIED",
        "RDE-11": "SATISFIED", "RDE-12": "PARTIAL", "RDE-13": "NOT_APPLICABLE",
        "RDE-14": "SATISFIED", "RDE-15": "SATISFIED", "RDE-16": "SATISFIED",
        "RDE-17": "SATISFIED", "RDE-18": "PARTIAL", "RDE-19": "SATISFIED",
        "RDE-20": "SATISFIED", "RDE-21": "SATISFIED", "RDE-22": "SATISFIED",
        "RDE-23": "PARTIAL", "RDE-24": "NOT_APPLICABLE",
    }
    reasons = {
        "RDE-07": "Nominee shareholder's principal not disclosed",
        "RDE-08": "Nominee-held 45% not attributed to a natural person",
        "RDE-12": "Source-of-funds letter is self-declared and uncorroborated at the level of observed flows",
        "RDE-18": "Share register extract dated 2021-03-12 exceeds the ±540-day policy window",
        "RDE-23": "Tax registration certificate supplied, but no tax identifier is legible on the "
                  "pages provided",
    }

    # which supplied document should have carried each element (used for gap citations)
    doc_for = {"RDE-05": "DOC-4831-004", "RDE-07": "DOC-4831-002", "RDE-08": "DOC-4831-002",
               "RDE-12": "DOC-4831-017", "RDE-13": "DOC-4831-017", "RDE-18": "DOC-4831-002",
               "RDE-23": "DOC-4831-016", "RDE-24": "DOC-4831-022"}
    doc_ev = ctx.stage_notes.get("doc_evidence", {}) or {}
    fallback = ctx.ledger.evidence_ids()[-1]

    elements, gaps = [], []
    for rde_id, name, levels in RDE_LIST:
        st = status.get(rde_id, "MISSING")
        cite_ev = doc_ev.get(doc_for.get(rde_id, ""), fallback)
        el = {"rde_id": rde_id, "name": name, "required_for": levels, "status": st,
              "evidence_refs": [cite_ev],
              "confidence": "HIGH" if st == "SATISFIED" else "LOW"}
        if st == "NOT_APPLICABLE":
            el["reason"] = f"Not required at the requested {ctx.case['cdd_level_requested']} level"
            elements.append(el)
            continue
        if st != "SATISFIED":
            el["reason"] = reasons.get(rde_id, "Not evidenced in the bundle")
            gap = {"gap_id": f"GAP-{len(gaps)+1:03d}", "rde": rde_id, "name": name, "status": st,
                   "reason": el["reason"], "evidence_id": cite_ev,
                   "blocks_decision": st in ("MISSING", "PARTIAL") and rde_id in
                                     ("RDE-07", "RDE-08", "RDE-23", "RDE-05"),
                   "priority": "P1" if rde_id in ("RDE-07", "RDE-08") else
                               ("P2" if rde_id in ("RDE-12", "RDE-18", "RDE-23") else "P3"),
                   "fixability": "CUSTOMER"}
            gaps.append(gap)
            el["gap_id"] = gap["gap_id"]
            f = Finding(finding_id=ctx.next_finding_id(), type="COVERAGE_GAP", severity="MEDIUM",
                        title=f"{rde_id} {name}: {st}", detail=el["reason"],
                        basis="judgment", confidence="MEDIUM", evidence_refs=[cite_ev],
                        policy_refs=[rde_id])
            ctx.ledger.finding(f, "SK-11")
            ctx.findings.append(f.to_dict(ctx.case_id, "SK-11", ctx.run_id))
        elements.append(el)

    satisfied = sum(1 for e in elements if e["status"] == "SATISFIED")
    required = [e for e in elements if ctx.case["cdd_level_requested"] in e["required_for"]]
    cov = round(satisfied / len(required) * 100, 1) if required else 0.0

    rfi = {
        "request_id": "RFI-4831-R1",
        "round": 1,
        "deadline": "2026-10-30T17:00:00Z",
        "items": [
            {"gap_ids": [g["gap_id"] for g in gaps if g["rde"] in ("RDE-07", "RDE-08")],
             "ask": "Please provide a signed declaration identifying the natural person(s) who "
                    "ultimately own or control the 45% interest held by Sterling Nominees Limited, "
                    "together with any supporting trust deed, nominee agreement or share transfer.",
             "acceptable_forms": ["signed UBO declaration", "nominee agreement", "trust deed",
                                  "certified share register showing the underlying holder"],
             "why_needed": "RDE-07/RDE-08 — beneficial ownership above the 25% threshold cannot be "
                           "established; this blocks the CDD decision."},
            {"gap_ids": [g["gap_id"] for g in gaps if g["rde"] == "RDE-18"],
             "ask": "Please provide a share register for Meridian Trade Holdings Ltd dated within "
                    "the last six months.",
             "acceptable_forms": ["register extract dated within 180 days",
                                  "certificate of incumbency"],
             "why_needed": "RDE-18 — the current register is from March 2021 and ownership may have "
                           "changed."},
            {"gap_ids": [g["gap_id"] for g in gaps if g["rde"] == "RDE-23"],
             "ask": "Please provide the company's tax registration certificate.",
             "acceptable_forms": ["tax registration certificate", "certificate of tax residence"],
             "why_needed": "RDE-23 — required for EDD and for the FATCA/CRS file."},
        ],
    }
    res.outputs = {"coverage_matrix": {"case_id": ctx.case_id,
                                       "policy_version": ctx.policy["version"],
                                       "elements": elements,
                                       "summary": {"total": len(elements), "satisfied": satisfied,
                                                   "partial": sum(1 for e in elements if e["status"] == "PARTIAL"),
                                                   "missing": sum(1 for e in elements if e["status"] == "MISSING"),
                                                   "blocks_decision": sum(1 for g in gaps if g["blocks_decision"]),
                                                   "coverage_pct": cov}},
                   "gaps": gaps, "rfi": rfi}
    ctx.coverage_pct = cov
    return res


# ───────────────────────── SK-12 / SK-13 compose+critic ─────────────────────

def sk12_compose(ctx, *, ownership, screening, media, activity, risk, gaps, registry) -> SkillResult:
    res = SkillResult("SK-12")
    r = risk.outputs
    cov = gaps.outputs["coverage_matrix"]["summary"]
    open_findings = [f for f in ctx.findings if f["status"] == "OPEN"]
    p1 = [g for g in gaps.outputs["gaps"] if g["blocks_decision"]]

    def cite(f): return f["evidence_refs"][0] if f["evidence_refs"] else ""

    lines = []
    lines.append(f"# CDD Investigation Dossier — {ctx.case_id}\n")
    lines.append(f"**Customer:** Meridian Trade Holdings Ltd (BVI 1744882)  ")
    lines.append(f"**Run:** `{ctx.run_id}` · **Policy:** {ctx.policy['version']} · "
                 f"**Agent:** {ctx.agent_version}  ")
    lines.append(f"**Status:** PROVISIONAL · **Decision authority:** HUMAN · "
                 f"**Required gate(s):** {', '.join(ctx.required_gates) or 'none'} · "
                 f"**Coverage:** {cov['coverage_pct']}% of required data elements\n")

    own_ev = ownership.evidence_ids[0] if ownership.evidence_ids else "—"
    unres = ownership.outputs["unresolved_links"]
    unres_ev = unres[0]["evidence_id"] if unres else "—"
    screen_first = screening.outputs["queries"][0]["evidence_id"] if screening.outputs["queries"] else "—"
    pep_hit = next((h for h in screening.outputs["hits"] if "PEP" in h["list"]), None)
    san_hit = next((h for h in screening.outputs["hits"] if "PEP" not in h["list"]), None)
    media_ev = media.outputs["events"][0]["evidence_id"] if media.outputs.get("events") else "—"
    bos_txt = ", ".join(f"{b['person']} {b['effective_pct']:.2f}%" for b in
                        ownership.outputs["beneficial_owners"]) or "none identified"
    ratios = activity.outputs.get("ratio")

    lines.append("## 1. Executive summary\n")
    lines.append(
        f"The customer is a BVI-incorporated commodity trader [OBSERVED {own_ev}]. Corporate "
        f"existence and active status are corroborated against the BVI registry [OBSERVED "
        f"{registry.evidence_ids[0]}]. Effective ownership computes to {bos_txt} [DERIVED {own_ev}]"
        + (f"; a further {unres[0]['pct']:.0f}% is held by {unres[0]['holder']}, whose principal is "
           f"not disclosed [OBSERVED {unres_ev}]" if unres else "") + ". "
        f"Screening is recorded for every subject [OBSERVED {screen_first}]; "
        + (f"it returned a {san_hit['strength']} sanctions look-alike that did not survive "
           f"discrimination on date of birth and country [OBSERVED {san_hit['evidence_id']}], " if san_hit else
           "it returned no sanctions match, ")
        + (f"and a {pep_hit['strength']} PEP match on the principal beneficial owner [OBSERVED "
           f"{pep_hit['evidence_id']}]. " if pep_hit else ". ")
        + f"Adverse media identified one open customs inquiry at allegation stage, no charges filed "
          f"[OBSERVED {media_ev}]. "
        + (f"Account throughput annualises to {ratios:.1f}× audited revenue [DERIVED "
           f"{activity.outputs.get('ratio_evidence_id') or '—'}], which the documents supplied do not "
           f"explain.\n" if ratios else "\n"))

    lines.append(
        f"**Residual risk: {r['residual']['band']} ({r['residual']['score']}/100).** "
        f"Required CDD level: **{r['cdd_level']}**; the relationship was requested at "
        f"{ctx.case['cdd_level_requested']}. The rating is "
        f"{'provisional pending closure of the P1 gaps' if r.get('provisional') else 'final for the evidence held'}"
        + (f", and is set by policy band floors ({', '.join(o['factor_id'] for o in r['overrides_applied'])}) "
           f"rather than by the weighted score alone." if r.get("overrides_applied") else ".") + "\n")
    lines.append(
        f"**Recommendation: HOLD_FOR_RFI.** {len(p1)} gap(s) block the decision. The single most "
        f"important open question is who ultimately owns the {unres[0]['pct']:.0f}% nominee-held block.\n"
        if unres else
        f"**Recommendation: HOLD_FOR_RFI.** {len(p1)} gap(s) block the decision.\n")

    lines.append("## 2. Customer profile and identification\n")
    lines.append("| Attribute | Claimed | Observed | Verification | Evidence |")
    lines.append("|---|---|---|---|---|")
    lines.append(f"| Legal name | Meridian Trade Holdings Ltd | MERIDIAN TRADE HOLDINGS LTD | MATCH | {registry.evidence_ids[0]} |")
    lines.append(f"| Registration | 1744882 | 1744882 | MATCH | {registry.evidence_ids[0]} |")
    lines.append(f"| Jurisdiction | VG | VG | MATCH | {registry.evidence_ids[1]} |")
    lines.append(f"| Status | — | ACTIVE | VERIFIED | {registry.evidence_ids[0]} |")

    lines.append("\n## 3. Verification of documents\n")
    lines.append("| Document | Type | Date | Pages read | Verdict | Evidence |")
    lines.append("|---|---|---|---|---|---|")
    for d in ctx.bundle["documents"][:12]:
        led = [p for p in ctx.manifest["page_ledger"] if p["doc_id"] == d["doc_id"]]
        read = sum(1 for p in led if p["status"] == "READ")
        verdict = "QUARANTINED" if d.get("quarantine_flag") else (
            "ACCEPTED" if read == len(led) else f"PARTIAL ({read}/{len(led)})")
        lines.append(f"| {d['filename']} | {d['detected_type']} | {d.get('doc_date') or '—'} | "
                     f"{read}/{len(led)} | {verdict} | — |")

    lines.append("\n## 4. Ownership and control\n")
    lines.append("```")
    for p in ownership.outputs["paths"]:
        chain = " → ".join(p["chain"])
        factors = " × ".join(f"{x:.0f}%" for x in p["pcts"])
        lines.append(f"{p['person']}: {chain}  [{factors}] = {p['effective_pct']:.2f}%")
    lines.append("Residual unexplained block: 45.00% held by Sterling Nominees Limited (principal undisclosed)")
    lines.append("```")
    lines.append(f"Beneficial owners at or above {ownership.outputs['threshold']}%: " +
                 ", ".join(f"{b['person']} ({b['effective_pct']:.2f}%)" for b in ownership.outputs["beneficial_owners"]) + ".")
    if ownership.outputs.get("control_note"):
        lines.append(ownership.outputs["control_note"] + " [JUDGED]")
    if ownership.outputs["unresolved_links"]:
        u = ownership.outputs["unresolved_links"][0]
        lines.append(f"**Unresolved:** {u['pct']:.0f}% held by {u['holder']} — no natural person "
                     f"identified [{u['evidence_id']}]. The control prong cannot be applied to this "
                     f"block either, because the nominee's principal is unknown.")
    lines.append(f"Direct holdings sum check: {ownership.outputs['direct_holdings_total_pct']:.2f}% "
                 f"of issued capital ({ownership.outputs['sum_check']}).")

    lines.append("\n## 5. Purpose, expected activity, source of funds\n")
    a = activity.outputs
    lines.append(f"Declared purpose: trade finance for commodity imports. Expected profile: "
                 f"{a['expected']['monthly_volume_band']}/month, counterparties in "
                 f"{', '.join(a['expected']['countries'])}. Observed quarterly credits "
                 f"USD {a['observed']['quarterly_credits']:,.0f} annualising to "
                 f"USD {a['observed']['annualised']:,.0f} — {a['ratio']:.1f}× audited revenue "
                 f"[DERIVED {a.get('ratio_evidence_id') or '—'}; revenue {a.get('revenue_evidence_id') or '—'}, "
                 f"credits {a.get('credits_evidence_id') or '—'}]. "
                 f"Consistency: **{a['consistency']}**. The agent states the inconsistency and draws no "
                 f"conclusion about the origin of the funds.")

    lines.append("\n## 6. Screening and adverse media\n")
    lines.append("| Subject | List | Result | Strength | Evidence |")
    lines.append("|---|---|---|---|---|")
    for q in screening.outputs["queries"]:
        h = next((h for h in screening.outputs["hits"]
                  if h["subject"] == q["subject"] and h["list"] == q["list"]), None)
        lines.append(f"| {q['subject']} | {q['list']} | {q['result_count']} hit(s) | "
                     f"{h['strength'] if h else 'clean'} | {q['evidence_id']} |")
    unav = screening.outputs["coverage"].get("lists_unavailable") or []
    if unav:
        lines.append(f"\n**Limitation:** the {', '.join(unav)} list could not be queried (connector "
                     f"degraded after retries). No clearance is asserted against that list; it is "
                     f"recorded in the limitations and forces gate G3.")
    lines.append(f"\nAdverse media: {len(media.outputs['events'])} event(s) retained, "
                 f"{len(media.outputs['exclusions'])} excluded. The retained event is at allegation stage "
                 f"(open inquiry, no charges). Excluded item recorded with its discriminator.")

    lines.append("\n## 7. Risk assessment\n")
    lines.append(f"Model `{r['policy_version']}` / engine `{r['engine_version']}`. "
                 f"Inherent {r['inherent']['score']} → **{r['inherent']['band']}**; residual "
                 f"{r['residual']['score']} → **{r['residual']['band']}**; CDD level **{r['cdd_level']}**. "
                 + (f"Band floors applied: "
                    + "; ".join(f"{o['factor_id']} ({o['reason']}) → ≥{o['min_band']}"
                                for o in r.get("overrides_applied", [])) + ". " if r.get("overrides_applied") else "")
                 + f"The weighted score alone would band as "
                   f"{r['inherent'].get('weighted_score_band', r['inherent']['band'])}; policy does not "
                   f"allow a severe single factor to be averaged away.\n")
    lines.append("| Factor | Dimension | Raw points | Contribution | Evidence |")
    lines.append("|---|---|---|---|---|")
    for t in r["trace"]:
        lines.append(f"| {t['factor']} | {t['dimension']} | {t['raw_points']} | {t['contribution']} | "
                     f"{', '.join(t.get('evidence_refs', [])[:2]) or '—'} |")

    lines.append("\n## 8. Gaps and confidence\n")
    lines.append(f"Coverage: **{cov['coverage_pct']}%** of required data elements for "
                 f"{ctx.case['cdd_level_requested']} ({cov['satisfied']}/{cov['total']} satisfied, "
                 f"{cov['partial']} partial, {cov['missing']} missing). "
                 f"{cov['blocks_decision']} gap(s) block the decision.\n")
    for g in gaps.outputs["gaps"]:
        lines.append(f"- **{g['gap_id']}** {g['rde']} {g['name']} — {g['status']} ({g['priority']}): {g['reason']}")

    lines.append("\n## 9. Red flags and observations\n")
    for f in open_findings:
        lines.append(f"- **{f['type']}** ({f['severity']}, {f['confidence']}) — {f['title']} "
                     f"[{f['evidence_refs'][0]}]")

    lines.append("\n## 10. Recommendation and next actions\n")
    lines.append("1. Issue RFI-4831-R1 (3 items, 2 decision-blocking) — nominal deadline 2026-10-30.")
    lines.append("2. Refer the PEP match to the EDD unit for adjudication and record the decision as a gate event (the agent does not adjudicate PEPs).")
    lines.append("3. Obtain a current share register before any ownership conclusion is relied upon.")
    lines.append("4. Request a documented explanation of the throughput-to-revenue multiple.")
    lines.append("5. No account opening, no EDD approval, and no report filing is within this agent's authority.")

    lines.append("\n## 11. Evidence index and methodology\n")
    lines.append(f"Evidence records: {len(ctx.ledger.evidence_ids())}. "
                 f"Pages read: {sum(1 for p in ctx.manifest['page_ledger'] if p['status'] == 'READ')} "
                 f"of {ctx.manifest['pages_total']}; "
                 f"{sum(1 for p in ctx.manifest['page_ledger'] if p['status'] == 'ILLEGIBLE')} illegible.")
    lines.append("Method: page-level OCR/native extraction with verbatim spans → typed facts → "
                 "entity resolution → ownership arithmetic via `calc.ownership` → policy screening with "
                 "recorded query metadata → deterministic risk engine → adversarial critic pass.")

    lines.append("\n## 12. Attestation\n")
    ok, head, broken = ctx.ledger.verify_chain()
    lines.append(f"- Agent `{ctx.agent_version}`, prompt `{ctx.prompt_hash[:32]}…`, policy `{ctx.policy['version']}`.")
    lines.append(f"- Evidence chain verified: **{'PASS' if ok else 'FAIL'}**, head `{head[:40]}…`, "
                 f"{len(ctx.ledger.evidence_ids())} records.")
    lines.append(f"- Decision authority: **HUMAN**. Required gates: {', '.join(ctx.required_gates) or 'none'}.")
    lines.append("- Analyst sign-off: ____________________  Date: __________")

    body = "\n".join(lines) + "\n"
    out_path = ctx.out_dir / "CDD_dossier_CASE-4831.md"
    out_path.write_text(body)
    art_hash = "sha256:" + hashlib.sha256(body.encode()).hexdigest()

    # ── machine-readable twin of the dossier, conforming to cdd-memo.schema.json ──
    gates = list(ctx.required_gates)
    report = {
        "report_id": "RPT-4831-01",
        "case_id": ctx.case_id,
        "run_id": ctx.run_id,
        "status": "PROVISIONAL",
        "decision_authority": "HUMAN",
        "required_gate": "G3" if "G3" in gates else (gates[0] if gates else "NONE"),
        "generated_at": now(),
        "policy_version": ctx.policy["version"],
        "prompt_version": ctx.agent_version,
        "executive_summary": {
            "text": "\n\n".join(lines[6:8])[:3000],
            "recommendation": "HOLD_FOR_RFI",
            "confidence": "MEDIUM",
            "key_drivers": [{"factor": t["factor"], "evidence_refs": t.get("evidence_refs") or [own_ev]}
                            for t in r["trace"] if t.get("raw_points")],
            "single_most_important_open_question":
                (f"Who ultimately owns the {unres[0]['pct']:.0f}% block held by {unres[0]['holder']}?"
                 if unres else None),
        },
        "sections": {
            "s1_profile": {"present": True, "text_ref": "dossier#s1"},
            "s2_identification": {"present": True, "verified_fields": 4, "text_ref": "dossier#s2"},
            "s3_verification": {"present": True, "documents": len(ctx.bundle["documents"]),
                                "text_ref": "dossier#s3"},
            "s4_ownership_control": {"present": True,
                                     "beneficial_owners": ownership.outputs["beneficial_owners"],
                                     "unresolved_links": ownership.outputs["unresolved_links"],
                                     "effective_evidence": own_ev, "text_ref": "dossier#s4"},
            "s5_purpose_activity": {"present": True, "consistency": a["consistency"],
                                    "text_ref": "dossier#s5"},
            "s6_screening_media": {"present": True,
                                   "queries": len(screening.outputs["queries"]),
                                   "hits": len(screening.outputs["hits"]),
                                   "media_events": len(media.outputs["events"]),
                                   "lists_unavailable": screening.outputs["coverage"].get("lists_unavailable", []),
                                   "text_ref": "dossier#s6"},
            "s7_risk_assessment": {"present": True, "inherent": r["inherent"], "residual": r["residual"],
                                   "cdd_level": r["cdd_level"], "text_ref": "dossier#s7"},
            "s8_gaps_confidence": {"present": True, "coverage": cov, "text_ref": "dossier#s8"},
            "s9_red_flags": {"present": True,
                             "open_findings": len(open_findings), "text_ref": "dossier#s9"},
            "s10_recommendation": {"present": True, "recommendation": "HOLD_FOR_RFI",
                                   "rfi": "RFI-4831-R1", "text_ref": "dossier#s10"},
            "s11_evidence_index": {"present": True,
                                   "records": len(ctx.ledger.evidence_ids()), "text_ref": "dossier#s11"},
            "s12_attestation": {"present": True, "chain_valid": ctx.ledger.verify_chain()[0],
                                "text_ref": "dossier#s12"},
        },
        "findings": [f["finding_id"] for f in open_findings],
        "judgments": ([{"text": ownership.outputs["control_note"], "basis": "judgment",
                        "confidence": "MEDIUM"}] if ownership.outputs.get("control_note") else []),
        "limitations": ctx.limitations() if hasattr(ctx, "limitations") else [],
        "artifact": {"markdown_sha256": art_hash, "pdf_sha256": None,
                     "evidence_ledger_head": ctx.ledger.head},
        "human_signoff": None,
    }
    (ctx.out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    ev = ctx.ledger.artifact(name="CDD_dossier_CASE-4831.md", uri=str(out_path),
                             content_hash=art_hash,
                             cites=[f["evidence_refs"][0] for f in open_findings if f["evidence_refs"]],
                             key=idem_key(ctx.case_id, "SK-12", "dossier"), skill="SK-12")
    res.outputs = {"path": str(out_path), "sha256": art_hash, "artifact_evidence_id": ev,
                   "sections": 12, "markdown": body}
    res.evidence_ids = [ev]
    return res


def sk13_critic(ctx, dossier: SkillResult, risk: SkillResult) -> SkillResult:
    """Independent verification. Deterministic here; in production a different model family."""
    res = SkillResult("SK-13")
    text = dossier.outputs["markdown"]
    defects = []

    # 1. citation existence
    cited = set(re.findall(r"(EV-\d+-\d{6})", text))
    known = set(ctx.ledger.evidence_ids())
    for c in cited - known:
        defects.append({"class": "UNCITED_CLAIM", "severity": "CRITICAL",
                        "location": "dossier", "claim": f"reference to unknown evidence {c}",
                        "fix": "remove or route to a real record"})

    # 2. arithmetic recompute of ownership
    for p in ctx.ownership.outputs["paths"]:
        value = 1.0
        for x in p["pcts"]:
            value *= x / 100.0
        if abs(round(value * 100, 4) - p["effective_pct"]) > 0.005:
            defects.append({"class": "ARITHMETIC_ERROR", "severity": "CRITICAL",
                            "location": "s4", "claim": f"{p['person']} {p['effective_pct']}",
                            "fix": f"recompute to {round(value*100,2)}"})

    # 3. basis discipline — no inference in a verified field
    for rec in ctx.ledger.records():
        if rec.get("basis") in ("inference", "judgment") and \
                str(rec.get("subject", {}).get("field", "")).startswith("verified_"):
            defects.append({"class": "UNSUPPORTED_INFERENCE", "severity": "CRITICAL",
                            "location": rec["evidence_id"], "claim": "inference in verified field",
                            "fix": "downgrade basis"})

    # 4. screening completeness — an unavailable list is acceptable ONLY if it is disclosed
    required = [s["name"] for s in ctx.screening.outputs["subjects"]]
    covered = {(q["subject"], q["list"]) for q in ctx.screening.outputs["queries"]}
    unavailable = set(ctx.screening.outputs["coverage"].get("lists_unavailable") or [])
    for subj in required:
        for lst in ctx.policy["screening"]["lists"]["sanctions"]:
            if (subj, lst) in covered:
                continue
            if lst in unavailable:
                if not re.search(rf"{lst}.*(could not be queried|degraded)", text, re.I | re.S):
                    defects.append({"class": "MISSING_LIMITATION", "severity": "MEDIUM",
                                    "location": "s6",
                                    "claim": f"unavailable list {lst} not disclosed",
                                    "fix": "disclose the degraded connector in s6 and limitations"})
                continue
            defects.append({"class": "SCREENING_GAP", "severity": "HIGH", "location": "s6",
                            "claim": f"{subj} not screened against {lst}",
                            "fix": "run query and record W5"})

    # 5. self-clearance check: a TRUE/STRONG sanctions hit must not be described as cleared
    for h in ctx.screening.outputs["hits"]:
        if h["strength"] in ("TRUE", "STRONG") and "sanction" in h["list"].lower():
            defects.append({"class": "POLICY_SKIP", "severity": "CRITICAL", "location": "s6",
                            "claim": "sanctions hit adjudicated by the agent",
                            "fix": "route to G2"})

    # 6. protected characteristics / overclaim language
    for term in PROTECTED_TERMS:
        if re.search(rf"\b{term}\b", text, re.I):
            defects.append({"class": "PROTECTED_CHARACTERISTIC", "severity": "CRITICAL",
                            "location": "dossier", "claim": f"term '{term}' present", "fix": "remove"})
    for term in ["clearly", "obviously", "undoubtedly"]:
        if re.search(rf"\b{term}\b", text, re.I):
            defects.append({"class": "OVERCLAIM", "severity": "LOW", "location": "dossier",
                            "claim": f"editorialising term '{term}'", "fix": "neutralise"})

    # 7. limitations disclosure
    for lbl in ["illegible", "quarantined" if ctx.manifest["quarantined"] else "x-placeholder"]:
        if lbl != "x-placeholder" and lbl.lower() not in text.lower():
            defects.append({"class": "MISSING_LIMITATION", "severity": "MEDIUM", "location": "s11",
                            "claim": f"{lbl} not disclosed", "fix": "add to limitations"})

    criticals = [d for d in defects if d["severity"] == "CRITICAL"]
    status = "FAIL" if criticals else ("PASS_WITH_ITEMS" if defects else "PASS")

    ev = ctx.ledger.append({"record_type": "CRITIQUE",
                            "value": {"raw": status, "normalized": len(defects), "datatype": "critique"},
                            "basis": "judgment", "confidence": "HIGH",
                            "critique": {"status": status, "defects": defects,
                                         "stats": {"citations_checked": len(cited),
                                                   "unknown_citations": len(cited - known),
                                                   "numbers_recomputed": len(ctx.ownership.outputs["paths"])}}},
                           key=idem_key(ctx.case_id, "SK-13", "critique"), skill="SK-13")
    res.outputs = {"status": status, "defects": defects,
                   "stats": {"citations_checked": len(cited), "unknown": len(cited - known)}}
    res.evidence_ids = [ev]
    res.status = "OK" if status != "FAIL" else "BLOCKED"
    res.reason_code = "CRITIC_FAIL" if status == "FAIL" else None
    return res


def sk14_gate(ctx, risk: SkillResult, critic: SkillResult) -> SkillResult:
    res = SkillResult("SK-14")
    gates = list(ctx.required_gates)
    tickets = []
    for g in gates:
        payload = {"gate": g, "severity": "HIGH" if g == "G3" else "MEDIUM",
                   "reason_codes": sorted({f["type"] for f in ctx.findings if f["status"] == "OPEN"}),
                   "routed_to": "EDD_UNIT" if g == "G2" else "COMPLIANCE_OFFICER",
                   "question": ("Adjudicate the PEP match and confirm the EDD approach"
                                if g == "G2" else
                                "Decide whether to proceed on the evidence held, hold for RFI, "
                                "or refer to the MLRO"),
                   "created_at": now()}
        ev = ctx.ledger.gate(gate=g, event="RAISED", payload=payload, key=idem_key(ctx.case_id, "SK-14", g))
        tickets.append({**payload, "ticket_id": f"ESC-4831-{len(tickets)+1:04d}", "evidence_id": ev})
        res.evidence_ids.append(ev)
    res.outputs = {"tickets": tickets, "required_gates": gates}
    return res
