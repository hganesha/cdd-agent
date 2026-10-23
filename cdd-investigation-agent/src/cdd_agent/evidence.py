"""Append-only, hash-chained evidence ledger.

Implements spec/04-evidence-writes.md. No UPDATE, no DELETE. Corrections are new records.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

GENESIS = "sha256:" + "0" * 64


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_str(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


def idem_key(*parts: Any) -> str:
    """Idempotency key: same inputs -> same key -> same evidence, never a duplicate."""
    return sha256_str("|".join(str(p) for p in parts))


class IdempotencyStore:
    """Maps idempotency key -> evidence_id so retries reuse evidence instead of duplicating."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._map: dict[str, str] = {}
        if path and path.exists():
            self._map = json.loads(path.read_text())

    def get(self, key: str) -> str | None:
        return self._map.get(key)

    def put(self, key: str, evidence_id: str) -> None:
        self._map[key] = evidence_id
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._map, indent=0, sort_keys=True))


@dataclass
class Finding:
    finding_id: str
    type: str
    severity: str
    title: str
    detail: str
    basis: str
    confidence: str
    evidence_refs: list[str]
    policy_refs: list[str]
    rating_impact: dict | None = None
    status: str = "OPEN"

    def to_dict(self, case_id: str, skill: str, run_id: str) -> dict:
        d = {
            "finding_id": self.finding_id,
            "case_id": case_id,
            "type": self.type,
            "severity": self.severity,
            "title": self.title[:160],
            "detail": self.detail[:2000],
            "basis": self.basis,
            "confidence": self.confidence,
            "evidence_refs": self.evidence_refs,
            "policy_refs": self.policy_refs,
            "status": self.status,
            "created_at": now(),
            "created_by": {"skill": skill, "run_id": run_id},
        }
        if self.rating_impact:
            d["rating_impact"] = self.rating_impact
        return d


