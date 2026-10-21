"""Append-only, hash-chained evidence ledger for the OPS agents.

Same construction as the CDD, search and policy ledgers — one evidence model across the stack, so an
auditor learns it once and one verifier tool can fold any of them:

    record_hash = "sha256:" + sha256(canonical(record - {record_hash, signature}))
    prev_hash   = previous record's record_hash        (GENESIS for the first record)
    evidence_id = "OX-" + seq:06d                      (monotonic, never reused)

Nothing here rewrites a line. There is no API to edit a record, and `evidence.rewrite` is a prohibited
action in the policy's authority projection — the only way to remove evidence is to destroy the file,
which breaks the chain and fails V-001 loudly.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import threading
from datetime import datetime, timezone

GENESIS = "sha256:" + "0" * 64


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_str(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()


def idem_key(*parts) -> str:
    return sha256_str("\u0001".join(str(p) for p in parts))


class IdempotencyStore:
    """key -> evidence_id. Makes a retried write a no-op instead of a duplicate record."""

    def __init__(self, path: pathlib.Path | None = None):
        self.path = pathlib.Path(path) if path else None
        self._map: dict[str, str] = {}
        if self.path and self.path.exists():
            try:
                self._map = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self._map = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        return self._map.get(key)

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._map[key] = value
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(self._map, indent=1, sort_keys=True))
                tmp.replace(self.path)


class OpsLedger:
    def __init__(self, path: pathlib.Path, *, agent_id: str = "OPS",
                 idem: IdempotencyStore | None = None):
        self.path = pathlib.Path(path)
        self.agent_id = agent_id
        self.idem = idem or IdempotencyStore()
        self._lock = threading.Lock()
        self._seq, self._head = 0, GENESIS
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._seq = max(self._seq, rec["seq"])
                    self._head = rec["record_hash"]

    # ── primitive ───────────────────────────────────────────────────────────
    def append(self, record: dict, *, key: str | None = None) -> str:
        with self._lock:
            if key:
                existing = self.idem.get(key)
                if existing:
                    return existing
            self._seq += 1
            rec = dict(record)
            rec.setdefault("agent_id", self.agent_id)
            rec.setdefault("seq", self._seq)
            rec.setdefault("created_at", now())
            rec["evidence_id"] = rec.get("evidence_id") or f"OX-{self._seq:06d}"
            rec["prev_hash"] = self._head
            body = {k: v for k, v in rec.items() if k not in ("record_hash", "signature")}
            rec["record_hash"] = sha256_str(canonical(body))
            rec["signature"] = "kms-stub:" + rec["record_hash"][7:23]
            with self.path.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._head = rec["record_hash"]
            if key:
                self.idem.put(key, rec["evidence_id"])
            return rec["evidence_id"]

    # ── write classes ───────────────────────────────────────────────────────
    def api_call(self, *, endpoint: str, request: dict, response_hash: str, status: int,
                 latency_ms: float, attempt: int = 1) -> str:
        return self.append(
            {"record_type": "API_CALL", "endpoint": endpoint,
             "request_hash": sha256_str(canonical(request)),
             "response_hash": response_hash, "status": status,
             "latency_ms": round(latency_ms, 2), "attempt": attempt},
            # the key excludes `attempt`: a retry that succeeds writes the same evidence as a
            # first-attempt success, so re-running an agent cannot double-charge the ledger
            key=idem_key("api", endpoint, canonical(request), status))

    def profile(self, *, profile: dict) -> str:
        return self.append(
            {"record_type": "PROFILE", "profile_id": profile["profile_id"],
             "case_id": profile["case_id"], "customer_id": profile["customer_id"],
             "as_of": profile["as_of"], "factors": profile["factors"],
             "coverage_pct": profile["coverage"]["coverage_pct"],
             "screening_complete": profile["screening"]["screening_complete"],
             "profile_hash": sha256_str(canonical({k: v for k, v in profile.items()
                                                   if k not in ("profile_id", "evidence_id")}))},
            key=idem_key("profile", profile["profile_id"]))

    def profile_delta(self, *, customer_id: str, previous_id: str | None, delta: list[dict]) -> str:
        return self.append(
            {"record_type": "PROFILE_DELTA", "customer_id": customer_id,
             "previous_profile_id": previous_id, "changes": delta},
            key=idem_key("delta", customer_id, previous_id, canonical(delta)))

    def verification(self, *, report: dict) -> str:
        return self.append(
            {"record_type": "VERIFICATION", "verification_id": report["verification_id"],
             "case_id": report.get("case_id"), "stage": report.get("stage"),
             "subject_agent": report["subject_agent"],
             "critic_status": report["critic_status"],
             "checks": [{"check_id": c["check_id"], "verdict": c["verdict"],
                         "severity": c["severity"]} for c in report["checks"]],
             "blocking": report.get("blocking_check_ids", [])},
            key=idem_key("verification", report["verification_id"], report["critic_status"]))

    def rfi(self, *, event: str, rfi: dict) -> str:
        return self.append(
            {"record_type": "RFI", "event": event, "rfi_id": rfi.get("rfi_id"),
             "case_id": rfi.get("case_id"), "round": rfi.get("round"),
             "state": rfi.get("state"), "message_hash": rfi.get("message_hash"),
             "approval": rfi.get("approval")},
            key=idem_key("rfi", rfi.get("rfi_id"), event, rfi.get("state"), rfi.get("round")))

    def disposition(self, *, disposition: dict) -> str:
        return self.append(
            {"record_type": "DISPOSITION",
             "disposition_id": disposition["disposition_id"],
             "case_id": disposition["case_id"],
             "recommendation": disposition.get("recommendation"),
             "policy_outcome": (disposition.get("policy") or {}).get("outcome"),
             "policy_decision_id": (disposition.get("policy") or {}).get("decision_id"),
             "admissible": (disposition.get("policy_admissibility") or {}).get("admissible"),
             "human_approval_required": (disposition.get("approval") or {}).get("required"),
             "state": disposition.get("state"), "finalised": False,
             "content_hash": sha256_str(canonical({k: v for k, v in disposition.items()
                                                   if k not in ("disposition_id", "evidence_id")}))},
            key=idem_key("disposition", disposition["disposition_id"]))

    def event(self, *, event: str, detail: dict) -> str:
        return self.append({"record_type": "EVENT", "event": event, "detail": detail})

    # ── verification ────────────────────────────────────────────────────────
    def verify_chain(self) -> tuple[bool, str, int | None]:
        if not self.path.exists():
            return True, GENESIS, None
        prev, seq = GENESIS, 0
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return False, prev, seq + 1
            seq += 1
            if rec.get("seq") != seq:
                return False, prev, seq
            if rec.get("prev_hash") != prev:
                return False, prev, seq
            body = {k: v for k, v in rec.items() if k not in ("record_hash", "signature")}
            if rec.get("record_hash") != sha256_str(canonical(body)):
                return False, prev, seq
            if rec.get("signature") != "kms-stub:" + str(rec.get("record_hash"))[7:23]:
                return False, prev, seq
            prev = rec["record_hash"]
        return True, prev, None

    def head(self) -> str:
        return self._head

    def records(self):
        if not self.path.exists():
            return iter(())
        return (json.loads(line) for line in self.path.read_text().splitlines() if line.strip())
