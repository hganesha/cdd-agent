"""Checkpointing for the OPS agents.

Every agent in this package can be killed and restarted, because every agent in this package runs
long enough that "start again from zero" is not an option: a profile is built from hundreds of
paginated transaction pages, a verification scans every artefact in a bundle, and an RFI sits waiting
for a customer for ten business days.

The unit of resumability is a **stage**. A stage is a named step with a payload: the API body it
fetched, the check result it computed, the policy envelope it received. Stage results are written to
a JSON file under `out/state/` after each stage, atomically (`tmp` file + `os.replace`), so a kill
mid-write leaves the previous checkpoint intact rather than a truncated file.

Three rules make resume safe:

1. **A completed stage is never re-executed.** Its payload is replayed from the checkpoint. That is
   what makes resume cheap *and* deterministic: the resumed run sees the same API bodies, so it
   derives the same profile id and the same evidence.
2. **A completed stage is never re-charged.** Counters (api calls, pages, checks) travel with the
   checkpoint, which is how the budget survives a restart.
3. **A checkpoint names its head evidence id.** On resume the ledger must still contain that record,
   otherwise the checkpoint belongs to a different (rebuilt, archived) ledger and is discarded rather
   than silently mixed with a new chain.

The checkpoint is deliberately *not* the evidence ledger. The ledger is append-only, hash-chained and
signed; the checkpoint is mutable working state. Evidence is written once per stage on the run that
actually did the work, so a resumed run adds no duplicate records — the idempotency store makes that
true even if a stage is retried.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import uuid

from .ledger import now


class BudgetExceeded(RuntimeError):
    """The run hit a hard budget (API calls, pages, wall clock). The run stops at a checkpoint and
    reports PARTIAL, which is a state the caller can resume from — not an error to swallow."""


class RunState:
    """A resumable run. One file per (case, agent)."""

    VERSION = 1

    def __init__(self, path: pathlib.Path, *, agent_id: str, case_id: str,
                 budget: dict | None = None):
        self.path = pathlib.Path(path)
        self.data = {
            "version": self.VERSION,
            "run_id": "RUN-" + uuid.uuid4().hex[:8].upper(),
            "agent_id": agent_id,
            "case_id": case_id,
            "created_at": now(),
            "updated_at": now(),
            "stages": {},
            "counters": {"api_calls": 0, "pages": 0, "checks": 0},
            "budget": dict(budget or {}),
            "head_record_hash": None,
            "status": "RUNNING",
        }

    # ── lifecycle ───────────────────────────────────────────────────────────
    @classmethod
    def open(cls, path: pathlib.Path, *, agent_id: str, case_id: str,
             budget: dict | None = None, resume: bool = True) -> "RunState":
        """Load an existing checkpoint if there is one for this (agent, case); otherwise start one."""
        path = pathlib.Path(path)
        if resume and path.exists():
            try:
                loaded = cls(path, agent_id=agent_id, case_id=case_id)
                loaded.data = json.loads(path.read_text())
                if (loaded.data.get("agent_id") != agent_id
                        or loaded.data.get("case_id") != case_id):
                    raise ValueError("checkpoint belongs to another agent or case")
                if budget:
                    loaded.data["budget"] = {**loaded.data.get("budget", {}), **budget}
                loaded.data["status"] = "RUNNING"
                loaded.resumed = bool(loaded.data.get("stages"))
                return loaded
            except (json.JSONDecodeError, ValueError):
                pass        # an unreadable checkpoint is not a checkpoint: start clean
        st = cls(path, agent_id=agent_id, case_id=case_id, budget=budget)
        st.save()
        return st

    def save(self) -> None:
        self.data["updated_at"] = now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, self.path)          # atomic: a kill leaves either the old file or the new one

    def attach(self, head_record_hash: str | None) -> None:
        """Record which ledger head this checkpoint was written against."""
        self.data["head_record_hash"] = head_record_hash
        self.save()

    def stale_for(self, head_record_hash: str | None) -> bool:
        """True when the checkpoint was written against a ledger head that is no longer in the chain."""
        pinned = self.data.get("head_record_hash")
        return bool(pinned) and bool(head_record_hash) and pinned != head_record_hash

    # ── stages ──────────────────────────────────────────────────────────────
    def done(self, stage: str) -> bool:
        return stage in self.data["stages"]

    def payload(self, stage: str):
        return self.data["stages"][stage].get("payload")

    def record(self, stage: str, payload=None, *, agent: str | None = None,
               evidence_id: str | None = None) -> None:
        self.data["stages"][stage] = {
            "completed_at": now(),
            "agent": agent,
            "evidence_id": evidence_id,
            "payload": payload,
        }
        self.save()

    def stage_names(self) -> list[str]:
        return list(self.data["stages"])

    # ── counters and budget ─────────────────────────────────────────────────
    def charge(self, *, api_calls: int = 0, pages: int = 0, checks: int = 0) -> None:
        c = self.data["counters"]
        c["api_calls"] += api_calls
        c["pages"] += pages
        c["checks"] += checks

    def spent(self) -> dict:
        return dict(self.data["counters"])

    def budget(self, key: str):
        return self.data.get("budget", {}).get(key)

    def remaining(self, key: str):
        limit = self.budget(key)
        counter = {"api_calls": "api_calls", "pages": "pages", "checks": "checks"}.get(key, key)
        if limit is None:
            return None
        return max(0, int(limit) - int(self.data["counters"].get(counter, 0)))

    def check_budget(self, key: str, *, what: str = "") -> None:
        """Raise before doing the work, never after: a budget that only reports is a budget that
        cannot stop a run."""
        left = self.remaining(key)
        if left is not None and left <= 0:
            self.data["status"] = "BUDGET_EXHAUSTED"
            self.save()
            raise BudgetExceeded(f"{key} budget exhausted" + (f" before {what}" if what else ""))

    # ── terminal states ─────────────────────────────────────────────────────
    def finish(self, status: str = "COMPLETE") -> None:
        self.data["status"] = status
        self.save()

    def summary(self) -> dict:
        return {"run_id": self.data["run_id"], "agent_id": self.data["agent_id"],
                "case_id": self.data["case_id"], "status": self.data["status"],
                "stages": len(self.data["stages"]), "counters": self.spent(),
                "budget": self.data.get("budget", {}), "resumed_from_checkpoint": self.resumed,
                "updated_at": self.data["updated_at"]}

    resumed = False


class KillSwitch:
    """Simulates a crash at a deterministic point, so resume is testable rather than aspirational.

    `--kill-at 12` means: the twelfth API call of the run never returns. The process exits with
    status 70 (EX_SOFTWARE) *after* the last completed checkpoint has been flushed, which is exactly
    what a container OOM does.
    """

    def __init__(self, after_calls: int | None = None):
        self.after_calls = after_calls
        self.calls = 0

    def tick(self) -> None:
        if self.after_calls is None:
            return
        self.calls += 1
        if self.calls > self.after_calls:
            raise SystemExit(70)


def default_budget() -> dict:
    return {"api_calls": 400, "pages": 600, "checks": 200, "wall_clock_s": 900}


def wall_clock_exceeded(started: float, budget: dict) -> bool:
    limit = budget.get("wall_clock_s")
    return bool(limit) and (time.monotonic() - started) > limit
