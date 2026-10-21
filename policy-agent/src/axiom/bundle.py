"""Policy bundle lifecycle: build → verify → stage → activate → rollback.

The agent never writes policy into production. It can build and verify a bundle, and it can *stage*
one (a gated action requiring PLATFORM_SECURITY approval). Activation is a human action performed by
the policy service, recorded as a bundle event.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .opa import OPAClient, OPAError

ENTRYPOINTS = ["agent/decision/result", "agent/authority/result", "cdd/risk/result"]
ROOT = Path(__file__).resolve().parents[2]


class PolicyBundleService:
    def __init__(self, opa: OPAClient, root: Path = ROOT):
        self.opa = opa
        self.root = Path(root)
        self.src = self.root / "policy"
        self.bundles = self.root / "out" / "bundles"
        self.bundles.mkdir(parents=True, exist_ok=True)

    # ── build ──
    def build(self, revision: str) -> dict:
        ok, msg = self.opa.check(self.src, restricted=True)
        if not ok:
            raise OPAError("bundle refused: policy does not compile under restricted capabilities",
                           stderr=msg)
        tests_ok, test_out = self.opa.test(self.src)
        if not tests_ok:
            raise OPAError("bundle refused: policy tests failed", stderr=test_out)

        target = self.bundles / f"{revision}.tar.gz"
        self.opa.build(self.src, target, entrypoints=ENTRYPOINTS)

        files = {}
        for f in sorted(self.src.rglob("*")):
            if f.is_file():
                files[str(f.relative_to(self.src))] = self.opa.sha256_file(f)

        data = json.loads((self.src / "data.json").read_text())["config"]
        manifest = {
            "revision": revision,
            "policy_version": data["policy_version"],
            "sha256": self.opa.sha256_file(target),
            "size_bytes": target.stat().st_size,
            "built_at": self._now(),
            "agent_id": "AXIOM-POLICY",
            "opa_version": self.opa.version(),
            "entrypoints": ENTRYPOINTS,
            "files": files,
            "source_file_count": len(files),
            "restricted_capabilities": True,
            "denied_builtins": __import__("axiom.opa", fromlist=["DENIED_BUILTINS"]).DENIED_BUILTINS,
            "tests": {"status": "PASS", "summary": test_out.splitlines()[-1] if test_out else ""},
            "bundle_path": str(target.relative_to(self.root)),
            "status": "STAGED",
            "activated_by": None,
            "activated_at": None,
        }
        (self.bundles / f"{revision}.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest

    # ── verify: re-hash sources, confirm the archive matches the manifest ──
    def verify(self, revision: str) -> dict:
        mpath = self.bundles / f"{revision}.manifest.json"
        if not mpath.exists():
            raise OPAError(f"no manifest for revision {revision}")
        manifest = json.loads(mpath.read_text())
        problems = []

        bundle = self.root / manifest["bundle_path"]
        if not bundle.exists():
            problems.append("bundle archive missing")
        elif self.opa.sha256_file(bundle) != manifest["sha256"]:
            problems.append("bundle archive hash mismatch")

        for rel, expected in manifest["files"].items():
            actual_path = self.src / rel
            if not actual_path.exists():
                problems.append(f"source missing: {rel}")
                continue
            if self.opa.sha256_file(actual_path) != expected:
                problems.append(f"source hash mismatch: {rel}")

        if bundle.exists():
            try:
                info = self.opa.inspect(bundle)
                manifest["inspect"] = {
                    "rego_files": len(info.get("rego_version", {}).get("files", []) or [])
                    if isinstance(info.get("rego_version"), dict) else None,
                    "roots": info.get("roots"),
                }
            except OPAError:
                problems.append("bundle could not be inspected")

        return {"revision": revision, "valid": not problems, "problems": problems,
                "manifest": manifest}

    # ── stage / activate pointer ──
    def stage(self, revision: str) -> dict:
        """Staging is a gated action in policy (bundle.stage → PLATFORM_SECURITY)."""
        mpath = self.bundles / f"{revision}.manifest.json"
        manifest = json.loads(mpath.read_text())
        manifest["status"] = "STAGED"
        mpath.write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest

    def activate(self, revision: str, *, actor: str, approval_ticket: str) -> dict:
        """Only the policy service calls this, after a human approves. The agent has no path here."""
        verification = self.verify(revision)
        if not verification["valid"]:
            raise OPAError(f"refusing to activate unverified bundle: {verification['problems']}")
        manifest = verification["manifest"]
        manifest["status"] = "ACTIVE"
        manifest["activated_by"] = actor
        manifest["activated_at"] = self._now()
        manifest["approval_ticket"] = approval_ticket
        (self.bundles / f"{revision}.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        pointer = self.bundles / "ACTIVE"
        if pointer.exists():
            prev = pointer.read_text().strip()
            shutil.copy(self.bundles / f"{prev}.manifest.json",
                        self.bundles / f"{prev}.superseded.json")
        pointer.write_text(revision + "\n")
        return manifest

    def active(self) -> dict | None:
        pointer = self.bundles / "ACTIVE"
        if not pointer.exists():
            return None
        rev = pointer.read_text().strip()
        m = self.bundles / f"{rev}.manifest.json"
        return json.loads(m.read_text()) if m.exists() else None

    @staticmethod
    def _now() -> str:
        from .evidence import now
        return now()
