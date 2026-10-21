"""Append-only, hash-chained evidence ledger for external research.

Write classes:
  W1 QUERY      every query issued, with facet and rationale (a missing query is a search gap)
  W2 RETRIEVAL  every fetch attempt, including refusals and paywalls, with status and content hash
  W3 QUARANTINE source excluded for instruction-like content, with the pattern that fired
  W4 CLAIM      a claim with its supporting and contradicting citations
  W5 PACK       the delivered evidence pack, hash-bound
  W6 STATE      budget/stall/checkpoint events
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
        self._map = json.loads(path.read_text()) if (path and path.exists()) else {}

    def get(self, key: str):
        return self._map.get(key)

    def put(self, key: str, value: str) -> None:
        self._map[key] = value
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._map, indent=0, sort_keys=True))


class EvidenceLedger:
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
            rec["evidence_id"] = rec.get("evidence_id") or f"WS-{self._seq:06d}"
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
    def query(self, *, request_id: str, query: dict) -> str:
        return self.append({"record_type": "QUERY", "request_id": request_id, "query": query},
                           key=idem_key("query", request_id, query["query_id"]))

    def retrieval(self, *, request_id: str, url: str, status: str, content_hash: str = "",
                  tier: int | None = None, reason: str | None = None) -> str:
        return self.append({"record_type": "RETRIEVAL", "request_id": request_id, "url": url,
                            "status": status, "content_hash": content_hash, "tier": tier,
                            "reason": reason},
                           key=idem_key("retrieval", request_id, url, status))

    def quarantine(self, *, source: dict) -> str:
        return self.append({"record_type": "QUARANTINE", "url": source["url"],
                            "patterns": source["patterns"],
                            "content_hash": source.get("content_hash", ""),
                            "reason": source["reason"]},
                           key=idem_key("quarantine", source["url"]))

    def refusal(self, *, source: dict) -> str:
        return self.append({"record_type": "RETRIEVAL", "url": source["url"],
                            "status": source["status"], "reason": source.get("reason"),
                            "refused": True},
                           key=idem_key("refusal", source["url"], source["status"]))

    def claim(self, *, claim: dict, request_id: str) -> str:
        return self.append({"record_type": "CLAIM", "request_id": request_id,
                            "claim_key": claim["claim_key"],
                            "claim": {"text": claim["claim"], "confidence": claim["confidence"],
                                      "corroboration_count": claim["corroboration_count"]},
                            "confidence": claim["confidence"],
                            "corroboration_count": claim["corroboration_count"],
                            "citations": [{"evidence_id": c["evidence_id"], "url": c["url"],
                                           "quote": c["quote"], "char_span": c["char_span"],
                                           "content_hash": c["content_hash"]}
                                          for c in claim["citations"]],
                            "contradictions": claim["contradictions"]},
                           key=idem_key("claim", request_id, claim["claim_key"],
                                        canonical(claim["citations"])))

    def pack(self, *, pack: dict) -> str:
        digest = sha256_str(canonical({k: v for k, v in pack.items() if k != "pack_id"}))
        return self.append({"record_type": "PACK", "pack_id": pack.get("pack_id"),
                            "request_id": pack["request_id"], "status": pack["status"],
                            "pack_hash": digest,
                            "claim_count": len(pack["claims"]),
                            "quarantined": len(pack["quarantined_sources"]),
                            "refused": len(pack["refused_sources"]),
                            "gaps": len(pack["gaps"]), "cost": pack["cost"]},
                           key=idem_key("pack", pack["request_id"], digest))

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
