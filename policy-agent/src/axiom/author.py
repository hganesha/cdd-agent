"""Policy authoring agent (SK-P5).

Turns prose obligations from procedures/SOPs into a *reviewable* policy change proposal:

  1. parse the source document into numbered clauses and quote them;
  2. classify each clause and check whether the current policy implements it;
  3. emit a candidate change (a data patch or a rule patch) with metadata and tests;
  4. compile the candidate under restricted capabilities and run the existing test suite;
  5. simulate the change against a decision corpus and report exactly which decisions move;
  6. emit a DRAFT proposal with the approvals required. It never activates anything.

In production steps 2–4 are model-assisted (constrained generation + tool validation); the harness
below is deterministic so the whole pipeline is testable offline.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .opa import OPAClient, OPAError

ROOT = Path(__file__).resolve().parents[2]

def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


CLAUSE_RE = re.compile(r"^(?P<id>(?:\d+\.){1,3}\d+)\s+(?P<text>.+?)(?=\n(?:\d+\.){1,3}\d+\s|\Z)",
                       re.M | re.S)

KEYWORDS = {
    "THRESHOLD": ["threshold", "per cent", "%", "percentage", "minimum", "at least", "no less than"],
    "MANDATORY_STEP": ["must", "shall", "is required", "are required", "obtain", "verify", "record"],
    "PROHIBITION": ["must not", "shall not", "may not", "prohibited", "may never", "is not permitted"],
    "ESCALATION": ["escalate", "refer", "notify the mlro", "senior management approval", "approval by"],
    "DEADLINE": ["within", "days", "hours", "business day", "promptly"],
    "DEFINITION": ["means", "is defined as", "refers to"],
}

# how a clause type maps to an implementation surface, and to the approvals that follow
IMPLEMENTATION = {
    "THRESHOLD": ("data_patch", ["POLICY_OWNER", "FINANCIAL_CRIME"]),
    "DEADLINE": ("data_patch", ["POLICY_OWNER", "FINANCIAL_CRIME"]),
    "MANDATORY_STEP": ("rule_patch", ["POLICY_OWNER", "FINANCIAL_CRIME", "MODEL_RISK"]),
    "PROHIBITION": ("rule_patch", ["POLICY_OWNER", "FINANCIAL_CRIME", "MODEL_RISK", "LEGAL"]),
    "ESCALATION": ("data_patch", ["POLICY_OWNER", "FINANCIAL_CRIME"]),
    "DEFINITION": ("none", ["POLICY_OWNER"]),
}


@dataclass
class Clause:
    clause_id: str
    text: str
    kind: str
    keywords: list[str]
    implemented: bool
    evidence: list[str] = field(default_factory=list)


class PolicyAuthor:
    def __init__(self, opa: OPAClient, root: Path = ROOT):
        self.opa = opa
        self.root = Path(root)
        self.policy_dir = self.root / "policy"
        self.corpus = self.root / "corpus"
        self.drafts = self.root / "proposals"

    # ── 1. parse ──
    def parse(self, doc_path: Path) -> list[Clause]:
        doc_path = Path(doc_path).resolve()
        text = doc_path.read_text()
        clauses = []
        for m in CLAUSE_RE.finditer(text):
            body = " ".join(m.group("text").split())
            if len(body) < 25:
                continue
            kind, kws = self._classify(body)
            implemented, evidence = self._implemented(body)
            clauses.append(Clause(m.group("id"), body, kind, kws, implemented, evidence))
        return clauses

    @staticmethod
    def _classify(text: str) -> tuple[str, list[str]]:
        low = text.lower()
        hits = {k: [kw for kw in kws if kw in low] for k, kws in KEYWORDS.items()}
        hits = {k: v for k, v in hits.items() if v}
        if not hits:
            return "MANDATORY_STEP", []
        # prohibitions and escalations dominate thresholds when both appear
        for kind in ("PROHIBITION", "ESCALATION", "THRESHOLD", "DEADLINE", "DEFINITION", "MANDATORY_STEP"):
            if kind in hits:
                return kind, hits[kind]
        return "MANDATORY_STEP", []

    def _implemented(self, text: str) -> tuple[bool, list[str]]:
        """Naive but honest coverage test: does any policy artefact mention the clause's distinctive
        terms? A miss is a candidate gap for a human to confirm, not an automatic change."""
        tokens = [t for t in re.findall(r"[a-z]{5,}", text.lower())
                  if t not in {"shall", "must", "which", "their", "there", "these", "customer",
                               "required", "within", "where", "other", "after", "before"}]
        if not tokens:
            return True, []
        interesting = set(tokens[:12])
        evidence = []
        for f in list(self.policy_dir.rglob("*.rego")) + [self.policy_dir / "data.json"]:
            body = f.read_text().lower()
            matched = sorted(t for t in interesting if t in body)
            if len(matched) >= 2:
                evidence.append(f"{f.relative_to(self.root)} (matched: {', '.join(matched[:4])})")
        return bool(evidence), evidence

    # ── 3. candidate change ──
    def propose(self, doc_path: Path, *, proposal_id: str, change: dict) -> dict:
        doc_path = Path(doc_path).resolve()
        clauses = self.parse(doc_path)
        gaps = [c for c in clauses if not c.implemented]

        kind = change.get("kind", "data_patch")
        candidate = self._materialize(change, proposal_id)
        validation = self._validate(change, candidate)
        impact = self._impact(change) if validation["check_ok"] and validation["tests_ok"] else {
            "skipped": "candidate did not compile or failed tests",
            "cases_evaluated": 0, "changes": [],
        }

        approvals = sorted({a for c in gaps for a in IMPLEMENTATION[c.kind][1]} |
                           {"POLICY_OWNER", "PLATFORM_SECURITY"})
        proposal = {
            "proposal_id": proposal_id,
            "status": "DRAFT",          # cannot be anything else: activation is a human act
            "author_agent": "AXIOM-POLICY",
            "created_at": __import__("axiom.evidence", fromlist=["now"]).now(),
            "source": {"document": str(_rel(doc_path, self.root)),
                       "clauses_parsed": len(clauses),
                       "clauses_without_implementing_rule": [
                           {"clause_id": c.clause_id, "kind": c.kind, "quote": c.text[:400]}
                           for c in gaps],
                       "coverage": round((len(clauses) - len(gaps)) / max(1, len(clauses)) * 100, 1)},
            "change": change,
            "candidate": candidate,
            "validation": validation,
            "impact": impact,
            "required_approvals": approvals,
            "activation": {"permitted_by_agent": False,
                           "reason": "policy.activate is a prohibited action for every agent"},
        }
        out = self.drafts / f"{proposal_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(proposal, indent=2) + "\n")
        proposal["_path"] = str(out)
        return proposal

    def _materialize(self, change: dict, proposal_id: str) -> dict:
        if change["kind"] == "data_patch":
            return {"file": "policy/data.json", "pointer": change["pointer"],
                    "old": change.get("old"), "new": change["new"]}
        if change["kind"] == "rule_patch":
            return {"file": f"proposals/candidates/{proposal_id}.rego",
                    "content": change["rego"]}
        raise ValueError(f"unknown change kind {change['kind']}")

    # ── 4. validate against a sandbox copy of the policy ──
    def _sandbox(self, change: dict) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="policy-sandbox-"))
        shutil.copytree(self.policy_dir, tmp / "policy",
                        ignore=shutil.ignore_patterns("drafts", "__pycache__"))
        if change["kind"] == "data_patch":
            data_file = tmp / "policy" / "data.json"
            data = json.loads(data_file.read_text())
            node = data
            parts = change["pointer"].strip("/").split("/")
            for p in parts[:-1]:
                node = node[p] if isinstance(node, dict) else node[int(p)]
            current = node.get(parts[-1]) if isinstance(node, dict) else node[int(parts[-1])]
            if change.get("old") is not None and current != change["old"]:
                raise OPAError(f"precondition failed: expected {change['old']} at {change['pointer']}, "
                               f"found {current}")
            if isinstance(node, dict):
                node[parts[-1]] = change["new"]
            else:
                node[int(parts[-1])] = change["new"]
            data_file.write_text(json.dumps(data, indent=2) + "\n")
        elif change["kind"] == "rule_patch":
            (tmp / "policy" / "cdd" / "drafts").mkdir(parents=True, exist_ok=True)
            (tmp / "policy" / "cdd" / "drafts" / "candidate.rego").write_text(change["rego"])
        return tmp / "policy"

    def _validate(self, change: dict, candidate: dict) -> dict:
        try:
            sandbox = self._sandbox(change)
        except (OPAError, KeyError) as exc:
            return {"check_ok": False, "tests_ok": False, "fmt_ok": False,
                    "restricted_capabilities": True, "error": str(exc)}
        check_ok, check_msg = self.opa.check(sandbox, restricted=True)
        tests_ok, test_msg = self.opa.test(sandbox) if check_ok else (False, "skipped")
        unformatted = self.opa.fmt_check(sandbox)
        return {
            "check_ok": check_ok,
            "check_detail": check_msg[:800],
            "tests_ok": tests_ok,
            "tests_detail": test_msg.splitlines()[-1] if test_msg else "",
            "fmt_ok": not unformatted,
            "unformatted_files": unformatted,
            "restricted_capabilities": True,
        }

    # ── 5. impact simulation over a decision corpus ──
    def _impact(self, change: dict) -> dict:
        sandbox = self._sandbox(change)
        cases = sorted((self.corpus / "decisions").glob("*.json"))
        changes = []
        for case_file in cases:
            payload = json.loads(case_file.read_text())
            base = self._decide(self.policy_dir, payload)
            cand = self._decide(sandbox, payload)
            if base is None or cand is None:
                changes.append({"case": case_file.stem, "error": "evaluation failed"})
                continue
            if base["outcome"] != cand["outcome"] or base["risk"]["band"] != cand["risk"]["band"]:
                changes.append({
                    "case": case_file.stem,
                    "outcome": f"{base['outcome']} -> {cand['outcome']}",
                    "band": f"{base['risk']['band']} -> {cand['risk']['band']}",
                    "new_reasons": sorted(set(cand["reasons"]) - set(base["reasons"])),
                    "dropped_reasons": sorted(set(base["reasons"]) - set(cand["reasons"])),
                })
        return {
            "cases_evaluated": len(cases),
            "cases_changed": len([c for c in changes if "error" not in c]),
            "changes": changes,
            "materiality": self._materiality(changes),
        }

    @staticmethod
    def _materiality(changes: list[dict]) -> str:
        loosening = [c for c in changes if "->" in c.get("outcome", "")
                     and c["outcome"].split(" -> ")[0] in {"BLOCK", "REQUIRE_EDD", "REQUIRE_RFI"}
                     and c["outcome"].split(" -> ")[1] in {"PERMIT", "PERMIT_WITH_CONDITIONS"}]
        if loosening:
            return "HIGH — the change would permit at least one case that is currently blocked"
        if changes:
            return "MEDIUM — outcomes move but none loosen a control"
        return "LOW — no decision changes across the corpus"

    def _decide(self, policy_path: Path, payload: dict) -> dict | None:
        try:
            r = self.opa.eval("data.agent.decision.result", data_paths=[policy_path],
                              input_obj=payload)
            return r.value
        except OPAError:
            return None
