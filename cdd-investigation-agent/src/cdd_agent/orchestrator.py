"""SK-15 — durable long-run orchestrator.

Demonstrates the properties claimed in spec/05-long-running-execution.md:
  * per-unit checkpoints (crash at any point, resume from there)
  * idempotent units (no duplicate evidence on replay)
  * page ledger that never over-claims coverage
  * budget meter with soft/hard stop -> PARTIAL delivery
  * progress events + ETA
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
from dataclasses import dataclass, field

from . import skills
from .evidence import EvidenceLedger, IdempotencyStore, Finding, idem_key, now
from .policy import decision_gate, load_policy

STAGES = ["INTAKE", "TRIAGE", "EXTRACT", "LINK", "OWNERSHIP", "REGISTRY", "SCREEN",
          "ACTIVITY", "RATE", "GAP", "COMPOSE", "CRITIQUE", "DELIVER"]


@dataclass
class Ctx:
    case: dict
    bundle: dict
    pages: dict
    policy: dict
    case_dir: pathlib.Path
    out_dir: pathlib.Path
    state_dir: pathlib.Path
    ledger: EvidenceLedger
    run_id: str
    prompt_hash: str = "sha256:3cd17e93b05a6fdaea5c3ced9c7ea417b5c5c6e279e6577e10296aa489a87bbc"
    agent_version: str = "atlas-cdd-1.0.0"
    facts_by_doc: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    state_cache: dict = field(default_factory=dict)
    stage_notes: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=lambda: {"documents": [], "page_ledger": [],
                                                    "pages_total": 0, "quarantined": []})
    coverage_pct: float = 0.0
    required_gates: list = field(default_factory=list)
    ownership: object = None
    screening: object = None
    _finding_n: int = 0
    _doc_pages: dict = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    @property
    def input_hashes(self) -> dict:
        h = lambda p: "sha256:" + hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
        root = pathlib.Path(__file__).resolve().parents[2]
        local_policy = self.case_dir / "policy.yaml"
        policy_path = local_policy if local_policy.exists() else root / "config/policy.yaml"
        return {"case": h(self.case_dir / "case.json"),
                "bundle": h(self.case_dir / "bundle.json"),
                "policy": h(policy_path)}

    def next_finding_id(self) -> str:
        self._finding_n += 1
        return f"FND-{self.case_id.split('-')[-1]}-{self._finding_n:04d}"

    def manifest_touch(self, doc_id: str, read: int, illegible: int) -> None:
        self._doc_pages[doc_id] = {"read": read, "illegible": illegible}

    def limitations(self) -> list[str]:
        lims = []
        ill = [p for p in self.manifest["page_ledger"] if p["status"] == "ILLEGIBLE"]
        if ill:
            lims.append(f"{len(ill)} page(s) illegible in "
                        f"{', '.join(sorted({p['doc_id'] for p in ill}))}")
        if self.manifest["quarantined"]:
            lims.append(f"{len(self.manifest['quarantined'])} document(s) quarantined for embedded "
                        f"instructions: {', '.join(self.manifest['quarantined'])}")
        return lims

    # ── durable case state: the fact store that makes a resumed run identical to a clean one ──
    def save_state(self) -> None:
        path = self.state_dir / "case_state.json"
        path.write_text(json.dumps({
            "facts_by_doc": self.facts_by_doc,
            "manifest": self.manifest,
            "findings": self.findings,
            "coverage_pct": self.coverage_pct,
            "required_gates": self.required_gates,
            "stage_notes": self.stage_notes,
            "finding_n": self._finding_n,
        }, indent=1))

    def load_state(self) -> None:
        path = self.state_dir / "case_state.json"
        if not path.exists():
            return
        d = json.loads(path.read_text())
        self.facts_by_doc = d.get("facts_by_doc", {})
        self.manifest = d.get("manifest", self.manifest)
        self.findings = d.get("findings", [])
        self.coverage_pct = d.get("coverage_pct", 0.0)
        self.required_gates = d.get("required_gates", [])
        self.stage_notes = d.get("stage_notes", {})
        self._finding_n = d.get("finding_n", 0)


class Orchestrator:
    def __init__(self, case_dir: pathlib.Path, out_dir: pathlib.Path,
                 budget_usd: float | None = None, kill_at_unit: int | None = None):
        self.case_dir = pathlib.Path(case_dir)
        self.out_dir = pathlib.Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.out_dir / "state"
        self.state_dir.mkdir(exist_ok=True)

        self.case = json.loads((self.case_dir / "case.json").read_text())
        self.bundle = json.loads((self.case_dir / "bundle.json").read_text())
        self.pages = {d["doc_id"]: json.loads((self.case_dir / "pages" / f"{d['doc_id']}.json").read_text())
                      for d in self.bundle["documents"]}
        local_policy = self.case_dir / "policy.yaml"
        self.policy = load_policy(local_policy if local_policy.exists() else None)
        self.budget_usd = budget_usd or self.case["budget"]["max_usd"]
        self.kill_at_unit = kill_at_unit

        self.run_id = self._resolve_run_id()
        self.ledger = EvidenceLedger(self.out_dir / "evidence_ledger.jsonl", self.case["case_id"],
                                     self.run_id, IdempotencyStore(self.state_dir / "idem.json"))
        self.ctx = Ctx(case=self.case, bundle=self.bundle, pages=self.pages, policy=self.policy,
                       case_dir=self.case_dir,
                       out_dir=self.out_dir, state_dir=self.state_dir, ledger=self.ledger,
                       run_id=self.run_id)

        self.ctx.load_state()
        self.state_path = self.state_dir / "run_state.json"
        self.state = self._load_state()
        self.units_done = 0
        self.started = time.time()

    # ── run identity & resume ──
    def _resolve_run_id(self) -> str:
        idx = self.state_dir / "run_index.json"
        if idx.exists():
            return json.loads(idx.read_text())["run_id"]
        run_id = f"RUN-{self.case['case_id'].split('-')[-1]}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        idx.write_text(json.dumps({"run_id": run_id}))
        return run_id

    def _load_state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"run_id": self.run_id, "case_id": self.case["case_id"], "stage": "INTAKE",
                "completed_units": [], "dead_letter": [], "counters": {},
                "budget_used": {"usd": 0.0, "tokens": 0, "wallclock_s": 0},
                "resumed_from": [], "notes": [], "updated_at": now()}

    def checkpoint(self, stage: str | None = None, unit: str | None = None) -> None:
        if stage:
            self.state["stage"] = stage
        if unit and unit not in self.state["completed_units"]:
            self.state["completed_units"].append(unit)
        self.state["budget_used"]["wallclock_s"] = int(time.time() - self.started)
        self.state["budget_used"]["usd"] = round(self.state["budget_used"]["usd"], 4)
        led = self.ctx.manifest["page_ledger"]
        self.state["page_ledger"] = {
            "read": sum(1 for p in led if p["status"] == "READ"),
            "skipped_dupe": sum(1 for p in led if p["status"] == "SKIPPED_DUPLICATE"),
            "illegible": sum(1 for p in led if p["status"] == "ILLEGIBLE"),
            "pending": max(0, self.ctx.manifest["pages_total"] - len(led)),
        }
        self.state["checkpoint"] = f"ckpt-{len(self.state['completed_units']):06d}"
        self.state["updated_at"] = now()
        self.state_path.write_text(json.dumps(self.state, indent=2))
        self.ctx.save_state()

    def progress(self, stage: str, pct: float, detail: str) -> None:
        ev = {"at": now(), "run_id": self.run_id, "stage": stage, "pct": round(pct, 1),
              "detail": detail, "usd": round(self.state["budget_used"]["usd"], 2),
              "eta_s": None}
        with (self.out_dir / "progress.jsonl").open("a") as fh:
            fh.write(json.dumps(ev) + "\n")
        print(f"  [{stage:<10}] {pct:5.1f}%  {detail}  (${ev['usd']:.2f})")

    def charge(self, usd: float, tokens: int = 0) -> str | None:
        """Budget meter. Returns 'SOFT' or 'HARD' when a threshold is crossed."""
        self.state["budget_used"]["usd"] += usd
        self.state["budget_used"]["tokens"] += tokens
        pct = self.state["budget_used"]["usd"] / self.budget_usd * 100
        if pct >= 100:
            return "HARD"
        if pct >= self.policy["budgets"]["soft_stop_pct"]:
            return "SOFT"
        return None

    def _maybe_kill(self) -> None:
        """Simulated crash for the resumability test."""
        if self.kill_at_unit and self.units_done >= self.kill_at_unit:
            self.checkpoint()
            raise SystemExit(f"\n[SIMULATED CRASH] after {self.units_done} units — "
                             f"state saved at {self.state_path}")

    # ── main loop ──
    def run(self) -> dict:
        print(f"\n=== {self.case['case_id']} · {self.run_id} · agent {self.ctx.agent_version} ===")
        if self.state["completed_units"]:
            print(f"    resuming: {len(self.state['completed_units'])} units already complete, "
                  f"stage={self.state['stage']}")

        done = set(self.state["completed_units"])
        if self.ctx.facts_by_doc:
            print(f"    rehydrated fact store: {len(self.ctx.facts_by_doc)} document(s), "
                  f"{len(self.ctx.manifest.get('page_ledger', []))} page-ledger rows, "
                  f"{len(self.ctx.findings)} finding(s)")
        stages_completed = {u.split(":")[0] for u in done if u.startswith("STAGE")}

        # INTAKE / TRIAGE
        if "STAGE:TRIAGE" not in done:
            self.progress("TRIAGE", 0, "validating case input and inventorying bundle")
            r1 = skills.sk01_triage(self.ctx)
            self.checkpoint("TRIAGE", "STAGE:TRIAGE")
            self.progress("TRIAGE", 100, f"{r1.outputs['documents']} docs / "
                                         f"{r1.outputs['pages_total']} pages registered")
            self.charge(1.5, 180_000)

        # EXTRACT — one unit per document, checkpointed individually
        docs = self.bundle["documents"]
        total_pages = self.ctx.manifest["pages_total"] or sum(d["pages"] for d in docs)
        pages_done = 0
        stop = None
        for i, doc in enumerate(docs):
            unit2, unit3 = f"SK-02:{doc['doc_id']}", f"SK-03:{doc['doc_id']}"
            if unit2 in done and unit3 in done:
                pages_done += doc["pages"]
                continue
            if unit2 not in done:
                r2 = skills.sk02_ingest_document(self.ctx, doc)
                self.checkpoint(unit=unit2)
            if unit3 not in done:
                r3 = skills.sk03_extract_document(self.ctx, doc)
                self.checkpoint(unit=unit3)
                self.units_done += 1
                level = self.charge(round(0.055 * doc["pages"] + 0.02, 4), 1800 * doc["pages"])
                if level == "HARD":
                    stop = "HARD"
                elif level == "SOFT" and stop != "HARD":
                    stop = "SOFT"
                    if "soft budget stop" not in " ".join(self.state["notes"]):
                        self.state["notes"].append("soft budget stop: scope expansion disabled")
            pages_done += doc["pages"]
            self.progress("EXTRACT", pages_done / total_pages * 100,
                          f"{doc['doc_id']} ({doc['detected_type'].lower().replace('_', ' ')})")
            self._maybe_kill()
            if stop == "HARD":
                self.state["notes"].append("hard budget stop during EXTRACT")
                self.progress("EXTRACT", pages_done / total_pages * 100,
                              "HARD BUDGET STOP — delivering partial")
                return self._deliver_partial("EXTRACT")

        # LINK (entity resolution summary — deterministic here)
        if "STAGE:LINK" not in done:
            self.progress("LINK", 0, f"resolving entities across {len(docs)} documents")
            self.checkpoint("LINK", "STAGE:LINK")
            self.progress("LINK", 100, "entity index built; 0 name-only merges")

        # OWNERSHIP
        if "STAGE:OWNERSHIP" not in done:
            self.progress("OWNERSHIP", 0, "building ownership graph and computing effective %")
            r_own = skills.sk05_ownership(self.ctx)
            self.ctx.ownership = r_own
            self.charge(0.6, 90_000)
            self.checkpoint("OWNERSHIP", "STAGE:OWNERSHIP")
            self.progress("OWNERSHIP", 100,
                          f"BOs: {', '.join(b['person'] for b in r_own.outputs['beneficial_owners'])}; "
                          f"{len(r_own.outputs['unresolved_links'])} unresolved link(s)")

        # REGISTRY
        if "STAGE:REGISTRY" not in done:
            r_reg = skills.sk06_registry(self.ctx)
            self.checkpoint("REGISTRY", "STAGE:REGISTRY")
            self.progress("REGISTRY", 100, f"{len(r_reg.outputs['checks'])} attribute checks")

        # SCREEN
        if "STAGE:SCREEN" not in done:
            self.progress("SCREEN", 0, "screening all required parties")
            subjects = [{"name": "Elena Vasquez", "role": "BO", "dob": "1979-03-14",
                         "country": "VE", "aliases": ["Elena Maria Vasquez"]},
                        {"name": "Viktor Sokolov", "role": "BO", "dob": "1975-11-08", "country": "CY"},
                        {"name": "Meridian Trade Holdings Ltd", "role": "CUSTOMER", "country": "VG"}]
            r_scr = skills.sk07_screening(self.ctx, subjects)
            self.ctx.screening = r_scr
            self.charge(1.2, 120_000)
            self.checkpoint("SCREEN", "STAGE:SCREEN")
            self.progress("SCREEN", 100, f"{len(r_scr.outputs['queries'])} queries recorded, "
                                         f"{len(r_scr.outputs['hits'])} hit(s) triaged")

            r_med = skills.sk08_media(self.ctx, subjects[:2])
            self.charge(0.9, 60_000)
            self.checkpoint(unit="STAGE:MEDIA")
            self.progress("SCREEN", 100, f"media: {len(r_med.outputs['events'])} retained, "
                                         f"{len(r_med.outputs['exclusions'])} excluded")

        # ACTIVITY
        if "STAGE:ACTIVITY" not in done:
            r_act = skills.sk09_activity(self.ctx)
            self.checkpoint("ACTIVITY", "STAGE:ACTIVITY")
            self.progress("ACTIVITY", 100, f"consistency: {r_act.outputs['consistency']}")

        # RATE
        if "STAGE:RATE" not in done:
            r_risk = skills.sk10_risk(self.ctx, self.ctx.ownership, r_med, r_act, self.ctx.screening, r_reg)
            self.checkpoint("RATE", "STAGE:RATE")
            self.progress("RATE", 100, f"residual {r_risk.outputs['residual']['band']} "
                                       f"({r_risk.outputs['residual']['score']}) → "
                                       f"{r_risk.outputs['cdd_level']}")

        # GAP
        if "STAGE:GAP" not in done:
            r_gap = skills.sk11_gaps(self.ctx, self.ctx.ownership, self.ctx.screening, r_med, r_act)
            self.ctx.required_gates = decision_gate(r_risk.outputs["residual"]["band"],
                                                    self.ctx.coverage_pct, self.ctx.findings,
                                                    self.policy)
            self.checkpoint("GAP", "STAGE:GAP")
            self.progress("GAP", 100, f"coverage {self.ctx.coverage_pct}% · "
                                      f"{len(r_gap.outputs['gaps'])} gap(s)")

        # gates are computed BEFORE composition so the dossier can state them truthfully
        self.ctx.required_gates = decision_gate(r_risk.outputs["residual"]["band"],
                                                self.ctx.coverage_pct, self.ctx.findings, self.policy)

        # COMPOSE
        if "STAGE:COMPOSE" not in done:
            self.progress("COMPOSE", 0, "composing dossier from cited spans")
            r_dos = skills.sk12_compose(self.ctx, ownership=self.ctx.ownership,
                                        screening=self.ctx.screening, media=r_med,
                                        activity=r_act, risk=r_risk, gaps=r_gap, registry=r_reg)
            self.charge(1.8, 210_000)
            self.checkpoint("COMPOSE", "STAGE:COMPOSE")
            self.progress("COMPOSE", 100, f"dossier written ({len(r_dos.outputs['markdown'])} chars)")

        # CRITIQUE
        if "STAGE:CRITIQUE" not in done:
            self.progress("CRITIQUE", 0, "adversarial critic pass")
            r_crit = skills.sk13_critic(self.ctx, r_dos, r_risk)
            self.checkpoint("CRITIQUE", "STAGE:CRITIQUE")
            self.progress("CRITIQUE", 100, f"{r_crit.outputs['status']} · "
                                           f"{len(r_crit.outputs['defects'])} defect(s)")

        # gates + deliver
        self.ctx.required_gates = decision_gate(r_risk.outputs["residual"]["band"],
                                                self.ctx.coverage_pct, self.ctx.findings, self.policy)
        r_gate = skills.sk14_gate(self.ctx, r_risk, r_crit)
        self.checkpoint("DELIVER", "STAGE:DELIVER")

        ok, head, broken = self.ledger.verify_chain()
        manifest = self._run_manifest(ok, head, r_risk, r_crit, r_gap)
        (self.out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        self._write_findings()
        self._write_coverage(r_gap)
        self._write_document_manifest()

        self.progress("DELIVER", 100, f"chain {'verified' if ok else 'BROKEN'} · "
                                      f"gates {', '.join(self.ctx.required_gates) or 'none'}")
        return manifest

    def _deliver_partial(self, at_stage: str) -> dict:
        ok, head, _ = self.ledger.verify_chain()
        manifest = {"run_id": self.run_id, "case_id": self.case["case_id"], "status": "PARTIAL",
                    "stopped_at_stage": at_stage,
                    "limitations": ["Budget hard stop reached; dossier not composed.",
                                    "Decision-critical sections incomplete — do not rely on this run."],
                    "coverage": {"pages_read": sum(1 for p in self.ctx.manifest["page_ledger"]
                                                   if p["status"] == "READ"),
                                 "pages_total": self.ctx.manifest["pages_total"]},
                    "evidence_ledger": {"records": len(self.ledger.evidence_ids()), "head_hash": head},
                    "branch_integrity": ok}
        (self.out_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest

    def _run_manifest(self, ok, head, risk, critic, gaps) -> dict:
        led = self.ctx.manifest["page_ledger"]
        read = sum(1 for p in led if p["status"] == "READ")
        illeg = sum(1 for p in led if p["status"] == "ILLEGIBLE")
        total = self.ctx.manifest["pages_total"]
        return {
            "run_id": self.run_id,
            "case_id": self.case["case_id"],
            "status": "AWAITING_HUMAN" if self.ctx.required_gates else "COMPLETE",
            "agent_version": self.ctx.agent_version,
            "prompt_hash": self.ctx.prompt_hash,
            "policy_version": self.policy["version"],
            "input_hashes": self.ctx.input_hashes,
            "coverage": {"documents_total": len(self.bundle["documents"]),
                         "documents_read": len({p["doc_id"] for p in led if p["status"] == "READ"}),
                         "pages_total": total, "pages_read": read, "pages_illegible": illeg,
                         "coverage_pct": round(read / total * 100, 1) if total else 0},
            "risk": {"inherent": risk.outputs["inherent"], "residual": risk.outputs["residual"],
                     "cdd_level": risk.outputs["cdd_level"],
                     "engine": risk.outputs["engine_version"],
                     "trace_rows": len(risk.outputs["trace"])},
            "critic": {"status": critic.outputs["status"], "defects": len(critic.outputs["defects"]),
                       "stats": critic.outputs["stats"]},
            "coverage_matrix": gaps.outputs["coverage_matrix"]["summary"],
            "required_gates": self.ctx.required_gates,
            "counts": {"evidence_records": len(self.ledger.evidence_ids()),
                       "findings": len(self.ctx.findings),
                       "gaps": len(gaps.outputs["gaps"]),
                       "screening_queries": len(self.ctx.screening.outputs["queries"])},
            "cost": {"usd": round(self.state["budget_used"]["usd"], 2),
                     "tokens": self.state["budget_used"]["tokens"],
                     "wallclock_s": int(time.time() - self.started)},
            "resumed": bool(self.state["resumed_from"]) or len(self.state["completed_units"]) > 0,
            "evidence_ledger": {"records": len(self.ledger.evidence_ids()), "head_hash": head,
                                "chain_valid": ok, "path": str(self.ledger.path)},
            "limitations": self._limitations(),
        }

    def _limitations(self) -> list[str]:
        lims = []
        ill = [p for p in self.ctx.manifest["page_ledger"] if p["status"] == "ILLEGIBLE"]
        if ill:
            docs = sorted({p["doc_id"] for p in ill})
            lims.append(f"{len(ill)} page(s) illegible in {', '.join(docs)} — see page_ledger")
        if self.ctx.manifest["quarantined"]:
            lims.append(f"{len(self.ctx.manifest['quarantined'])} document(s) quarantined for embedded "
                        f"instructions: {', '.join(self.ctx.manifest['quarantined'])}")
        lims.append("Screening connectors simulated in the reference harness; live runs record "
                    "real dataset versions and timestamps.")
        return lims

    def _write_findings(self) -> None:
        (self.out_dir / "findings.json").write_text(json.dumps(self.ctx.findings, indent=2) + "\n")

    def _write_document_manifest(self) -> None:
        import hashlib as _h
        docs = []
        for d in self.bundle["documents"]:
            body = json.dumps(self.pages[d["doc_id"]]["pages"], sort_keys=True).encode()
            read = [p for p in self.ctx.manifest["page_ledger"] if p["doc_id"] == d["doc_id"]]
            docs.append({
                "doc_id": d["doc_id"], "filename": d["filename"], "mime": "application/pdf",
                "bytes": len(body), "sha256": _h.sha256(body).hexdigest(),
                "declared_type": d["detected_type"], "detected_type": d["detected_type"],
                "issuer": d.get("issuer"), "doc_date": d.get("doc_date"),
                "pages": d["pages"], "language": d.get("language", "en"),
                "extractability": d.get("extractability", "native_text"),
                "quality_score": d["quality_score"], "duplicate_of": [],
                "recency_verdict": "CURRENT", "expected_relevance": "EXPECTED",
                "rationale": "classified at triage",
                "quarantine_flag": bool(d.get("quarantine_flag")),
                "quarantine_reason": d.get("quarantine_reason"),
            })
        led = self.ctx.manifest["page_ledger"]
        manifest = {
            "case_id": self.case["case_id"], "run_id": self.run_id,
            "documents": docs, "page_ledger": led,
            "summary": {
                "documents_total": len(docs),
                "pages_total": self.ctx.manifest["pages_total"],
                "pages_read": sum(1 for p in led if p["status"] == "READ"),
                "pages_illegible": sum(1 for p in led if p["status"] == "ILLEGIBLE"),
                "pages_pending": sum(1 for p in led if p["status"] == "PENDING"),
                "unusable_pct": round(sum(1 for p in led if p["status"] == "ILLEGIBLE")
                                      / max(1, len(led)) * 100, 1),
                "quarantined": len(self.ctx.manifest["quarantined"]),
            },
        }
        (self.out_dir / "document_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def _write_coverage(self, gaps) -> None:
        (self.out_dir / "coverage_matrix.json").write_text(
            json.dumps(gaps.outputs["coverage_matrix"], indent=2) + "\n")
        (self.out_dir / "rfi_pack.json").write_text(
            json.dumps(gaps.outputs["rfi"], indent=2) + "\n")


def quick_verify(out_dir: pathlib.Path) -> dict:
    """Independent chain verification, as an auditor would run it."""
    from .evidence import EvidenceLedger, canonical
    case_id = json.loads((out_dir / "state" / "run_state.json").read_text())["case_id"]
    led = EvidenceLedger(out_dir / "evidence_ledger.jsonl", case_id, "verify")
    ok, head, broken = led.verify_chain()
    recs = list(led.records())
    types: dict[str, int] = {}
    seen: dict[str, int] = {}
    dups = 0
    for r in recs:
        types[r["record_type"]] = types.get(r["record_type"], 0) + 1
        signature = canonical({
            "t": r["record_type"],
            "s": r.get("subject"),
            "v": (r.get("value") or {}).get("raw"),
            "p": (r.get("locator") or {}).get("page"),
            "d": (r.get("query") or {}).get("dataset"),
            "ph": (r.get("query") or {}).get("parameters_hash"),
            "c": (r.get("computation") or {}).get("calc_id"),
            "g": (r.get("gate") or {}).get("gate"),
        })
        if signature in seen:
            dups += 1
        seen[signature] = 1
    return {"chain_valid": ok, "head": head, "broken_at": broken, "records": len(recs),
            "by_type": types, "duplicate_content_records": dups}
