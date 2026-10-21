"""Append-only, hash-chained decision evidence ledger.

Same discipline as the CDD agent's evidence model: no UPDATE, no DELETE, corrections are new
records, and every policy decision is reconstructible offline from (input hash, bundle revision,
policy version, OPA version, decision_id).
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "sha256:" + "0" * 64


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_str(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()


def idem_key(*parts) -> str:
    return sha256_str("|".join(str(p) for p in parts))


class IdempotencyStore:
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


class DecisionLedger:
    """W1 DECISION · W2 AUTHORITY · W3 BUNDLE · W4 PROPOSAL · W5 TEST_RUN · W6 STATE · W7 CORRECTION"""

    def __init__(self, path: Path, *, agent_id: str, idem: IdempotencyStore | None = None):
        self.path = Path(path)
        self.agent_id = agent_id
        self.idem = idem or IdempotencyStore()
        self._lock = threading.Lock()
        self._seq, self._head = 0, GENESIS
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            for line in self.path.open():
                if line.strip():
                    rec = json.loads(line)
                    self._seq = max(self._seq, rec["seq"])
                    self._head = rec["record_hash"]

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
            rec["evidence_id"] = rec.get("evidence_id") or f"PZ-{self._seq:06d}"
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

    # ── write classes ──
    def decision(self, *, decision: dict, input_obj: dict, bundle: dict, elapsed_ms: float) -> str:
        return self.append({
            "record_type": "DECISION",
            "decision_id": decision["decision_id"],
            "input_hash": sha256_str(canonical(input_obj)),
            "bundle": {"revision": bundle["revision"], "sha256": bundle["sha256"],
                       "policy_version": bundle["policy_version"]},
            "opa_version": bundle["opa_version"],
            "engine": "data.agent.decision.result",
            "outcome": decision["outcome"],
            "reasons": decision["reasons"],
            "obligations": [o["kind"] for o in decision["obligations"]],
            "route": decision["gates"]["route_to"],
            "gates": decision["gates"]["required_gates"],
            "risk": {k: decision["risk"][k] for k in ("score", "band", "cdd_level")},
            "elapsed_ms": round(elapsed_ms, 2),
            # the payload is NOT stored: it carries personal data. Its digest binds the record to
            # the case file, which is retained separately under SOP 8.1.
            "input_fields": sorted(input_obj),
        }, key=idem_key("decision", decision["decision_id"], sha256_str(canonical(input_obj))))

    def authority(self, *, action: str, result: dict, actor: str) -> str:
        return self.append({"record_type": "AUTHORITY", "action": action, "actor": actor,
                            "decision": result["decision"], "reason": result["reason"],
                            "approval": result.get("approval")},
                           key=idem_key("authority", actor, action, result["decision"]))

    def bundle_event(self, *, event: str, manifest: dict) -> str:
        return self.append({"record_type": "BUNDLE", "event": event, "manifest": manifest},
                           key=idem_key("bundle", event, manifest["revision"]))

    def proposal(self, *, proposal: dict) -> str:
        return self.append({"record_type": "PROPOSAL", "proposal": proposal},
                           key=idem_key("proposal", proposal["proposal_id"]))

    def test_run(self, *, scope: str, passed: int, failed: int, detail: str = "") -> str:
        return self.append({"record_type": "TEST_RUN", "scope": scope, "passed": passed,
                            "failed": failed, "detail": detail[:2000]})

    def state(self, *, event: str, detail: dict) -> str:
        return self.append({"record_type": "STATE", "event": event, "detail": detail})

    # ── integrity ──
    def verify_chain(self) -> tuple[bool, str, int | None]:
        prev, n = GENESIS, 0
        if not self.path.exists():
            return True, GENESIS, None
        for line in self.path.open():
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

    def records(self):
        if not self.path.exists():
            return []
        return (json.loads(l) for l in self.path.open() if l.strip())

    def by_decision(self, decision_id: str) -> list[dict]:
        return [r for r in self.records() if r.get("decision_id") == decision_id]
