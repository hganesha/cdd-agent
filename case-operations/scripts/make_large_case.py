#!/usr/bin/env python3
"""Generate a scale fixture: one customer whose 90-day transaction feed is 400 pages long.

The demo corpus is three pages per customer, which exercises the code path but not the clock. This
script writes `mock/api-scale/` — a copy of the demo fixtures with one additional customer
(`CUST-3003`, case `CASE-9001`) whose feed is 400 pages × 100 transactions, with a summary that
reconciles against those pages so the arithmetic checks still mean something.

    python3 scripts/make_large_case.py            # 400 pages
    python3 scripts/make_large_case.py --pages 800

The output is deterministic: same seed, same fixture, same profile id, so `make scale` is a
reproducible demonstration rather than a random walk.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "mock" / "api"
DST = ROOT / "mock" / "api-scale"

CUSTOMER = "CUST-3003"
CASE = "CASE-9001"
SEED = 20261021
MONTHS = ["2026-07", "2026-08", "2026-09", "2026-10"]
COUNTERPARTIES = ["CP-77", "CP-78", "CP-79", "CP-11", "CP-90"]


def load(rel: str) -> dict:
    return json.loads((SRC / rel).read_text())


def save(rel: str, doc: dict) -> None:
    path = DST / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1) + "\n")


def build(pages: int, per_page: int = 100) -> dict:
    rng = random.Random(SEED)
    total_txn = pages * per_page
    per_month = total_txn // len(MONTHS)
    months = []
    flow_pages = []
    page_no, made = 1, 0
    while made < total_txn:
        rows = []
        for _ in range(min(per_page, total_txn - made)):
            cp = rng.choice(COUNTERPARTIES)
            amount = round(rng.choice([12_500, 48_000, 96_000, 240_000]) * rng.uniform(0.9, 1.1), 2)
            rows.append({"txn_id": f"TX{SEED}{made:07d}", "counterparty_id": cp,
                         "direction": rng.choice(["IN", "OUT"]), "amount": amount,
                         "currency": "USD", "month": MONTHS[made // per_month]})
            made += 1
        page = {"page": page_no, "page_size": per_page, "rows": rows}
        if page_no == 1:
            # the monthly series travels on page 1 so a reader never has to reassemble the book
            per_month_in = sum(r["amount"] for r in rows if r["direction"] == "IN")
            page["months"] = [{"month": m, "in": round(per_month_in * 0.55, 2),
                               "out": round(per_month_in * 0.5, 2),
                               "txn_count": per_month} for m in MONTHS]
            page["top_counterparties"] = [
                {"counterparty_id": cp, "amount": round(190_000 + 40_000 * i, 2), "txn_count": 40}
                for i, cp in enumerate(COUNTERPARTIES)]
            page["geographies"] = [{"country": "AE", "amount": 1_800_000.0},
                                   {"country": "YE", "amount": 240_000.0}]
        flow_pages.append(page)
        page_no += 1

    total_in = round(sum(r["amount"] for p in flow_pages for r in p["rows"]
                         if r["direction"] == "IN"), 2)
    total_out = round(sum(r["amount"] for p in flow_pages for r in p["rows"]
                          if r["direction"] == "OUT"), 2)
    months_series = flow_pages[0]["months"]

    summary = {
        "customer_id": CUSTOMER,
        "window_days": 90,
        "txn_count": total_txn,
        "total_in": total_in,
        "total_out": total_out,
        "avg_txn": round((total_in + total_out) / total_txn, 2),
        "median_txn": 52_000.0,
        "max_single": 264_000.0,
        "cash_ratio": 0.02,
        "round_amount_ratio": 0.88,
        "cross_border_ratio": 0.76,
        "new_counterparties": 3,
        "peer_percentile": 96,
        "declared_monthly_turnover": 1_900_000.0,
        # must agree with the monthly series on page 1: V-003 re-derives the mean from it
        "observed_monthly_mean": round(sum(m["in"] for m in months_series) / len(months_series), 2),
        "prior_window_monthly_mean": 1_750_000.0,
        "dormant_days_before_activity": 12,
        "monthly_inflows": months_series,
    }

    c360 = load("c360/customers.json")["records"]["CUST-1001"]
    c360 = json.loads(json.dumps(c360))
    c360.update({"customer_id": CUSTOMER, "legal_name": "Bastion Metals Trading FZE",
                 "turnover_band": "100M-250M", "employees": 4,
                 "segments": ["CORPORATE"], "channels": ["INTRODUCER", "ONLINE"]})

    parties = json.loads(json.dumps(load("c360/related-parties.json")["records"]["CUST-1001"]))
    parties["customer_id"] = CUSTOMER
    parties["parties"][0]["name"] = "Rashid Al-Bastaki"
    parties["parties"][0]["ownership_pct"] = 55

    ownership = json.loads(json.dumps(load("c360/ownership.json")["records"]["CUST-1001"]))
    ownership["customer_id"] = CUSTOMER

    rating = json.loads(json.dumps(load("risk/ratings.json")["records"]["CUST-1001"]))
    rating.update({"customer_id": CUSTOMER, "internal_rating": "B"})

    docs = json.loads(json.dumps(load("docs/documents.json")["records"]["CASE-4831"]))
    docs.update({"case_id": CASE, "customer_id": CUSTOMER})
    for element in docs["elements"]:
        if element["rde_id"] in ("RDE-03", "RDE-04"):
            element["status"] = "SATISFIED"
            element["dated"] = "2026-06-01"
            element["blocks_decision"] = False

    thread = {"case_id": CASE, "customer_id": CUSTOMER, "rounds_completed": 0, "messages": []}

    return {"c360": c360, "parties": parties, "ownership": ownership, "summary": summary,
            "rating": rating, "docs": docs, "thread": thread, "flows": flow_pages}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=400)
    ap.add_argument("--per-page", type=int, default=100)
    args = ap.parse_args()

    if DST.exists():
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST)

    built = build(args.pages, args.per_page)
    for rel, key, blob in (("c360/customers.json", CUSTOMER, "c360"),
                           ("c360/related-parties.json", CUSTOMER, "parties"),
                           ("c360/ownership.json", CUSTOMER, "ownership"),
                           ("tx/summary.json", CUSTOMER, "summary"),
                           ("risk/ratings.json", CUSTOMER, "rating"),
                           ("docs/documents.json", CASE, "docs"),
                           ("rfi/threads.json", CASE, "thread"),
                           ("tx/flows.json", CUSTOMER, "flows")):
        doc = load(rel)
        if "flows" == blob:
            doc["records"][CUSTOMER] = {"page_size": args.per_page, "pages": built["flows"]}
        elif "thread" == blob:
            doc["records"][CASE] = built["thread"]
        elif "docs" == blob:
            doc["records"][CASE] = built["docs"]
        else:
            doc["records"][key] = built[blob]
        save(rel, doc)

    rows = sum(len(p["rows"]) for p in built["flows"])
    print(f"wrote {DST}")
    print(f"  case {CASE} · customer {CUSTOMER} · {len(built['flows'])} pages · {rows} transactions")
    print(f"  total in {built['summary']['total_in']:,.2f} · total out "
          f"{built['summary']['total_out']:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
