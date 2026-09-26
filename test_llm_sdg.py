"""Tests for the independent hosted LLM classification contract."""

from __future__ import annotations

import csv
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch

import app
import llm_sdg
import openalex_sdg


class LlmDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = llm_sdg.LlmConfig("https://provider.example/v1", "secret")

    def test_valid_no_sdg_and_independent_assignments(self) -> None:
        self.assertEqual(llm_sdg.validate_decision({"status": "no_sdg", "sdgs": []})["sdgs"], [])
        result = llm_sdg.validate_decision(
            {"status": "classified", "sdgs": [{"code": 3, "evidence": "drug shortages"}]},
            "Preventing drug shortages", "",
        )
        self.assertEqual(llm_sdg.format_decision(result), "SDG 3 (Good Health and Well-being)")
        self.assertEqual(llm_sdg.evidence_text(result), "SDG 3: drug shortages")
        with self.assertRaises(ValueError):
            llm_sdg.validate_decision(
                {"status": "classified", "sdgs": [{"code": 3, "evidence": "invented health outcome"}]},
                "Preventing drug shortages", "",
            )

    def test_cache_identity_changes_with_model_and_text(self) -> None:
        other = llm_sdg.LlmConfig(self.config.base_url, self.config.api_key, "other-model")
        self.assertNotEqual(self.config.cache_model, other.cache_model)
        self.assertNotEqual(
            llm_sdg.input_hash("Title", "Abstract", self.config),
            llm_sdg.input_hash("Title", "Other abstract", self.config),
        )
        thinking = llm_sdg.LlmConfig(self.config.base_url, self.config.api_key,
                                     enable_thinking=True)
        self.assertNotEqual(self.config.cache_model, thinking.cache_model)

    def test_result_summary_distinguishes_no_sdg_from_failed(self) -> None:
        self.assertEqual(
            app.llm_result_counts([
                {"llm_status": "no_sdg"},
                {"llm_status": "classified"},
                {"llm_status": "failed", "llm_note": "llm_http_400"},
            ]),
            {"classified": 1, "no_sdg": 1, "manual_review": 0, "failed": 1},
        )

    def test_provider_response_and_failure_are_separate(self) -> None:
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {
            "choices": [{"message": {"content": json.dumps({"status": "no_sdg", "sdgs": []})}}]
        }
        session.post.return_value.raise_for_status.return_value = None
        decision, note = llm_sdg.classify_publication("A software release", "", session, self.config)
        self.assertEqual((decision, note), ({"status": "no_sdg", "sdgs": []}, ""))
        payload = session.post.call_args.kwargs["json"]
        self.assertNotIn("aurora", json.dumps(payload).lower())
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        other_config = llm_sdg.LlmConfig(self.config.base_url, self.config.api_key, "other-model")
        llm_sdg.classify_publication("Title", "", session, other_config)
        self.assertNotIn("chat_template_kwargs", session.post.call_args.kwargs["json"])
        session.post.return_value.json.return_value = {"choices": [{"message": {"content": "bad"}}]}
        self.assertEqual(llm_sdg.classify_publication("Title", "", session, self.config)[1], "llm_invalid_response")

    def test_worker_rechecks_empty_openalex_and_caches_llm_comparison(self) -> None:
        publication = {
            "publication_key": "openalex:W-LLM", "source": "openalex",
            "source_record_id": "W-LLM", "title": "A software release", "abstract": "",
            "_openalex_aurora_sdgs": [],
        }
        no_sdg = {"status": "no_sdg", "sdgs": []}
        with (
            patch.object(openalex_sdg, "get_cached_work", return_value=None),
            patch.object(openalex_sdg, "get_cached_sdg_result", return_value=None),
            patch.object(openalex_sdg, "classify_text_aurora", return_value=({"predictions": []}, "")) as aurora,
            patch.object(openalex_sdg, "classify_publication", return_value=(no_sdg, "")),
            patch.object(openalex_sdg, "upsert_work"),
            patch.object(openalex_sdg, "upsert_sdg_result") as cached,
        ):
            result = openalex_sdg._enrich_and_classify_publication(
                publication, session_factory=Mock, model="llm-independent",
                user_agent="test", semantic_scholar_api_key=None,
                enable_google_scholar=False, serpapi_api_key=None,
                aurora_limiter=Mock(), cancel_event=threading.Event(),
                llm_config=self.config,
            )
        aurora.assert_called_once()
        self.assertEqual(result.row["sdg_status"], "no_sdg")
        self.assertEqual(result.row["sdg_source"], "aurora_recheck_openalex_empty")
        self.assertEqual(result.row["openalex_aurora_response"], "[]")
        exported = next(csv.DictReader(io.StringIO(app.rows_to_csv_bytes([result.row]).decode())))
        self.assertEqual(exported["openalex_aurora_response"], "[]")
        self.assertEqual(exported["openalex_aurora_status"], "empty")
        self.assertEqual(exported["sdg_source"], "aurora_recheck_openalex_empty")
        self.assertEqual(result.row["llm_status"], "no_sdg")
        self.assertEqual(result.row["llm_provider_model"], self.config.model)
        self.assertEqual(result.row["llm_prompt_version"], llm_sdg.PROMPT_VERSION)
        self.assertEqual(result.row["sdg_formatted"], "")
        self.assertEqual(cached.call_args.kwargs["model"], self.config.cache_model)
        self.assertEqual(app.aggregate_sdg_counts([result.row]), [])
        self.assertIn(b"no_sdg", app.rows_to_csv_bytes([result.row]))

    def test_llm_assignment_does_not_change_primary_chart(self) -> None:
        publication = {
            "publication_key": "openalex:W-COMPARE", "source": "openalex",
            "source_record_id": "W-COMPARE", "title": "Preventing drug shortages", "abstract": "",
            "_openalex_aurora_sdgs": [
                {"id": "https://openalex.org/sdgs/9", "display_name": "Industry", "score": 0.75},
            ],
        }
        decision = {"status": "classified", "sdgs": [{"code": 3, "evidence": "drug shortages"}]}
        with (
            patch.object(openalex_sdg, "get_cached_work", return_value=None),
            patch.object(openalex_sdg, "get_cached_sdg_result", return_value=None),
            patch.object(openalex_sdg, "classify_text_aurora") as aurora,
            patch.object(openalex_sdg, "classify_publication", return_value=(decision, "")),
            patch.object(openalex_sdg, "upsert_work"),
            patch.object(openalex_sdg, "upsert_sdg_result"),
        ):
            result = openalex_sdg._enrich_and_classify_publication(
                publication, session_factory=Mock, model="llm-independent",
                user_agent="test", semantic_scholar_api_key=None,
                enable_google_scholar=False, serpapi_api_key=None,
                aurora_limiter=Mock(), cancel_event=threading.Event(),
                llm_config=self.config,
            )
        aurora.assert_not_called()
        self.assertEqual(result.row["sdg_formatted"], "75% SDG 9 (Industry)")
        self.assertEqual(result.row["openalex_aurora_status"], "classified")
        self.assertEqual(result.row["llm_sdgs"], "SDG 3 (Good Health and Well-being)")
        self.assertEqual(app.aggregate_sdg_counts([result.row])[0][0], "9")

    def test_cached_no_sdg_is_reused_without_provider_call(self) -> None:
        publication = {
            "publication_key": "openalex:W-LLM", "source": "openalex",
            "source_record_id": "W-LLM", "title": "A software release", "abstract": "",
            "_openalex_aurora_sdgs": [
                {"id": "https://openalex.org/sdgs/9", "display_name": "Industry", "score": 0.75},
            ],
        }
        cached = {
            "text_hash": llm_sdg.input_hash(publication["title"], "", self.config),
            "sdg_response": json.dumps({"status": "no_sdg", "sdgs": []}),
            "sdg_formatted": "", "sdg_note": "",
        }
        with (
            patch.object(openalex_sdg, "get_cached_work", return_value=None),
            patch.object(openalex_sdg, "get_cached_sdg_result", return_value=cached),
            patch.object(openalex_sdg, "classify_publication") as classify,
            patch.object(openalex_sdg, "upsert_work"),
        ):
            result = openalex_sdg._enrich_and_classify_publication(
                publication, session_factory=Mock, model="llm-independent",
                user_agent="test", semantic_scholar_api_key=None,
                enable_google_scholar=False, serpapi_api_key=None,
                aurora_limiter=Mock(), cancel_event=threading.Event(),
                llm_config=self.config,
            )
        classify.assert_not_called()
        self.assertEqual(result.row["llm_status"], "no_sdg")


if __name__ == "__main__":
    unittest.main()
