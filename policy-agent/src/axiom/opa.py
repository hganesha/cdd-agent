"""OPA client — the only way this agent talks to policy.

Design rules (see spec/02-skills.md):
  * every call is an explicit, logged subprocess invocation; no in-process policy evaluation;
  * the bundle is verified before use, and the revision is pinned into every decision;
  * policies are compiled with restricted capabilities: a policy that reaches the network or reads
    the host is rejected at authoring time, not discovered in production.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BINARY = ROOT / "bin" / "opa"

# Builtins a CDD policy has no business calling. Removing them from the compiler's capability set
# makes their use a compile error, which is how "policies cannot make network calls" is enforced.
DENIED_BUILTINS = [
    "http.send",
    "net.lookup_ip_addr",
    "opa.runtime",
    "trace",
    "io.jwt.decode_verify",
]


class OPAError(RuntimeError):
    def __init__(self, message: str, *, stderr: str = "", code: int = 1):
        super().__init__(message)
        self.stderr = stderr
        self.code = code


@dataclass
class EvalResult:
    value: object
    raw: dict
    duration_ms: float | None = None


def find_binary() -> str:
    for candidate in (os.environ.get("OPA_BIN"), str(DEFAULT_BINARY), "opa"):
        if candidate and (Path(candidate).exists() or shutil.which(candidate)):
            return candidate
    raise OPAError("OPA binary not found. Set OPA_BIN or place opa in bin/.")


class OPAClient:
    def __init__(self, binary: str | None = None, timeout_s: int = 30):
        self.binary = binary or find_binary()
        self.timeout_s = timeout_s

    # ── primitives ──
    def _run(self, args: list[str], *, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [self.binary, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
        )
        if check and proc.returncode != 0:
            raise OPAError(f"opa {' '.join(args)} failed", stderr=proc.stderr.strip(),
                           code=proc.returncode)
        return proc

    def version(self) -> str:
        out = self._run(["version"]).stdout
        for line in out.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
        return out.strip()

    def capabilities(self, *, deny: list[str] | None = None) -> dict:
        ver = self.version()
        # `opa capabilities --version` wants a release tag (vX.Y.Z) and rejects unknown ones,
        # so fall back to the built-in capability set of the running binary.
        for candidate in (f"v{ver}", ver):
            proc = self._run(["capabilities", "--version", candidate], check=False)
            if proc.returncode == 0 and proc.stdout.strip().startswith("{"):
                caps = json.loads(proc.stdout)
                break
        else:
            raise OPAError("could not obtain capabilities from the OPA binary")
        deny = set(deny if deny is not None else DENIED_BUILTINS)
        caps["builtins"] = [b for b in caps["builtins"] if b["name"] not in deny]
        return caps

    def capabilities_file(self, *, deny: list[str] | None = None) -> Path:
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(self.capabilities(deny=deny), fh)
        fh.close()
        return Path(fh.name)

    # ── evaluation ──
    def eval(self, query: str, *, data_paths: list[Path] | None = None,
             input_obj: dict | None = None, explain: str | None = None) -> EvalResult:
        args = ["eval", "--format=json"]
        for p in data_paths or []:
            args += ["-d", str(p)]
        if explain:
            args.append(f"--explain={explain}")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(input_obj or {}, fh)
            input_file = fh.name
        try:
            args += ["--input", input_file, query]
            proc = self._run(args, check=False)
            if proc.returncode != 0:
                raise OPAError(f"opa eval failed for {query}", stderr=proc.stderr.strip(),
                               code=proc.returncode)
            payload = json.loads(proc.stdout)
            if "errors" in payload:
                raise OPAError(payload["errors"][0].get("message", "eval error"),
                               stderr=json.dumps(payload["errors"][:1]))
            results = payload.get("result") or []
            if not results:
                return EvalResult(value=None, raw=payload)
            return EvalResult(value=results[0]["expressions"][0]["value"], raw=payload)
        finally:
            os.unlink(input_file)

    def eval_many(self, queries: dict[str, str], *, data_paths: list[Path],
                  input_obj: dict) -> dict[str, object]:
        """Batch several queries against one input. One process per query: cheap, and keeps the
        audit trail one-call-one-record."""
        return {name: self.eval(q, data_paths=data_paths, input_obj=input_obj).value
                for name, q in queries.items()}

    def explain(self, query: str, *, data_paths: list[Path], input_obj: dict) -> str:
        args = ["eval", "--format=json", "--explain=notes"]
        for p in data_paths:
            args += ["-d", str(p)]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(input_obj, fh)
            input_file = fh.name
        try:
            return self._run(args + ["--input", input_file, query], check=False).stdout
        finally:
            os.unlink(input_file)

    # ── static checks & tests ──
    def check(self, path: Path, *, restricted: bool = True, strict: bool = False) -> tuple[bool, str]:
        args = ["check", str(path)]
        if strict:
            args.append("--strict")
        if restricted:
            args += ["--capabilities", str(self.capabilities_file())]
        proc = self._run(args, check=False)
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()

    def test(self, path: Path, *, verbose: bool = False) -> tuple[bool, str]:
        args = ["test", str(path)]
        if verbose:
            args.append("--verbose")
        proc = self._run(args, check=False)
        return proc.returncode == 0, proc.stdout.strip()

    def fmt_check(self, path: Path) -> list[str]:
        proc = self._run(["fmt", "--list", str(path)], check=False)
        return [l for l in proc.stdout.splitlines() if l.strip()]

    def fmt(self, path: Path) -> None:
        self._run(["fmt", "-w", str(path)])

    # ── bundles ──
    def build(self, src: Path, out: Path, *, entrypoints: list[str]) -> Path:
        args = ["build", str(src), "-o", str(out)]
        for e in entrypoints:
            args += ["-e", e]
        self._run(args)
        return out

    def inspect(self, bundle: Path) -> dict:
        proc = self._run(["inspect", "--format=json", str(bundle)], check=False)
        if proc.returncode != 0:
            raise OPAError("opa inspect failed", stderr=proc.stderr.strip())
        return json.loads(proc.stdout)

    @staticmethod
    def sha256_file(path: Path) -> str:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
