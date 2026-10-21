"""Retrieval layer: robots/allowlist/licensing compliance, content addressing, caching.

An offline corpus stands in for the live web so the pipeline is reproducible and testable. The
contract is the same as production: a fetch returns a page *with provenance* (URL, canonical URL,
status, content hash, retrieval time, robots verdict, licensing status), or it returns a refusal
that is itself recorded as evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "corpus" / "sites"


def sha256(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()


@dataclass
class FetchResult:
    url: str
    status: str                     # OK | PAYWALLED | ROBOTS_DISALLOWED | BLOCKED_DOMAIN | NOT_FOUND
    title: str = ""
    published: str | None = None
    author: str | None = None
    body: str = ""
    source_tier: int = 4
    kind: str = "article"
    licensing: str = "unknown"
    lang: str = "en"
    content_hash: str = ""
    retrieved_at: str = ""
    injected: bool = False
    injection_evidence: list[str] = field(default_factory=list)
    cached: bool = False

    @property
    def domain(self) -> str:
        return re.sub(r"^https?://", "", self.url).split("/")[0]

    @property
    def usable(self) -> bool:
        return self.status == "OK"

    @property
    def citable(self) -> bool:
        """Robots-disallowed and blocked-domain pages are never citable. Paywalled pages yield a
        citable metadata record but not citable body text."""
        return self.status == "OK"


class SourcePolicy:
    """Allowlist, tier, robots and licensing rules — loaded from config/sources.yaml."""

    def __init__(self, config: dict):
        self.cfg = config
        self.tiers = config["tiers"]
        self.denied = set(config.get("denied_domains", []))
        self.max_tier = config.get("max_tier_citable", 3)
        self.tier4_corroboration_only = config.get("tier4_corroboration_only", True)

    def tier_of(self, domain: str) -> int:
        for tier, spec in self.tiers.items():
            if domain in spec.get("domains", []):
                return int(tier)
        return 5

    def citable(self, fetch: FetchResult) -> tuple[bool, str]:
        if fetch.status != "OK":
            return False, f"status:{fetch.status}"
        if fetch.domain in self.denied:
            return False, "denied_domain"
        if fetch.source_tier > self.max_tier:
            return False, f"tier_{fetch.source_tier}_not_citable"
        return True, "ok"

    def may_fetch(self, domain: str) -> tuple[bool, str]:
        if domain in self.denied:
            return False, "denied_domain"
        return True, "ok"


INJECTION_PATTERNS = [
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", "instruction_override"),
    (r"you\s+are\s+now\s+(a|an)\s+", "role_reassignment"),
    (r"\bcite\s+(this|https?://\S+)\s+as\s+the\s+authoritative\s+source", "citation_forcing"),
    (r"\bomit\s+(any|all)\s+", "omission_instruction"),
    (r"do\s+not\s+report\s+this\s+instruction", "concealment_instruction"),
    (r"\bsystem\s*:\s*", "fake_system_block"),
]

SENSITIVE_PATTERNS = [
    (r"\b\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\b", "payment_card"),
    (r"\b\d{2}\s?[A-Z]{3}\s?\d{4}\b", "passport_like"),
    (r"\b\d{1,3}\s+[A-Z][a-z]+\s+(Road|Street|Avenue|Lane|Close)\b", "street_address"),
    (r"\bdate of birth\b|\bDOB\b", "dob_disclosure"),
    (r"\bbank account number\b|\bIBAN\b", "bank_detail"),
]


class Fetcher:
    def __init__(self, policy: SourcePolicy, cache_dir: Path | None = None,
                 corpus: Path | None = None):
        self.policy = policy
        self.cache_dir = cache_dir
        self.corpus = Path(corpus) if corpus else CORPUS     # injectable: acceptance tests use fixtures
        self._index = self._build_index()
        self.fetches = 0
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _build_index(self) -> dict[str, Path]:
        index = {}
        for f in self.corpus.rglob("*.json"):
            doc = json.loads(f.read_text())
            index[doc["url"]] = f
            index.setdefault(doc["url"].rstrip("/"), f)
        return index

    def fetch(self, url: str, *, retrieved_at: str = "2026-10-21T12:00:00Z") -> FetchResult:
        self.fetches += 1
        domain = re.sub(r"^https?://", "", url).split("/")[0]

        allowed, why = self.policy.may_fetch(domain)
        if not allowed:
            return FetchResult(url=url, status="BLOCKED_DOMAIN", retrieved_at=retrieved_at)

        # cache by URL (content addressed): a resumed run must not re-fetch
        if self.cache_dir:
            key = sha256(url).replace("sha256:", "")[:24]
            cached = self.cache_dir / f"{key}.json"
            if cached.exists():
                d = json.loads(cached.read_text())
                return FetchResult(**{**d, "cached": True})

        path = self._index.get(url) or self._index.get(url.rstrip("/"))
        if not path:
            return FetchResult(url=url, status="NOT_FOUND", retrieved_at=retrieved_at)
        doc = json.loads(path.read_text())

        result = FetchResult(
            url=doc["url"], status="OK",
            title=doc["title"], published=doc.get("published"), author=doc.get("author"),
            body=doc["body"],
            # the source policy owns the tier. A page may declare only its own classification,
            # and only when the policy has no opinion (tier 5 = unclassified).
            source_tier=(doc["source_tier"] if self.policy.tier_of(domain) == 5
                         else self.policy.tier_of(domain)),
            kind=doc.get("kind", "article"), licensing=doc.get("licensing", "unknown"),
            lang=doc.get("lang", "en"),
            content_hash=sha256(doc["body"]), retrieved_at=retrieved_at,
        )

        if doc.get("robots") == "disallow":
            result.status = "ROBOTS_DISALLOWED"
            result.body = ""            # never ingest content we may not crawl
        elif doc.get("paywalled"):
            result.status = "PAYWALLED"  # metadata citable, body is not
            result.body = doc["body"]

        hits = self._scan_injection(doc["body"])
        if hits:
            result.injected = True
            result.injection_evidence = hits

        if self.cache_dir:
            key = sha256(url).replace("sha256:", "")[:24]
            d = dict(result.__dict__)
            d.pop("cached", None)
            (self.cache_dir / f"{key}.json").write_text(json.dumps(d, indent=1))
        return result

    @staticmethod
    def _scan_injection(body: str) -> list[str]:
        low = body.lower()
        return [label for pat, label in INJECTION_PATTERNS if re.search(pat, low)]

    @staticmethod
    def scan_sensitive(body: str) -> list[dict]:
        found = []
        for pat, label in SENSITIVE_PATTERNS:
            for m in re.finditer(pat, body, re.I):
                found.append({"kind": label, "span": [m.start(), m.end()]})
        return found

    # ── the offline "search engine": a deterministic lexical index over the corpus ──
    def search(self, query: str, *, limit: int = 10) -> list[dict]:
        terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
        scored = []
        for url, path in self._index.items():
            doc = json.loads(path.read_text())
            if doc["url"] != url:
                continue
            hay = (doc["title"] + " " + doc["body"]).lower()
            score = sum(hay.count(t) for t in terms)
            if doc["published"] and doc["published"][:4] >= "2024":
                score += 2                      # mild recency prior, declared and testable
            if score:
                scored.append({"url": doc["url"], "score": score,
                               "title": doc["title"], "tier": doc["source_tier"]})
        scored.sort(key=lambda r: (-r["score"], r["url"]))
        return scored[:limit]
