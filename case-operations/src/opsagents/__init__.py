"""OPS-* agents — decision, profiling, verification and customer outreach.

Four agents that sit on top of the three existing packages:

    WEBIS-SEARCH (evidence)  →  CDD-INVESTIGATION (findings)  →  AXIOM-POLICY (decision)
                                        │
                          ┌─────────────┴───────────────────────────────┐
                          │  OPS agents                                 │
                          │  PROFILE      internal customer 360 / tx / cp│
                          │  VERIFY       independent quality gate       │
                          │  OUTREACH     RFI lifecycle                  │
                          │  DECIDE       disposition, admitted by policy│
                          └─────────────────────────────────────────────┘
"""
__version__ = "1.0.0"
AGENT_IDS = {
    "profile": "OPS-PROFILE",
    "verification": "OPS-VERIFY",
    "outreach": "OPS-OUTREACH",
    "decision": "OPS-DECIDE",
}
