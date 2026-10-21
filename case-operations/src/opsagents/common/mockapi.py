"""Mock internal API layer.

In production these are six REST/GraphQL services (customer master, transaction warehouse,
counterparty registry, internal rating, sanctions/list reference, document store). Here they are
file-backed, but they behave like services rather than like fixtures:

* every call has a **queryable evidence record** (endpoint, request hash, response hash, status,
  latency, attempt) — the profile agent's numbers are only as good as the calls behind them;
* latency is deterministic per request, so a run is reproducible and an SLO can be asserted;
* two endpoints are **deliberately unreliable** (one flaky 503, one rate-limited) so the retry and
  back-off paths are exercised by the test suite rather than discovered in production;
* responses are paginated where the real service paginates.

Swap `MockApi` for a real client with the same three methods (`get`, `paginate`, `evidence`) and
nothing above this layer changes.
"""
from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass, field

from .ledger import OpsLedger, canonical, sha256_str


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 500, endpoint: str = ""):
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint


@dataclass
class ApiResponse:
    status: int
    body: object
    latency_ms: float
    attempts: int
    evidence_id: str
    endpoint: str
    request: dict = field(default_factory=dict)
    pages_total: int = 1

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


# Endpoints whose behaviour is deliberately imperfect, and how.
FLAKY = {
    "/tx/summary": {"fail_attempts": 1, "status": 503, "label": "transaction warehouse is flaky"},
    "/counterparties/counterparties": {"rate_limit_after": 2, "status": 429, "retry_after_s": 1,
                                       "label": "counterparty registry rate-limits after 2 calls"},
}


