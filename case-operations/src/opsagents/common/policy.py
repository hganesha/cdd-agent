"""The client for AXIOM-POLICY, the OPA/Rego decision service.

The four agents have no policy of their own: no thresholds, no weights, no decision matrix. This
module is the only way they can reach one, and it deliberately does three things:

* **fails closed** — any transport, parse or undefined-result problem raises `PolicyUnavailable`,
  never a default. An unreachable policy service is an incident, not a reason to guess;
* **caches on content** — the same input document returns the same envelope without re-invoking the
  engine, so a re-run is cheap and bit-identical;
* **exposes the factor vocabulary** — the profiler maps observed facts onto factor ids the pack
  defines, and an id the pack does not define is a hard error rather than a silently dropped factor.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
from functools import lru_cache

from .ledger import canonical, sha256_str


class PolicyUnavailable(RuntimeError):
    """The policy service could not answer. Every caller must stop, not approximate."""


def _find_opa() -> str:
    env = os.environ.get("OPA_BIN")
    if env and pathlib.Path(env).exists():
        return env
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "policy-agent" / "bin" / "opa"
        if candidate.exists():
            return str(candidate)
    found = shutil.which("opa")
    if found:
        return found
    raise PolicyUnavailable("no OPA binary: set OPA_BIN or install opa")


@lru_cache(maxsize=1)
def _policy_dir() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "policy-agent" / "policy"
        if candidate.is_dir():
            return candidate
    raise PolicyUnavailable("cannot locate the policy bundle (policy-agent/policy)")


class PolicyEngine:
    """Thin, strict client over `opa eval`. Not a policy implementation: a transport."""

    DECISION = "data.agent.decision.result"
    DISPOSITION = "data.agent.disposition.result"
    AUTHORITY = "data.agent.authority.result"

    def __init__(self, policy_dir: pathlib.Path | None = None, opa: str | None = None,
                 root: pathlib.Path | None = None):
        # `root` is accepted as the name the tests and the other packages use for "where the bundle
        # lives"; it is the same thing as `policy_dir`, and passing neither means the sibling bundle
        self.policy_dir = pathlib.Path(root or policy_dir) if (root or policy_dir) else _policy_dir()
        if not self.policy_dir.is_dir():
            raise PolicyUnavailable(f"policy bundle not found at {self.policy_dir}")
        self._opa = opa or _find_opa()
        self._cache: dict[str, dict] = {}
        self._pack = json.loads((self.policy_dir / "data.json").read_text())

    # ── metadata ────────────────────────────────────────────────────────────
    def pack(self) -> dict:
        """The policy configuration as the pack publishes it for machines: thresholds, factor
        catalogue, decision matrix, gates. Read-only; the agents never mutate it."""
        return self._cfg

    def document(self) -> dict:
        """The raw `policy/data.json` document, i.e. the bundle's data namespace. Use this when you
        need the exact bytes that were signed, not the configuration the agents reason over."""
        return self._pack

    @property
    def _cfg(self) -> dict:
        # the pack is published as {"config": {...}} so `opa eval -d policy` sees one document
        return self._pack.get("config", self._pack)

    @property
    def policy_version(self) -> str:
        """Alias kept because the pin is quoted by that name in evidence and in the profile."""
        return self.version

    def digest(self) -> str:
        return sha256_str(canonical(self._pack))

    @property
    def version(self) -> str:
        return self._cfg["policy_version"]

    @property
    def thresholds(self) -> dict:
        return dict(self._cfg["thresholds"])

    def factor_catalog(self) -> dict:
        return dict(self._cfg["risk"]["factors"])

    def allowed_recommendations(self, band: str) -> list[str]:
        return list(self._cfg["decision_matrix"][band]["allowed_recommendations"])

    def required_lists(self) -> list[str]:
        return list(self._cfg["screening"]["required_lists"])

    def weights(self) -> dict:
        return dict(self._cfg["risk"]["weights"])

    def bands(self) -> dict:
        return dict(self._cfg["risk"]["bands"])

    def band_for(self, score: float) -> str:
        for name, rng in self._cfg["risk"]["bands"].items():
            if rng["min"] <= score <= rng["max"]:
                return name
        return "VERY_HIGH" if score > 100 else "LOW"

    # ── evaluation ──────────────────────────────────────────────────────────
    def _eval(self, query: str, payload: dict) -> dict:
        key = query + ":" + sha256_str(canonical(payload))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            proc = subprocess.run(
                [self._opa, "eval", "-d", str(self.policy_dir), "--stdin-input", "--format", "json",
                 query],
                input=json.dumps(payload), text=True, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PolicyUnavailable(f"cannot run opa: {exc}") from exc
        if proc.returncode != 0:
            raise PolicyUnavailable(f"opa eval failed for {query}: {proc.stderr.strip()[:400]}")
        try:
            doc = json.loads(proc.stdout)
            value = doc["result"][0]["expressions"][0]["value"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise PolicyUnavailable(f"unreadable opa response for {query}: {proc.stdout[:300]}") from exc
        if not isinstance(value, dict) or not value:
            raise PolicyUnavailable(f"{query} returned no decision for this input")
        self._cache[key] = value
        return value

    def decide(self, policy_input: dict) -> dict:
        return self._eval(self.DECISION, policy_input)

    def dispose(self, *, band: str, recommendation: str, required_gates: list[str],
                critic_status: str) -> dict:
        return self._eval(self.DISPOSITION, {"band": band, "recommendation": recommendation,
                                             "required_gates": required_gates,
                                             "critic_status": critic_status})

    def authority(self, action: str) -> dict:
        return self._eval(self.AUTHORITY, {"action": action})
