"""Contract and integration checks for the optional local comparison."""

import csv
import io
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np
from openpyxl import load_workbook
from streamlit.testing.v1 import AppTest

import app
import openalex_local_sdg as local
import openalex_sdg
from test_app import make_selection


def result_with(scores):
    return local.validate_result({"identity": local.IDENTITY, "scores": scores})


class LocalContractTests(unittest.TestCase):
    def test_base_import_does_not_load_optional_ml_stack(self):
        check = subprocess.run(
            [sys.executable, "-c", "import openalex_sdg, sys; "
             "assert 'torch' not in sys.modules; "
             "assert 'sentence_transformers' not in sys.modules"],
            capture_output=True, text=True,
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    # @lat: [[openalex-local#Local OpenAlex comparison#Verification]]
    def test_rejects_incomplete_nonfinite_and_wrong_identity_results(self):
        for scores in ([0.1] * 16, [float("nan")] * 17, [1.1] * 17, [True] * 17):
            with self.assertRaises(ValueError):
                result_with(scores)
        with self.assertRaises(ValueError):
            local.validate_result({"identity": {}, "scores": [0.1] * 17})
        with self.assertRaises(ValueError):
            local.Head().score(np.zeros((1, 1024), np.float32))

    def test_thresholds_before_rounding_and_rebuilds_cached_membership(self):
        scores = [0.39999, 0.40001] + [0.1] * 15
        result = result_with(scores)
        self.assertEqual([item["code"] for item in result["sdgs"]], [2])
        result["status"] = "below_threshold"
        result["sdgs"] = []
        self.assertEqual(local.validate_result(result)["status"], "classified")
        self.assertEqual(result_with([0.1] * 17)["status"], "below_threshold")

    def test_input_and_identity(self):
        self.assertEqual(local.work_text("Title", "Abstract"), "Title: Title\n\nAbstract: Abstract")
        self.assertEqual(len(local.work_text("Title", "x" * 4000)), 2000)
        self.assertNotEqual(local.input_hash("Title", "a"), local.input_hash("Title", "b"))
        with patch.object(local, "CACHE_MODEL", "changed"):
            changed = local.input_hash("Title", "a")
        self.assertNotEqual(changed, local.input_hash("Title", "a"))

    def test_failed_model_load_only_attempted_once_per_fetch(self):
        classifier = local.LocalClassifier()
        with patch.object(local, "load_resources", side_effect=OSError("private detail")) as load:
            for _ in range(2):
                self.assertEqual(classifier.classify("Title", "", lambda: None),
                                 (None, "local_model_unavailable"))
        load.assert_called_once()

    def test_inference_receives_only_formatted_publication_text(self):
        encoder, head = Mock(), Mock()
        head.score.return_value = np.asarray([[0.1] * 17])
        with patch.object(local, "load_resources", return_value=(encoder, head)):
            result, note = local.LocalClassifier().classify("Title", "", lambda: None)
        self.assertEqual(result["status"], "below_threshold")
        self.assertIn("title_only", note)
        self.assertEqual(encoder.encode.call_args.args[0], ["Title: Title"])


class LocalIntegrationTests(unittest.TestCase):
    def run_worker(self, classifier, cached=None, cancel_event=None, tags_present=True):
        publication = {"publication_key": "doi:test", "title": "A title", "abstract": "An abstract",
                       "_openalex_aurora_sdgs": [{"id": "https://openalex.org/sdgs/3",
                                                  "display_name": "Health", "score": 0.8}]}
        if not tags_present:
            publication.pop("_openalex_aurora_sdgs")
        with (
            patch.object(openalex_sdg, "get_cached_work", return_value=None),
            patch.object(openalex_sdg, "get_cached_sdg_result", return_value=cached),
            patch.object(openalex_sdg, "upsert_work") as work,
            patch.object(openalex_sdg, "upsert_sdg_result") as save,
            patch.object(openalex_sdg, "classify_text_aurora") as aurora,
        ):
            row = openalex_sdg._enrich_and_classify_publication(
                publication, session_factory=Mock, model="openalex-local", user_agent="test",
                semantic_scholar_api_key=None, enable_google_scholar=False, serpapi_api_key=None,
                aurora_limiter=openalex_sdg._RateLimiter(0),
                cancel_event=cancel_event or threading.Event(), local_classifier=classifier,
            ).row
        aurora.assert_not_called()
        return row, work, save

    def test_local_primary_exports_all_scores_without_using_legacy_tags(self):
        result = result_with([0.1] * 17)
        row, work, save = self.run_worker(Mock(classify=Mock(return_value=(result, "venue_omitted"))))
        self.assertEqual(row["sdg_source"], "openalex_local")
        self.assertEqual(row["sdg_status"], "below_threshold")
        self.assertEqual(row["sdg_model"], "openalex-local")
        self.assertEqual(row["sdg_classifier_version"], local.CACHE_MODEL)
        self.assertEqual(app.aggregate_sdg_counts([row]), [])
        self.assertEqual(row["local_sdg_status"], "below_threshold")
        work.assert_called_once()
        self.assertEqual(save.call_args.kwargs["model"], local.CACHE_MODEL)
        exported = next(csv.DictReader(io.StringIO(app.rows_to_csv_bytes([row]).decode())))
        self.assertEqual(len(json.loads(exported["local_sdg_response"])["scores"]), 17)
        workbook = load_workbook(io.BytesIO(app.rows_to_excel_bytes([row])), read_only=True)
        headers = next(workbook.active.values)
        self.assertIn("local_sdg_classifier_version", headers)
        workbook.close()

    def test_valid_empty_cache_reused_but_corrupt_and_changed_inputs_recomputed(self):
        valid = result_with([0.1] * 17)
        cache = {"text_hash": local.input_hash("A title", "An abstract"),
                 "sdg_response": json.dumps(valid)}
        classifier = Mock(classify=Mock(return_value=(valid, "")))
        row, _, save = self.run_worker(classifier, cache)
        self.assertEqual(row["local_sdg_status"], "below_threshold")
        classifier.classify.assert_not_called()
        save.assert_not_called()
        for bad_cache in ({**cache, "sdg_response": "{}"}, {**cache, "text_hash": "stale"}):
            self.run_worker(classifier, bad_cache)
        self.assertEqual(classifier.classify.call_count, 2)

    def test_failure_is_not_cached_or_interpreted_as_empty(self):
        row, _, save = self.run_worker(Mock(classify=Mock(return_value=(None, "local_model_unavailable"))), tags_present=False)
        self.assertEqual(row["local_sdg_status"], "failed")
        self.assertEqual(row["sdg_status"], "failed")
        self.assertEqual(row["local_sdg_response"], "")
        save.assert_not_called()

    def test_primary_chart_uses_local_assignments_without_aurora_fallback(self):
        scores = [0.1] * 17
        scores[6] = 0.75
        row, _, _ = self.run_worker(Mock(classify=Mock(return_value=(result_with(scores), ""))), tags_present=False)
        self.assertEqual(app.aggregate_sdg_counts([row])[0][0], "7")
        self.assertEqual(json.loads(row["sdg_response"])["scores"], scores)

    def test_cancellation_does_not_write_result(self):
        event = threading.Event()
        def classify(*args):
            event.set()
            return result_with([0.1] * 17), ""
        with self.assertRaises(openalex_sdg.FetchCancelled):
            self.run_worker(Mock(classify=classify), cancel_event=event)

    def test_selection_preflight_and_streamlit_missing_dependency_notice(self):
        selection = make_selection(model="openalex-local")
        self.assertEqual(app.build_query_params(selection)["local_sdg_version"], local.CACHE_MODEL)
        with patch.object(local, "dependencies_available", return_value=False):
            self.assertIn("requirements-openalex-local", app.query_configuration_errors(selection)[0])
            ui = AppTest.from_file("app.py").run(timeout=30)
            selector = next(box for box in ui.selectbox if box.label == "Choose SDG processing")
            index = next(i for i, (name, _) in enumerate(openalex_sdg.AURORA_MODELS)
                         if name == "openalex-local")
            self.assertEqual(selector.value, index)
            selector.set_value(index).run(timeout=30)
            self.assertFalse(ui.exception)
            self.assertTrue(any("requirements-openalex-local" in info.value for info in ui.info))
        with patch.object(local, "dependencies_available", return_value=True):
            self.assertEqual(app.query_configuration_errors(make_selection(model="openalex-local", aurora_base_url=None)), [])


if __name__ == "__main__":
    unittest.main()