class MockApi:
    MAX_ATTEMPTS = 3

    def __init__(self, fixtures: pathlib.Path, ledger: OpsLedger, *, sleep: bool = True,
                 clock=lambda: time.monotonic()):
        self.root = pathlib.Path(fixtures)
        self.ledger = ledger
        self.sleep = sleep
        self.clock = clock
        self._index = self._load()
        self._calls: dict[str, int] = {}
        self.call_log: list[dict] = []

    def _load(self) -> dict[str, dict[str, dict]]:
        """/<family>/<resource> -> document, mirroring the URL shape of the real services."""
        index: dict[str, dict[str, dict]] = {}
        for f in self.root.rglob("*.json"):
            index.setdefault(f.parent.name, {})[f.stem] = json.loads(f.read_text())
        return index

    # ── behaviour ───────────────────────────────────────────────────────────
    def _behaviour(self, endpoint: str) -> dict:
        for prefix, spec in FLAKY.items():
            if endpoint.startswith(prefix):
                return {**spec, "prefix": prefix}
        return {}

    def _latency(self, endpoint: str, request: dict) -> float:
        # deterministic pseudo-latency in [4, 40] ms, stable for the same request
        h = int(sha256_str(endpoint + canonical(request))[7:15], 16)
        return 4.0 + (h % 360) / 10.0

    def _pick(self, endpoint: str, key: str | None = None):
        parts = [p for p in endpoint.strip("/").split("/") if p]
        family = parts[0]
        resource = parts[1] if len(parts) > 1 else None
        doc = self._index.get(family, {}).get(resource)
        if doc is None:
            raise ApiError(f"no such endpoint: {endpoint}", status=404, endpoint=endpoint)
        records = doc.get("records")
        if records is None:
            return doc
        if key is None:
            return records
        if isinstance(records, dict):
            if key not in records:
                raise ApiError(f"{endpoint}: {key} not found", status=404, endpoint=endpoint)
            return records[key]
        return records

    def get(self, endpoint: str, *, key: str | None = None, query: dict | None = None) -> ApiResponse:
        query = query or {}
        behaviour = self._behaviour(endpoint)
        self._calls[endpoint] = self._calls.get(endpoint, 0) + 1
        attempt, last_status, last_err = 0, 0, ""
        started = self.clock()
        while attempt < self.MAX_ATTEMPTS:
            attempt += 1
            last_status = 200
            # flaky endpoint: the first attempt fails, deterministically
            if behaviour.get("fail_attempts", 0) >= attempt:
                last_status = behaviour["status"]
                last_err = f"{behaviour.get('label', 'upstream error')} (attempt {attempt})"
            # rate-limited endpoint: once the call budget is exceeded, 429 with Retry-After
            elif behaviour.get("rate_limit_after") and \
                    self._calls[endpoint] > behaviour["rate_limit_after"] and attempt == 1:
                last_status = behaviour["status"]
                last_err = f"retry after {behaviour.get('retry_after_s', 1)}s"

            if last_status == 200:
                break
            if self.sleep:
                time.sleep(0.001 * attempt)          # back-off, mocked to stay fast

        latency = (self.clock() - started) * 1000 + self._latency(endpoint, {"key": key, **query})
        if last_status != 200:
            raise ApiError(f"{endpoint}: {last_err}", status=last_status, endpoint=endpoint)

        body = self._pick(endpoint, key)
        ev = self.ledger.api_call(endpoint=endpoint, request={"key": key, **query},
                                  response_hash=sha256_str(canonical(body)),
                                  status=200, latency_ms=latency, attempt=attempt)
        rec = {"endpoint": endpoint, "key": key, "status": 200, "latency_ms": round(latency, 2),
               "attempts": attempt, "evidence_id": ev}
        self.call_log.append(rec)
        return ApiResponse(status=200, body=body, latency_ms=latency, attempts=attempt,
                           evidence_id=ev, endpoint=endpoint, request={"key": key, **query})

    def page(self, endpoint: str, *, key: str, page: int, page_field: str = "pages") -> ApiResponse:
        """One page of a paginated endpoint, as its own call with its own evidence record.

        Page-level granularity is what makes a 400-page read resumable: the profile checkpoints
        per page, so a kill costs one page rather than the whole series.
        """
        doc = self._pick(endpoint, key)
        pages = doc.get(page_field) or []
        if not 1 <= page <= len(pages):
            raise ApiError(f"{endpoint}: page {page} out of range (1..{len(pages)})",
                           status=404, endpoint=endpoint)
        body = pages[page - 1]
        latency = self._latency(endpoint, {"key": key, "page": page})
        ev = self.ledger.api_call(endpoint=f"{endpoint}?page={page}",
                                  request={"key": key, "page": page},
                                  response_hash=sha256_str(canonical(body)),
                                  status=200, latency_ms=latency, attempt=1)
        self.call_log.append({"endpoint": endpoint, "key": key, "status": 200,
                              "latency_ms": round(latency, 2), "attempts": 1,
                              "evidence_id": ev, "page": page})
        return ApiResponse(status=200, body=body, latency_ms=latency, attempts=1,
                           evidence_id=ev, endpoint=endpoint,
                           request={"key": key, "page": page}, pages_total=len(pages))

    def paginate(self, endpoint: str, *, key: str, page_field: str = "pages") -> list[dict]:
        """Walk a paginated endpoint. Every page is its own API call with its own evidence record,
        so a partial read is visible as a missing page rather than as a smaller number."""
        first = self.get(endpoint, key=key)
        pages = list((first.body or {}).get(page_field, []))
        for page in pages:
            if page.get("page", 1) > 1:
                self.ledger.api_call(endpoint=f"{endpoint}?page={page['page']}",
                                     request={"key": key, "page": page["page"]},
                                     response_hash=sha256_str(canonical(page)),
                                     status=200, latency_ms=self._latency(endpoint, page),
                                     attempt=1)
        return pages

    # ── totals for the profile's data-quality block ─────────────────────────
    def evidence(self) -> dict:
        if not self.call_log:
            # No live calls in this process: a resumed run replays its checkpoint rather than
            # refetching, so latency is zero-by-absence rather than unknown.
            return {"calls": 0, "p95_latency_ms": 0.0, "retried": 0, "endpoints": [],
                    "evidence_ids": [], "replayed_from_checkpoint": True}
        lat = sorted(c["latency_ms"] for c in self.call_log)
        p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
        return {"calls": len(self.call_log), "p95_latency_ms": round(p95, 2),
                "retried": sum(1 for c in self.call_log if c["attempts"] > 1),
                "endpoints": sorted({c["endpoint"] for c in self.call_log}),
                "evidence_ids": [c["evidence_id"] for c in self.call_log]}
