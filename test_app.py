"""Focused Streamlit state regression tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import app as app_module
from openalex_sdg import FetchStats


class AppStateTests(unittest.TestCase):
    def test_institution_network_renders_nodes_above_edges_with_black_labels(self) -> None:
        rows = [
            {
                "publication_date": "2024-05-01",
                "institution_affiliations_json": (
                    '[{"id":"I1","name":"Primary University","country":"DE"},'
                    '{"id":"I2","name":"Partner University","country":"PL"}]'
                ),
            }
        ]

        with patch.object(app_module.st, "plotly_chart") as plotly_chart:
            app_module.render_institution_network(
                rows,
                "2024-01-01",
                "2024-12-31",
                selected_institution_id="I1",
            )

        figure = plotly_chart.call_args.args[0]
        node_trace = figure.data[-1]
        self.assertEqual(node_trace.mode, "markers+text")
        self.assertEqual(node_trace.marker.opacity, 1.0)
        self.assertEqual(node_trace.marker.line.color, "#ffffff")
        self.assertEqual(node_trace.textfont.color, "#000000")
        self.assertTrue(all(trace.mode == "lines" for trace in figure.data[:-1]))
        self.assertTrue(
            all(
                trace.line.color.startswith("rgba(130,130,130,")
                for trace in figure.data[:-1]
            )
        )
        self.assertTrue(
            all(
                trace.hoverlabel.font.color == "#000000"
                for trace in figure.data[:-1]
            )
        )

    def test_result_payload_requires_current_schema_and_matching_params(self) -> None:
        params = {"sources": ["openalex"], "from": "2024-01-01"}
        current = {
            "schema_version": app_module.RESULT_SCHEMA_VERSION,
            "params": params,
        }

        self.assertTrue(app_module._result_payload_matches_params(current, params))
        self.assertFalse(
            app_module._result_payload_matches_params({"params": params}, params)
        )

    def test_completed_result_is_invalidated_when_query_controls_differ(self) -> None:
        app = AppTest.from_file("app.py")
        app.session_state["selected_institution_id"] = "https://openalex.org/I1"
        app.session_state["fetch_result"] = {
            "csv_bytes": b"title,publication_date\nOld result,2023-01-01\n",
            "rows": [
                {
                    "title": "Old result",
                    "publication_date": "2023-01-01",
                }
            ],
            "stats": FetchStats(
                total_expected=1,
                total_processed=1,
                openalex_abstract_missing=0,
                ss_abstract_retrieved=0,
                gs_abstract_retrieved=0,
                total_source_records=1,
                sources_queried=["OpenAlex"],
            ),
            "filename": "old.csv",
            "params": {
                "sources": ["openalex"],
                "institution": "https://openalex.org/I1",
                "type": "article",
                "model": "skip",
                "from": "2023-01-01",
                "to": "2023-12-31",
                "limit": None,
            },
        }

        app.run(timeout=30)

        self.assertEqual(list(app.exception), [])
        self.assertEqual(list(app.success), [])
        self.assertNotIn("fetch_result", app.session_state)
        self.assertTrue(
            any("Query settings changed" in message.value for message in app.info)
        )


if __name__ == "__main__":
    unittest.main()