class EvidenceLedger:
    """Append-only JSONL ledger with hash chaining and per-record signatures (stub KMS)."""

    def __init__(self, path: Path, case_id: str, run_id: str, idem: IdempotencyStore | None = None):
        self.path = path
        self.case_id = case_id
        self.run_id = run_id
        self.idem = idem or IdempotencyStore()
        self._lock = threading.Lock()
        self._seq = 0
        self._head = GENESIS
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._recover()

    # ── recovery: a resumed run must continue the chain, not restart it ──
    def _recover(self) -> None:
        with self.path.open() as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                self._seq = max(self._seq, rec["seq"])
                self._head = rec["record_hash"]

    # ── append ──
    def append(self, record: dict, *, key: str | None = None, skill: str = "SK-15") -> str:
        """Append one record. If `key` was seen before, return the original evidence_id."""
        with self._lock:
            if key:
                existing = self.idem.get(key)
                if existing:
                    return existing

            self._seq += 1
            rec = dict(record)
            rec.setdefault("case_id", self.case_id)
            rec.setdefault("run_id", self.run_id)
            rec.setdefault("seq", self._seq)
            rec.setdefault("created_at", now())
            rec.setdefault("created_by", {"skill": skill})
            rec.setdefault("quarantined", False)
            rec.setdefault("supersedes", None)
            rec.setdefault("superseded_by", None)
            rec.setdefault("correction_reason", None)
            rec["evidence_id"] = rec.get("evidence_id") or self._make_id(rec)
            rec["prev_hash"] = self._head

            body = {k: v for k, v in rec.items() if k not in ("record_hash", "signature")}
            rec["record_hash"] = sha256_str(canonical(body))
            rec["signature"] = "kms-stub:" + rec["record_hash"][7:23]  # real impl: KMS asymmetric sign

            with self.path.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._head = rec["record_hash"]
            if key:
                self.idem.put(key, rec["evidence_id"])
            return rec["evidence_id"]

    def _make_id(self, rec: dict) -> str:
        digest = hashlib.sha256(canonical({k: rec[k] for k in ("record_type", "subject", "value",
                                                               "locator", "seq") if k in rec}).encode())
        return f"EV-{self.case_id.split('-')[-1]}-{rec['seq']:06d}"

    # ── typed writers (the W1..W9 write classes) ──
    def register(self, subject: dict, value: dict, **kw) -> str:
        return self.append({"record_type": "REGISTRATION", "subject": subject, "value": value,
                            "basis": kw.get("basis", "document"), "confidence": kw.get("confidence", "HIGH")},
                           key=kw.get("key"), skill=kw.get("skill", "SK-01"))

    def observe(self, *, quote: str, locator: dict, method: str, conf: str, key=None, skill="SK-02") -> str:
        return self.append({"record_type": "OBSERVATION", "verbatim_quote": quote, "locator": locator,
                            "extraction_method": method, "basis": "document", "confidence": conf},
                           key=key, skill=skill)

    def extract(self, *, subject: dict, value: dict, quote: str, locator: dict, method: str,
                conf: str, basis: str = "document", key=None, skill="SK-03") -> str:
        return self.append({"record_type": "EXTRACTION", "subject": subject, "value": value,
                            "verbatim_quote": quote, "locator": locator, "source_meta": {},
                            "extraction_method": method, "basis": basis, "confidence": conf},
                           key=key, skill=skill)

    def compute(self, *, subject: dict, calc_id: str, inputs: list[dict], operation: str,
                result: str, basis: str = "computation", key=None, skill="SK-05") -> str:
        # a tool-computed number is HIGH confidence; a computation resting on an assumption is MEDIUM
        conf = "HIGH" if basis == "computation" else "MEDIUM"
        return self.append({"record_type": "COMPUTATION", "subject": subject, "basis": basis,
                            "confidence": conf,
                            "computation": {"calc_id": calc_id, "inputs": inputs,
                                            "operation": operation, "result": result},
                            "value": {"raw": result, "normalized": result, "datatype": "numeric"}},
                           key=key, skill=skill)

    def query(self, *, connector: str, dataset: str, dataset_version: str, params: dict,
              result_count: int, response_ref: str | None = None, hits: list | None = None,
              subject_name: str | None = None, key=None, skill="SK-07") -> str:
        """W5: every external query, INCLUDING zero-hit. No query record => no clearance."""
        return self.append({
            "record_type": "EXTERNAL_QUERY",
            "subject": {"kind": "QUERY_SUBJECT", "name": subject_name, "dataset": dataset},
            "value": {"raw": f"{result_count} hit(s)", "normalized": result_count, "datatype": "count"},
            "basis": "screening" if any(kind in connector for kind in ("sanction", "pep", "media"))
                     else "registry",
            "confidence": "HIGH",
            "query": {"connector": connector, "dataset": dataset, "dataset_version": dataset_version,
                      "queried_at": now(), "parameters_hash": sha256_str(canonical(params)),
                      "response_ref": response_ref or "inline", "result_count": result_count},
            "hits": hits or [],
        }, key=key, skill=skill)

    def finding(self, f: Finding, skill: str) -> str:
        return self.append({"record_type": "FINDING", "subject": {"kind": "FINDING", "id": f.finding_id},
                            "value": {"raw": f.title, "normalized": f.severity, "datatype": "finding"},
                            "basis": f.basis, "confidence": f.confidence,
                            "supports": f.evidence_refs, "finding": f.to_dict(self.case_id, skill, self.run_id)},
                           key=idem_key(self.case_id, "finding", f.finding_id), skill=skill)

    def artifact(self, *, name: str, uri: str, content_hash: str, cites: list[str], key=None,
                 skill="SK-12") -> str:
        return self.append({"record_type": "ARTIFACT",
                            "value": {"raw": name, "normalized": content_hash, "datatype": "artifact"},
                            "basis": "judgment", "confidence": "HIGH",
                            "artifact": {"name": name, "uri": uri, "sha256": content_hash,
                                         "citation_count": len(cites)},
                            "supports": cites}, key=key, skill=skill)

    def gate(self, *, gate: str, event: str, payload: dict, actor: str = "ATLAS-CDD", key=None) -> str:
        return self.append({"record_type": "GATE",
                            "value": {"raw": f"{gate}:{event}", "normalized": gate, "datatype": "gate"},
                            "basis": "human" if actor != "ATLAS-CDD" else "judgment",
                            "confidence": "HIGH", "gate": {"gate": gate, "event": event,
                            "actor": actor, "payload": payload}},
                           key=key, skill="SK-14")

    def correct(self, *, supersedes: str, reason: str, value: dict, quote: str | None = None,
                key=None, skill="SK-15") -> str:
        aid = self.append({"record_type": "CORRECTION", "supersedes": supersedes,
                           "correction_reason": reason, "value": value, "verbatim_quote": quote,
                           "basis": "document", "confidence": "HIGH"}, key=key, skill=skill)
        return aid

    # ── integrity ──
    def verify_chain(self) -> tuple[bool, str, int | None]:
        prev, n = GENESIS, 0
        if not self.path.exists():
            return True, GENESIS, None
        with self.path.open() as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                n += 1
                if rec.get("prev_hash") != prev:
                    return False, prev, rec.get("seq")
                body = {k: v for k, v in rec.items() if k not in ("record_hash", "signature")}
                if sha256_str(canonical(body)) != rec.get("record_hash"):
                    return False, prev, rec.get("seq")
                prev = rec["record_hash"]
        return True, prev, None

    @property
    def head(self) -> str:
        return self._head

    def records(self) -> Iterable[dict]:
        if not self.path.exists():
            return
        with self.path.open() as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)

    def by_finding(self, finding_id: str) -> list[dict]:
        return [r for r in self.records()
                if finding_id in (r.get("supports") or [])
                or r.get("finding", {}).get("finding_id") == finding_id]

    def evidence_ids(self) -> list[str]:
        return [r["evidence_id"] for r in self.records()]
