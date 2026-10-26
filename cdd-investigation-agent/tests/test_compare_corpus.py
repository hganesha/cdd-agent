"""Check blind inputs and fail-closed scoring for the corpus benchmark."""
from __future__ import annotations

import copy
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
from compare_corpus import (connector_page, gold_expectations, page_lookup, reference_run,
                            score_answer)  # noqa: E402
from export_blind_case import export  # noqa: E402
from cdd_agent.orchestrator import Orchestrator  # noqa: E402
from cdd_agent.skills import sk08_media  # noqa: E402


class CorpusComparisonTests(unittest.TestCase):
    def test_blind_export_excludes_gold_and_can_score_exact_quotes(self):
        gold_path = ROOT / "evals" / "golden" / "CASE-700001" / "gold.json"
        import json
        gold = json.loads(gold_path.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "case"
            export("CASE-700001", workspace)
            self.assertFalse((workspace / "gold.json").exists())
            self.assertFalse((workspace / "manifest.json").exists())
            anchor = gold["evidence_anchors"][0]
            source = json.loads((workspace / "pages" / f"{anchor['doc_id']}.json").read_text())
            pages = {(anchor["doc_id"], 1): source["pages"][0]["text"]}
            answer = gold_expectations(gold)
            answer["claims"] = [{"doc_id": anchor["doc_id"], "page": anchor["page"],
                                  "quote": anchor["quote"]}]
            score = score_answer(answer, gold, pages, require_pi_schema=False)
            self.assertEqual(score["fields_correct"], score["fields_total"])
            self.assertEqual(score["quotes_exact"], 1)

    def test_malformed_claims_and_codes_fail_closed(self):
        import json
        gold = json.loads((ROOT / "evals" / "golden" / "CASE-700001" / "gold.json").read_text())
        answer = copy.deepcopy(gold_expectations(gold))
        answer["risk_band"] = []
        answer["required_gates"] = [{}]
        answer["claims"] = [{"doc_id": [], "page": {}, "quote": "invented"}]
        score = score_answer(answer, gold, {}, require_pi_schema=False)
        self.assertFalse(score["checks"]["risk_band"])
        self.assertFalse(score["checks"]["required_gates"])
        self.assertFalse(score["recommendation_policy_admissible"])
        self.assertEqual(score["quotes_exact"], 0)

    def test_media_uses_bundle_document_and_exact_source_spans(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "case"
            export("CASE-700141", workspace)
            ctx = Orchestrator(workspace, pathlib.Path(tmp) / "run").ctx
            result = sk08_media(ctx, [{"name": "Cato Simvale 700141", "role": "CUSTOMER"}])
            self.assertEqual(len(result.outputs["events"]), 1)
            self.assertEqual(len(result.outputs["exclusions"]), 1)
            self.assertEqual(result.outputs["events"][0]["allegation_stage"], "FINAL_NOTICE")
            for record in ctx.ledger.records():
                if record["record_type"] != "EXTRACTION":
                    continue
                loc = record["locator"]
                import json
                doc = json.loads((workspace / "pages" / f"{loc['doc_id']}.json").read_text())
                source = doc["pages"][loc["page"] - 1]["text"]
                self.assertEqual(source[slice(*loc["char_span"])], record["verbatim_quote"])

    def test_absent_media_source_does_not_invent_an_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "case"
            export("CASE-700001", workspace)
            ctx = Orchestrator(workspace, pathlib.Path(tmp) / "media_run").ctx
            media = sk08_media(ctx, [{"name": "Bela Simvale Trading 700001 Ltd"}])
            self.assertEqual(media.outputs["events"], [])
            self.assertEqual(media.outputs["exclusions"], [])
            self.assertEqual(media.reason_code, "MEDIA_SOURCE_UNAVAILABLE")
            self.assertFalse(any(f["type"] == "ADVERSE_MEDIA" for f in ctx.findings))

            connector = connector_page(workspace, "CASE-700001")
            pages = page_lookup(workspace, connector)
            result = reference_run(workspace, pathlib.Path(tmp), pages)
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["answer"]["case_id"], "CASE-700001")
            self.assertEqual(result["answer"]["media_status"], "NONE")

    def test_blind_reference_scores_representative_cases_across_all_segments(self):
        import json
        for case_id in ("CASE-700001", "CASE-700041", "CASE-700101", "CASE-700141",
                        "CASE-700171", "CASE-700201", "CASE-700241"):
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as tmp:
                workspace = pathlib.Path(tmp) / "case"
                export(case_id, workspace)
                connector = connector_page(workspace, case_id)
                pages = page_lookup(workspace, connector)
                result = reference_run(workspace, pathlib.Path(tmp), pages)
                self.assertEqual(result["status"], "OK", result.get("error"))
                gold = json.loads((ROOT / "evals" / "golden" / case_id / "gold.json").read_text())
                score = score_answer(result["answer"], gold, pages, require_pi_schema=True)
                self.assertTrue(score["schema_valid"], score["schema_errors"])
                self.assertEqual(score["fields_correct"], score["fields_total"])
                self.assertEqual(score["quotes_exact"], score["claims"])


if __name__ == "__main__":
    unittest.main()
