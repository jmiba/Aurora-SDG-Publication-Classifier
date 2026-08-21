"""Focused Streamlit state regression tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import app as app_module
from openalex_sdg import FetchStats


class AppStateTests(unittest.TestCase):
    def test_selecting_linked_dspace_source_uses_configured_institution(self) -> None:
        app = AppTest.from_file("app.py").run(timeout=30)
        app.multiselect[0].set_value(["openalex", "dspace:swps-share"])
        app.run(timeout=30)

        self.assertEqual(list(app.exception), [])
        self.assertTrue(
            any(
                "https://openalex.org/I36685595" in caption.value
                and "https://ror.org/0407f1r36" in caption.value
                for caption in app.caption
            )
        )
        self.assertFalse(
            any("Search by institution" in widget.label for widget in app.text_input)
        )
        self.assertIn("Artistic works", app.multiselect[1].options)

    def test_publication_type_selector_is_multichoice_and_expands_dspace_books(self) -> None:
        source = app_module.DSpaceSource(
            id="example",
            label="Example",
            base_url="https://repo.example/server/api",
            entity_types=("Book", "Artistic"),
        )
        with patch.object(
            app_module.st,
            "multiselect",
            return_value=["book", "book-chapter"],
        ) as multiselect:
            selected = app_module.render_publication_type_selector(False, [source])

        self.assertEqual(selected, ["book", "book-chapter"])
        options = multiselect.call_args.kwargs["options"]
        self.assertEqual(options, ["book", "book-chapter", "artistic-work"])
        self.assertEqual(multiselect.call_args.kwargs["default"], options)

    def test_configured_dspace_identifiers_drive_openalex_selection(self) -> None:
        source = app_module.DSpaceSource(
            id="example",
            label="Example",
            base_url="https://repo.example/server/api",
            openalex_institution_id="https://openalex.org/I123",
            ror_id="https://ror.org/012345678",
        )
        with patch.object(app_module.st, "checkbox", return_value=False):
            institution_ids, include_lineage = (
                app_module.render_configured_institution_selector([source])
            )

        self.assertEqual(institution_ids, ["https://openalex.org/I123"])
        self.assertFalse(include_lineage)

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
