#!/usr/bin/env python3
"""OPS agents CLI.

  python -m opsagents.cli run      --case CASE-4831            full loop, printed
  python -m opsagents.cli profile  --case CASE-4831            profile only
  python -m opsagents.cli verify   --case CASE-4831            verification report only
  python -m opsagents.cli rfi      --case CASE-4831            draft the RFI (never sends)
  python -m opsagents.cli decide   --case CASE-4831            disposition, through the policy service
  python -m opsagents.cli ledger                                verify the evidence chain
  python -m opsagents.cli show     --case CASE-4831 --what disposition
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from opsagents.common.ledger import IdempotencyStore, OpsLedger     # noqa: E402
from opsagents.common.runs import (BudgetExceeded, KillSwitch, RunState,  # noqa: E402
                                   default_budget)
from opsagents.common.mockapi import MockApi                        # noqa: E402
from opsagents.common.policy import PolicyEngine                    # noqa: E402
from opsagents.decision.agent import DecisionAgent                  # noqa: E402
from opsagents.outreach.agent import OutreachAgent                  # noqa: E402
from opsagents.profile.agent import ProfileAgent                    # noqa: E402
from opsagents.verification.agent import VerificationAgent          # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "out"

CASES = {
    "CASE-4831": {"case_id": "CASE-4831", "customer_id": "CUST-1001",
                  "customer_name": "Meridian Trade Holdings Ltd",
                  "cdd_level_requested": "EDD", "jurisdiction_profile": "EU_AMLD",
                  "as_of": "2026-10-21T00:00:00Z",
                  "prior_rfi_rounds": 1},
    "CASE-4902": {"case_id": "CASE-4902", "customer_id": "CUST-2002",
                  "customer_name": "Nordwind Logistics GmbH",
                  "cdd_level_requested": "CDD", "jurisdiction_profile": "EU_AMLD",
                  "as_of": "2026-10-21T00:00:00Z",
                  "prior_rfi_rounds": 0},
}


def _ctx(mock: str = "mock/api", ledger_path: pathlib.Path | None = None):
    ledger = OpsLedger(ledger_path or (OUT / "ops_evidence.jsonl"), agent_id="OPS",
                       idem=IdempotencyStore(OUT / "state" / "idem.json"))
    policy = PolicyEngine()
    api = MockApi(ROOT / mock, ledger, sleep=False)
    return ledger, policy, api


def _case(args) -> dict:
    if args.case in CASES:
        case = dict(CASES[args.case])
    else:
        if not getattr(args, "customer", None):
            raise SystemExit(f"unknown case {args.case}: pass --customer CUST-nnnn")
        case = {"case_id": args.case, "customer_id": args.customer,
                "customer_name": getattr(args, "name", None) or args.customer,
                "cdd_level_requested": getattr(args, "cdd", None) or "CDD",
                "jurisdiction_profile": getattr(args, "jurisdiction", None) or "EU_AMLD",
                "prior_rfi_rounds": 0, "as_of": "2026-10-21T00:00:00Z"}
    case["as_of"] = args.as_of or case["as_of"]
    return case


def _state_path(case: dict, agent: str) -> pathlib.Path:
    return OUT / "state" / f"{case['case_id']}-{agent}.json"


def _build_profile(ledger, policy, api, case, *, resume: bool = True,
                   budget: dict | None = None, kill_at: int | None = None) -> dict:
    """Run OPS-PROFILE as a resumable job. The checkpoint is keyed on (case, agent), so `--resume`
    picks up wherever the last process died."""
    pack = None
    pack_path = ROOT.parent / "external-search-agent" / "out" / "evidence_pack.json"
    if pack_path.exists():
        pack = json.loads(pack_path.read_text())
    state = RunState.open(_state_path(case, "profile"), agent_id="OPS-PROFILE",
                          case_id=case["case_id"], budget=budget or default_budget(),
                          resume=resume)
    profile_path = OUT / "profiles" / f"{case['case_id']}.json"
    state.resumed = bool(state.stage_names())      # stages already there = this is a resume
    agent = ProfileAgent(api, ledger, policy)
    try:
        prof = agent.build({"case_id": case["case_id"], "customer_id": case["customer_id"],
                            "as_of": case["as_of"], "evidence_pack": pack},
                           state=state, kill=KillSwitch(kill_at))
    except BudgetExceeded as exc:
        state.finish("PARTIAL")
        print(f"  profile: PARTIAL — {exc}", file=sys.stderr)
        print(f"  checkpoint {_state_path(case, 'profile')} holds "
              f"{len(state.stage_names())} completed stages; resume with --resume and a larger budget",
              file=sys.stderr)
        return None
    except SystemExit as exc:                 # the kill switch: propagate the crash status
        state.finish("PARTIAL")
        print(f"  profile: killed mid-run ({exc.code}) — checkpoint written; resume with --resume",
              file=sys.stderr)
        raise
    state.record("assemble", {"profile_id": prof["profile_id"]}, agent="OPS-PROFILE",
                 evidence_id=prof["evidence_id"])
    state.finish("COMPLETE")
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json.dumps(prof, indent=2) + "\n")
    (OUT / "state" / f"{case['case_id']}-profile-summary.json").write_text(
        json.dumps(state.summary(), indent=2) + "\n")
    return prof


def args_safe_use_pack() -> bool:
    """The search pack is an *optional* input: if the search agent has not run, the profile simply
    has no external adverse-media evidence, and says so."""
    return True


def _budget(args) -> dict:
    b = default_budget()
    if getattr(args, "budget", None) is not None:
        b["api_calls"] = args.budget
    if getattr(args, "page_budget", None) is not None:
        b["pages"] = args.page_budget
    return b


def cmd_run(args) -> int:
    ledger, policy, api = _ctx(args.mock)
    case = _case(args)
    prof = _build_profile(ledger, policy, api, case, resume=not args.fresh,
                          budget=_budget(args), kill_at=args.kill_at)
    if prof is None:
        return 3
    verifier = VerificationAgent(ledger, policy)
    pre = verifier.verify({"case": case, "profile": prof, "as_of": case["as_of"]},
                          stage="pre_decision")

    outreach = OutreachAgent(ledger, policy)
    obligations = _obligations_for(prof, case)
    rfi = None
    try:
        rfi = outreach.draft(case=case, profile=prof, obligations=obligations,
                             prior_thread={"rounds_completed": case["prior_rfi_rounds"]},
                             as_of=case["as_of"][:10])
    except Exception as exc:                       # noqa: BLE001 — a refusal is a valid outcome
        print(f"  outreach: no RFI — {exc}")

    disp = DecisionAgent(ledger, policy, verifier).propose(
        case=case, profile=prof, verification=pre, rfi=rfi)
    pin = DecisionAgent(ledger, policy).policy_input(case=case, profile=prof, verification=pre)
    post = verifier.verify({"case": case, "profile": prof, "policy_input": pin,
                            "policy_decision": policy.decide(pin),
                            "disposition": disp, "rfi": rfi,
                            "approval": (disp["approval"] if rfi else None),
                            "as_of": case["as_of"]}, stage="pre_finalisation")

    (OUT / "dispositions").mkdir(parents=True, exist_ok=True)
    (OUT / "dispositions" / f"{case['case_id']}.json").write_text(json.dumps(disp, indent=2) + "\n")
    if rfi:
        (OUT / "rfi").mkdir(parents=True, exist_ok=True)
        (OUT / "rfi" / f"{rfi['rfi_id']}.json").write_text(json.dumps(rfi, indent=2) + "\n")
    _print_run(case, prof, pre, rfi, disp, post)
    return 0 if post["critic_status"] == "PASS" else 2


def _obligations_for(prof: dict, case: dict) -> list[dict]:
    """What the case needs, before the policy has spoken — used only to draft outreach. The policy's
    own obligation list is authoritative and arrives with the decision."""
    obs = []
    if prof["ownership"]["unresolved_pct"] > 0:
        obs.append({"kind": "CLOSE_UBO_GAP", "because": "UNRESOLVED_BENEFICIAL_OWNERSHIP"})
    if not prof["screening"]["screening_complete"]:
        obs.append({"kind": "COMPLETE_SCREENING", "because": "SCREENING_INCOMPLETE"})
    if case["cdd_level_requested"] == "EDD":
        obs.append({"kind": "HUMAN_EDD_APPROVAL", "because": "CDD_LEVEL_EDD"})
    if any(e["rde_id"] == "RDE-07" and e["status"] != "SATISFIED"
           for e in prof["coverage"]["elements"]):
        obs.append({"kind": "OBTAIN_SOURCE_OF_WEALTH", "because": "JURISDICTION_REQUIRES_SOW"})
    return obs


def cmd_profile(args) -> int:
    ledger, policy, api = _ctx(args.mock)
    prof = _build_profile(ledger, policy, api, _case(args), resume=not args.fresh,
                          budget=_budget(args), kill_at=args.kill_at)
    if prof is None:
        return 3
    print(json.dumps({k: prof[k] for k in ("profile_id", "customer_id", "as_of", "coverage",
                                           "factors_triggered", "data_quality")}, indent=2))
    return 0


def cmd_verify(args) -> int:
    ledger, policy, _ = _ctx(args.mock)
    prof = json.loads((OUT / "profiles" / f"{args.case}.json").read_text())
    case = _case(args)
    report = VerificationAgent(ledger, policy).verify(
        {"case": case, "profile": prof, "as_of": case["as_of"]}, stage=args.stage)
    print(json.dumps(report, indent=2))
    return 0 if report["critic_status"] == "PASS" else 2


def cmd_rfi(args) -> int:
    ledger, policy, _ = _ctx(args.mock)
    prof = json.loads((OUT / "profiles" / f"{args.case}.json").read_text())
    case = _case(args)
    agent = OutreachAgent(ledger, policy)
    rfi = agent.draft(case=case, profile=prof, obligations=_obligations_for(prof, case),
                      prior_thread={"rounds_completed": case["prior_rfi_rounds"]},
                      as_of=case["as_of"][:10])
    (OUT / "rfi").mkdir(parents=True, exist_ok=True)
    (OUT / "rfi" / f"{rfi['rfi_id']}.json").write_text(json.dumps(rfi, indent=2) + "\n")
    print(json.dumps({k: rfi[k] for k in ("rfi_id", "round", "state", "due_date", "subject",
                                          "covers_rde_ids", "internal_actions", "approval")},
                     indent=2))
    print("\n" + rfi["body"])
    return 0


def cmd_decide(args) -> int:
    ledger, policy, _ = _ctx(args.mock)
    prof = json.loads((OUT / "profiles" / f"{args.case}.json").read_text())
    case = _case(args)
    verifier = VerificationAgent(ledger, policy)
    pre = verifier.verify({"case": case, "profile": prof, "as_of": case["as_of"]},
                          stage="pre_decision")
    disp = DecisionAgent(ledger, policy, verifier).propose(case=case, profile=prof, verification=pre)
    (OUT / "dispositions").mkdir(parents=True, exist_ok=True)
    (OUT / "dispositions" / f"{case['case_id']}.json").write_text(json.dumps(disp, indent=2) + "\n")
    print(json.dumps({k: disp[k] for k in ("disposition_id", "recommendation", "state",
                                           "policy", "policy_admissibility", "actions",
                                           "approval")}, indent=2))
    print("\n" + disp["narrative"])
    return 0


def cmd_ledger(args) -> int:
    ledger, _, _ = _ctx()
    ok, head, broken = ledger.verify_chain()
    recs = list(ledger.records())
    types: dict[str, int] = {}
    for r in recs:
        types[r["record_type"]] = types.get(r["record_type"], 0) + 1
    print(json.dumps({"chain_valid": ok, "head": head, "broken_at": broken,
                      "records": len(recs), "by_type": types}, indent=2))
    return 0 if ok else 2


def cmd_show(args) -> int:
    path = {"profile": OUT / "profiles" / f"{args.case}.json",
            "disposition": OUT / "dispositions" / f"{args.case}.json"}.get(args.what)
    if not path or not path.exists():
        print(f"nothing to show for {args.what} {args.case} — run the agent first", file=sys.stderr)
        return 1
    print(path.read_text())
    return 0


def _print_run(case, prof, pre, rfi, disp, post) -> None:
    print(f"\n=== {case['case_id']} · {prof['customer_360']['legal_name']} ===")
    print(f"  profile        {prof['profile_id']}  factors {len(prof['factors_triggered'])}  "
          f"coverage {prof['coverage']['coverage_pct']}%")
    print(f"  verification   {pre['critic_status']}  ({pre['counts']['pass']} pass, "
          f"{pre['counts']['fail']} fail, {pre['counts']['not_applicable']} n/a)")
    print(f"  policy         {disp['policy']['outcome']:<22} band {disp['policy']['risk']['band']}"
          f" (weighted {disp['policy']['risk']['weighted_score_band']}, "
          f"score {disp['policy']['risk']['score']})  route {disp['policy']['route']}")
    print(f"  recommendation {disp['recommendation']}  admissible="
          f"{disp['policy_admissibility']['admissible']}  from {disp['approval']['approver_role']}")
    if rfi:
        print(f"  outreach       {rfi['rfi_id']} round {rfi['round']}/{rfi['max_rounds']} "
              f"{rfi['state']}  (approval {rfi['approval']['approver_role']}, "
              f"{rfi['approval']['ttl_hours']}h)")
    print(f"  obligations    {', '.join(o['kind'] for o in disp['obligations'])}")
    for a in disp["actions"]:
        print(f"    - {a['obligation']:<24} {a['owner']:<14} {a['status']:<18} due {a['due']}")
    print(f"  state          {disp['state']}   (finalised: {disp['finalised']}, "
          f"agent_may_finalise: {disp['policy_admissibility']['agent_may_finalise']})")
    print(f"  final verify   {post['critic_status']}  ({post['counts']['fail']} failed)")
    print()


def main() -> int:
    p = argparse.ArgumentParser(prog="opsagents", description="OPS decision, profiling, "
                                                              "verification and outreach agents")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("profile", cmd_profile), ("verify", cmd_verify),
                     ("rfi", cmd_rfi), ("decide", cmd_decide), ("ledger", cmd_ledger),
                     ("show", cmd_show)):
        sp = sub.add_parser(name)
        if name != "ledger":
            sp.add_argument("--case", default="CASE-4831",
                            help="CASE-4831 and CASE-4902 ship as demo cases; others need --customer")
            sp.add_argument("--as-of", default=None)
            sp.add_argument("--resume", action="store_true",
                            help="reuse the last checkpoint for this case")
            sp.add_argument("--fresh", action="store_true", help="ignore any checkpoint")
            sp.add_argument("--kill-at", type=int, default=None,
                            help="simulate a crash at the Nth API call (exit 70)")
            sp.add_argument("--budget", type=int, default=None,
                            help="hard cap on API calls for this run")
            sp.add_argument("--page-budget", type=int, default=None,
                            help="hard cap on transaction pages for this run")
            sp.add_argument("--mock", default="mock/api", help="fixture root (mock/api-scale for scale)")
            sp.add_argument("--customer", default=None, help="customer id for a case not in the demo set")
            sp.add_argument("--name", default=None, help="customer name for an ad-hoc case")
            sp.add_argument("--cdd", default=None, choices=["SDD", "CDD", "EDD"])
            sp.add_argument("--jurisdiction", default=None,
                            choices=["US_FED_FINCEN_CDD", "EU_AMLD", "UK_MLR_2017"])
        if name == "verify":
            sp.add_argument("--stage", default="pre_decision")
        if name == "show":
            sp.add_argument("--what", default="disposition", choices=["profile", "disposition"])
        sp.set_defaults(func=fn)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
