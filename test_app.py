"""Focused Streamlit state regression tests."""

from __future__ import annotations

import io
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import load_workbook
from streamlit.testing.v1 import AppTest

import app as app_module
import fetch_jobs
import openalex_sdg
from openalex_sdg import FetchStats

APP_PATH = Path(__file__).resolve().parent / "app.py"


def make_selection(**overrides):
    values = {
        "include_openalex": True,
        "dspace_sources": (),
        "institution_id": "https://openalex.org/I123",
        "institution_ids": ("https://openalex.org/I123",),
        "include_lineage": False,
        "cached_lineage": (),
        "publication_types": ("article",),
        "model": "aurora-sdg-multi",
        "from_date": "2024-01-01",
        "to_date": "2024-12-31",
        "limit_rows": 10,
        "user_agent": "Classifier (mailto:researcher@university.edu)",
        "semantic_scholar_api_key": None,
        "google_scholar_enabled": False,
        "serpapi_api_key": None,
        "aurora_base_url": "https://aurora.example/classify",
    }
    values.update(overrides)
    return app_module.QuerySelection(**values)


class AppStateTests(unittest.TestCase):
    def test_network_edges_are_clipped_by_sphere_radius(self) -> None:
        clipped_start, clipped_end = app_module._edge_endpoints_outside_spheres(
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            start_radius=0.105,
            end_radius=0.03,
        )

        self.assertAlmostEqual(clipped_start[0], 0.105)
        self.assertAlmostEqual(clipped_end[0], 0.97)
        self.assertEqual(clipped_start[1:], (0.0, 0.0))
        self.assertEqual(clipped_end[1:], (0.0, 0.0))

        short_start, short_end = app_module._edge_endpoints_outside_spheres(
            (0.0, 0.0, 0.0),
            (0.1, 0.0, 0.0),
            start_radius=0.105,
            end_radius=0.105,
        )
        self.assertLess(short_start[0], short_end[0])

    def test_sphere_mesh_has_expected_vertices_faces_and_metadata(self) -> None:
        geometry = app_module._build_sphere_mesh(
            {"Example University": (0.0, 0.0, 0.0)},
            {"Example University": 0.1},
            {"Example University": 3},
            latitude_steps=2,
            longitude_steps=4,
        )

        self.assertEqual(len(geometry.x), 12)
        self.assertEqual(len(geometry.i), 16)
        self.assertAlmostEqual(max(geometry.x), 0.1)
        self.assertEqual(set(geometry.intensity), {3.0})
        self.assertEqual(
            set(geometry.hover_text),
            {"Example University (3 co-affiliations)"},
        )

    def test_default_sphere_mesh_uses_smooth_tessellation(self) -> None:
        geometry = app_module._build_sphere_mesh(
            {"Example University": (0.0, 0.0, 0.0)},
            {"Example University": 0.1},
            {"Example University": 3},
        )

        expected_vertices = (
            app_module.SPHERE_LATITUDE_STEPS + 1
        ) * app_module.SPHERE_LONGITUDE_STEPS
        expected_faces = (
            app_module.SPHERE_LATITUDE_STEPS
            * app_module.SPHERE_LONGITUDE_STEPS
            * 2
        )
        self.assertEqual(len(geometry.x), expected_vertices)
        self.assertEqual(len(geometry.i), expected_faces)
        self.assertGreaterEqual(app_module.SPHERE_LATITUDE_STEPS, 16)
        self.assertGreaterEqual(app_module.SPHERE_LONGITUDE_STEPS, 32)

    def test_network_edge_groups_rank_only_partner_connections(self) -> None:
        primary, secondary = app_module._network_edge_groups_for_origins(
            {
                ("Origin", "Partner A"): 10,
                ("Origin", "Partner B"): 8,
                ("Origin", "Partner C"): 6,
                ("Partner A", "Partner B"): 3,
                ("Partner A", "Partner C"): 5,
                ("Partner B", "Outside"): 20,
            },
            {"Origin"},
            min_secondary_weight=2,
        )

        self.assertEqual(
            primary,
            {
                ("Origin", "Partner A"): 10,
                ("Origin", "Partner B"): 8,
                ("Origin", "Partner C"): 6,
            },
        )
        self.assertEqual(
            secondary,
            [
                (("Partner A", "Partner C"), 5),
                (("Partner A", "Partner B"), 3),
            ],
        )

    def test_network_edge_groups_keep_links_from_all_selected_origins(self) -> None:
        primary, secondary = app_module._network_edge_groups_for_origins(
            {
                ("Origin A", "Partner A"): 5,
                ("Origin B", "Partner B"): 4,
                ("Partner A", "Partner B"): 3,
                ("Partner B", "Outside"): 8,
            },
            {"Origin A", "Origin B"},
            min_secondary_weight=2,
        )

        self.assertEqual(
            primary,
            {
                ("Origin A", "Partner A"): 5,
                ("Origin B", "Partner B"): 4,
            },
        )
        self.assertEqual(secondary, [(('Partner A', 'Partner B'), 3)])

    def test_network_label_filter_matches_partial_comma_separated_names(self) -> None:
        labels = [
            "Primary University (DE)",
            "University of Warsaw (PL)",
            "Paris Research Institute (FR)",
        ]

        self.assertEqual(
            app_module._network_labels_matching_query(labels, "warsaw, PARIS"),
            {"University of Warsaw (PL)", "Paris Research Institute (FR)"},
        )
        self.assertEqual(
            app_module._network_labels_matching_query(labels, ""),
            set(labels),
        )

    def test_network_label_style_adapts_to_light_and_dark_modes(self) -> None:
        light = app_module._network_label_style("light")
        dark = app_module._network_label_style("dark")

        self.assertEqual(light["font_color"], "#111827")
        self.assertIn("255,255,255", light["background_color"])
        self.assertEqual(dark["font_color"], "#f8fafc")
        self.assertIn("15,23,42", dark["background_color"])

    def test_model_selector_uses_selected_index_when_descriptions_collide(self) -> None:
        models = [("first", "Same description"), ("second", "Same description")]
        with (
            patch.object(app_module, "AURORA_MODELS", models),
            patch.object(app_module.st, "subheader"),
            patch.object(app_module.st, "selectbox", return_value=1) as selectbox,
        ):
            selected = app_module.render_model_selector()

        self.assertEqual(selected, "second")
        self.assertEqual(list(selectbox.call_args.kwargs["options"]), [0, 1])

    def test_query_params_and_fetch_state_helpers(self) -> None:
        selection = make_selection()
        params = app_module.build_query_params(selection)
        self.assertEqual(params["institutions"], ["https://openalex.org/I123"])
        self.assertEqual(params["types"], ["article"])

        state = {
            "fetch_in_progress": True,
            "fetch_params": {**params, "limit": 5},
            "fetch_cancel_requested": False,
        }
        self.assertTrue(app_module.request_cancel_for_changed_params(state, params))
        self.assertTrue(state["fetch_cancel_requested"])

        app_module.begin_fetch(state, params)
        self.assertEqual(state["fetch_params"], params)
        self.assertTrue(state["fetch_in_progress"])
        self.assertFalse(state["fetch_cancel_requested"])

    def test_background_fetch_receives_cancel_signal(self) -> None:
        started = threading.Event()

        def cancellable_fetch(job):
            started.set()
            job.publish_progress(1, 2, "Working")
            while not job.cancel_event.is_set():
                threading.Event().wait(0.01)
            raise app_module.FetchCancelled()

        job_id = app_module.start_fetch_job(cancellable_fetch)
        self.assertTrue(started.wait(timeout=1))
        state = {
            "fetch_in_progress": True,
            "fetch_cancel_requested": False,
            app_module.FETCH_JOB_SESSION_KEY: job_id,
        }
        app_module.request_fetch_cancel(state)
        job = app_module.get_fetch_job(job_id)
        self.assertIsNotNone(job)
        assert job is not None
        assert job.thread is not None
        job.thread.join(timeout=1)

        snapshot = job.snapshot()
        self.assertTrue(state["fetch_cancel_requested"])
        self.assertTrue(snapshot.done)
        self.assertIsInstance(snapshot.error, app_module.FetchCancelled)
        self.assertEqual(snapshot.progress_message, "Working")
        app_module.discard_fetch_job(job_id)

    def test_job_registry_survives_main_script_rerun(self) -> None:
        """Streamlit re-executes the main script in a fresh module namespace
        on every rerun. A job registry defined in app.py would be reset to an
        empty dict, orphaning a running fetch; the registry must therefore
        live in an imported module (fetch_jobs) that stays cached."""
        import sys
        import types

        import fetch_jobs

        job_id = fetch_jobs.start_fetch_job(lambda job: {"status": "ok"})
        try:
            self.assertIsNotNone(fetch_jobs.get_fetch_job(job_id))
            registry_before = fetch_jobs._FETCH_JOBS

            # Mirror Streamlit's rerun: a fresh module object, registered in
            # sys.modules, then the app source executed into it.
            sim_module = types.ModuleType("app_rerun_sim")
            sim_module.__file__ = str(APP_PATH)
            sys.modules["app_rerun_sim"] = sim_module
            try:
                with open(APP_PATH, "r", encoding="utf-8") as handle:
                    app_source = handle.read()
                exec(compile(app_source, str(APP_PATH), "exec"), sim_module.__dict__)

                # Sanity: the rerun simulation really re-executed app.py's
                # top level in a fresh namespace (its module-level state is
                # brand new while imported modules are shared).
                self.assertIsNot(sim_module.SDG_COLORS, app_module.SDG_COLORS)
                self.assertIs(fetch_jobs._FETCH_JOBS, registry_before)
                self.assertIsNotNone(sim_module.get_fetch_job(job_id))
            finally:
                sys.modules.pop("app_rerun_sim", None)
        finally:
            fetch_jobs.discard_fetch_job(job_id)

    def test_progress_reaches_completion_when_source_yields_less_than_limit(self) -> None:
        self.assertEqual(
            app_module.fetch_progress_fraction(3, 3, 10, "Completed"),
            1.0,
        )
        self.assertEqual(
            app_module.fetch_progress_fraction(1, None, 10, "Fetching"),
            0.1,
        )

    def test_focus_candidates_are_bounded_and_search_all_rows(self) -> None:
        rows = [
            {"title": f"Publication {index}", "authors": "Example Author"}
            for index in range(150)
        ]
        rows[120]["doi"] = "10.1234/special"

        self.assertEqual(
            app_module.focus_candidate_indices(
                rows,
                "",
                page_start=25,
                page_size=25,
            ),
            list(range(25, 50)),
        )
        self.assertEqual(
            app_module.focus_candidate_indices(
                rows,
                "special",
                page_start=0,
                page_size=25,
            ),
            [120],
        )
        self.assertEqual(
            len(
                app_module.focus_candidate_indices(
                    rows,
                    "publication",
                    page_start=0,
                    page_size=25,
                )
            ),
            100,
        )

    def test_stale_result_invalidation_is_independent_of_streamlit(self) -> None:
        selection = make_selection()
        params = app_module.build_query_params(selection)
        state = {
            "fetch_in_progress": False,
            app_module.RESULT_SESSION_KEY: {
                "schema_version": app_module.RESULT_SCHEMA_VERSION,
                "params": {**params, "limit": 5},
            },
            "preview_focus_index": 2,
        }

        payload, invalidated = app_module.invalidate_stale_result(state, params)

        self.assertIsNone(payload)
        self.assertTrue(invalidated)
        self.assertNotIn(app_module.RESULT_SESSION_KEY, state)
        self.assertNotIn("preview_focus_index", state)
        self.assertEqual(state["preview_page"], 1)

    def test_query_configuration_requires_real_contact_and_aurora_url(self) -> None:
        invalid = make_selection(
            user_agent="Fetcher (mailto:you@example.com)",
            aurora_base_url=None,
        )
        errors = app_module.query_configuration_errors(invalid)
        self.assertEqual(len(errors), 2)

        dspace_skip = make_selection(
            include_openalex=False,
            institution_id=None,
            institution_ids=(),
            model="skip",
            user_agent=app_module.DEFAULT_USER_AGENT,
            aurora_base_url=None,
        )
        self.assertEqual(app_module.query_configuration_errors(dspace_skip), [])

    def test_fetch_orchestration_passes_config_and_builds_payload(self) -> None:
        source = app_module.DSpaceSource(
            id="example",
            label="Example",
            base_url="https://repo.example/server/api",
        )
        selection = make_selection(dspace_sources=(source,))
        stats = FetchStats(
            total_expected=1,
            total_processed=1,
            openalex_abstract_missing=0,
            ss_abstract_retrieved=0,
            gs_abstract_retrieved=0,
            total_source_records=1,
        )
        rows = [{"title": "Publication"}]
        with patch.object(
            app_module,
            "fetch_publications_with_sdg",
            return_value=(rows, stats),
        ) as fetch:
            payload = app_module.execute_publication_fetch(
                selection,
                progress_callback=Mock(),
                cancel_check=Mock(return_value=False),
            )

        self.assertEqual(payload["rows"], rows)
        self.assertEqual(payload["params"], app_module.build_query_params(selection))
        self.assertEqual(
            fetch.call_args.kwargs["aurora_base_url"],
            "https://aurora.example/classify",
        )

    def test_fetch_orchestration_passes_oai_sources(self) -> None:
        source = app_module.OaiPmhSource(
            id="example-oai",
            label="Example OAI",
            base_url="https://repo.example/oai",
        )
        selection = make_selection(oai_sources=(source,))
        stats = FetchStats(
            total_expected=0,
            total_processed=0,
            openalex_abstract_missing=0,
            ss_abstract_retrieved=0,
            gs_abstract_retrieved=0,
        )
        with patch.object(
            app_module,
            "fetch_publications_with_sdg",
            return_value=([], stats),
        ) as fetch:
            app_module.execute_publication_fetch(
                selection,
                progress_callback=Mock(),
                cancel_check=Mock(return_value=False),
            )

        self.assertEqual(fetch.call_args.kwargs["oai_sources"], (source,))
        self.assertIn("example-oai", app_module.build_query_params(selection)["sources"])

    def test_fetch_summary_warns_once_when_semantic_scholar_rejects_key(self) -> None:
        stats = FetchStats(
            total_expected=2,
            total_processed=2,
            openalex_abstract_missing=2,
            ss_abstract_retrieved=0,
            gs_abstract_retrieved=2,
            total_source_records=2,
            sources_queried=["OpenAlex"],
            semantic_scholar_auth_error_status=401,
        )

        with (
            patch.object(app_module.st, "success"),
            patch.object(app_module.st, "warning") as warning,
        ):
            app_module.render_fetch_summary(
                stats,
                google_scholar_enabled=True,
                semantic_scholar_api_key_configured=True,
            )

        warning.assert_called_once()
        message = warning.call_args.args[0]
        self.assertIn("rejected the configured API key", message)
        self.assertIn("HTTP 401", message)
        self.assertNotIn("stale-key", message)

    def test_excel_export_round_trips_values_and_column_order(self) -> None:
        rows = [
            {
                "title": '=HYPERLINK("https://example.invalid")',
                "source_count": 3,
                "abstract": "Formula-looking metadata stays text",
            },
            {
                "title": "A & B <research>",
                "source_count": 2,
                "abstract": None,
                "ignored": "not exported",
            },
            {
                "title": "Über sustainability",
                "source_count": 1,
                "abstract": "First line\nSecond line",
            },
        ]

        exported = app_module.rows_to_excel_bytes(
            rows,
            ["title", "source_count", "abstract"],
        )
        workbook = load_workbook(io.BytesIO(exported), data_only=False)
        self.assertEqual(workbook.active["A2"].data_type, "s")
        dataframe = app_module.pd.read_excel(
            io.BytesIO(exported),
            engine="openpyxl",
            keep_default_na=False,
        )

        self.assertEqual(
            list(dataframe.columns),
            ["title", "source_count", "abstract"],
        )
        self.assertEqual(
            dataframe.to_dict(orient="records"),
            [
                {
                    "title": '=HYPERLINK("https://example.invalid")',
                    "source_count": 3,
                    "abstract": "Formula-looking metadata stays text",
                },
                {
                    "title": "A & B <research>",
                    "source_count": 2,
                    "abstract": "",
                },
                {
                    "title": "Über sustainability",
                    "source_count": 1,
                    "abstract": "First line\nSecond line",
                },
            ],
        )

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
        self.assertIn("Software", app.multiselect[1].options)

    def test_repository_sources_are_sorted_alphabetically(self) -> None:
        app = AppTest.from_file("app.py").run(timeout=30)

        source_options = app.multiselect[0].options
        self.assertEqual(source_options[0], "OpenAlex")
        self.assertEqual(source_options[1:], sorted(source_options[1:], key=str.casefold))

    def test_fetch_click_and_first_poll_survive_script_rerun(self) -> None:
        """Regression: Streamlit re-executes app.py in a fresh module
        namespace on every rerun. When the job registry lived in app.py, the
        first poll after clicking Fetch lost the running job and rendered
        'The background fetch state is unavailable.'"""
        release = threading.Event()
        stats = FetchStats(
            total_expected=0,
            total_processed=0,
            openalex_abstract_missing=0,
            ss_abstract_retrieved=0,
            gs_abstract_retrieved=0,
        )

        def blocking_fetch(**kwargs):
            release.wait(timeout=5)
            return [], stats

        with patch.object(
            openalex_sdg,
            "fetch_publications_with_sdg",
            side_effect=blocking_fetch,
        ):
            app = AppTest.from_file("app.py")
            app.run(timeout=60)
            # Use a configuration that needs no secrets (no OpenAlex contact
            # user agent, no Aurora URL) so the Fetch button is enabled in CI
            # as well as locally.
            app.multiselect[0].set_value(["dspace:swps-share"])
            model_box = next(
                box for box in app.selectbox if box.label == "Choose a model"
            )
            model_box.set_value(4)
            app.run(timeout=60)
            self.assertEqual(list(app.exception), [])
            app.button(key="main_fetch_button").click()
            app.run(timeout=60)

            rendered = [
                element.value
                for element in [*app.text, *app.error, *app.info]
            ]
            self.assertEqual(list(app.exception), [])
            self.assertFalse(
                any("background fetch state is unavailable" in value for value in rendered)
            )
            self.assertTrue(app.session_state["fetch_in_progress"])
            job_id = app.session_state["fetch_job_id"]
            self.assertTrue(job_id)
            job = fetch_jobs.get_fetch_job(job_id)
            self.assertIsNotNone(job)

            release.set()
            assert job is not None
            job.thread.join(timeout=5)
            app.run(timeout=60)

        self.assertEqual(list(app.exception), [])
        self.assertFalse(app.session_state["fetch_in_progress"])
        self.assertIn("fetch_result", app.session_state)
        self.assertNotIn("fetch_job_id", app.session_state)

    def test_selecting_viadrina_oai_source_uses_configured_institution(self) -> None:
        app = AppTest.from_file("app.py").run(timeout=30)
        app.multiselect[0].set_value(["openalex", "oai:viadrina-opus"])
        app.run(timeout=30)

        self.assertEqual(list(app.exception), [])
        self.assertTrue(
            any(
                "https://openalex.org/I254029264" in caption.value
                and "https://ror.org/02msan859" in caption.value
                for caption in app.caption
            )
        )
        self.assertFalse(
            any("Search by institution" in widget.label for widget in app.text_input)
        )
        self.assertIn("Preprints", app.multiselect[1].options)

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

    def test_google_scholar_status_reports_missing_optional_fallback(self) -> None:
        with (
            patch.object(
                app_module,
                "scholarly_fallback_available",
                return_value=False,
            ),
            patch.object(app_module.st, "warning") as warning,
        ):
            app_module.render_google_scholar_status(True, None)

        message = warning.call_args.args[0]
        self.assertIn("Lookups will be skipped", message)
        self.assertIn("requirements-scholarly.txt", message)

    def test_google_scholar_status_reports_installed_optional_fallback(self) -> None:
        with (
            patch.object(
                app_module,
                "scholarly_fallback_available",
                return_value=True,
            ),
            patch.object(app_module.st, "warning") as warning,
        ):
            app_module.render_google_scholar_status(True, None)

        self.assertIn("optional scholarly free-proxy fallback", warning.call_args.args[0])

    def test_institution_network_renders_zoom_consistent_spheres(self) -> None:
        rows = [
            {
                "publication_date": "2024-05-01",
                "institution_affiliations_json": (
                    '[{"id":"I1","name":"Primary University","country":"DE"},'
                    '{"id":"I2","name":"Partner University","country":"PL"}]'
                ),
            }
        ]

        with (
            patch.object(app_module.st, "text_input", return_value=""),
            patch.object(app_module.st, "plotly_chart") as plotly_chart,
        ):
            app_module.render_institution_network(
                rows,
                "2024-01-01",
                "2024-12-31",
                selected_institution_id="I1",
                theme_type="light",
            )

        figure = plotly_chart.call_args.args[0]
        edge_traces = figure.data[:-1]
        sphere_trace = figure.data[-1]
        self.assertEqual(sphere_trace.type, "mesh3d")
        self.assertEqual(sphere_trace.opacity, 1.0)
        self.assertFalse(sphere_trace.flatshading)
        annotations = list(figure.layout.scene.annotations)
        primary_annotation = next(
            annotation
            for annotation in annotations
            if annotation.text == "Primary University (DE)"
        )
        self.assertEqual(primary_annotation.font.color, "#111827")
        self.assertEqual(primary_annotation.font.weight, 300)
        self.assertEqual(primary_annotation.bgcolor, "rgba(255,255,255,0.90)")
        self.assertEqual(primary_annotation.bordercolor, "rgba(77,31,227,0.55)")
        self.assertFalse(primary_annotation.showarrow)
        self.assertEqual(primary_annotation.x, 0.0)
        self.assertEqual(primary_annotation.y, 0.0)
        self.assertEqual(primary_annotation.z, 0.0)
        self.assertTrue(all(trace.mode == "lines" for trace in edge_traces))
        single_edge = figure.data[0]
        self.assertEqual(single_edge.line.width, 4.0)
        self.assertEqual(single_edge.line.color, "rgba(140,140,140,0.82)")
        self.assertEqual(single_edge.hoverlabel.bgcolor, "#f2f2f2")
        self.assertTrue(
            all(
                trace.line.color.startswith("rgba(140,140,140,")
                for trace in edge_traces
            )
        )
        self.assertTrue(
            all(
                trace.hoverlabel.font.color == "#000000"
                for trace in edge_traces
            )
        )

    def test_secondary_network_edges_are_hidden_by_default(self) -> None:
        affiliations = (
            '[{"id":"I1","name":"Primary University","country":"DE"},'
            '{"id":"I2","name":"Partner A","country":"PL"},'
            '{"id":"I3","name":"Partner B","country":"FR"}]'
        )
        rows = [
            {
                "publication_date": f"2024-05-0{day}",
                "institution_affiliations_json": affiliations,
            }
            for day in (1, 2)
        ]

        with (
            patch.object(app_module.st, "toggle", return_value=False) as toggle,
            patch.object(app_module.st, "text_input", return_value=""),
            patch.object(app_module.st, "plotly_chart") as plotly_chart,
        ):
            app_module.render_institution_network(
                rows,
                "2024-01-01",
                "2024-12-31",
                selected_institution_id="I1",
                theme_type="light",
            )

        toggle.assert_called_once()
        figure = plotly_chart.call_args.args[0]
        self.assertEqual(len(figure.data[:-1]), 2)

        with (
            patch.object(app_module.st, "toggle", return_value=True),
            patch.object(app_module.st, "text_input", return_value=""),
            patch.object(app_module.st, "caption") as caption,
            patch.object(app_module.st, "plotly_chart") as plotly_chart,
        ):
            app_module.render_institution_network(
                rows,
                "2024-01-01",
                "2024-12-31",
                selected_institution_id="I1",
                theme_type="light",
            )

        figure = plotly_chart.call_args.args[0]
        self.assertEqual(len(figure.data[:-1]), 3)
        self.assertIn("1 of 1", caption.call_args.args[0])

    def test_institution_network_keeps_multiple_selected_origins(self) -> None:
        rows = [
            {
                "publication_date": "2024-05-01",
                "institution_affiliations_json": (
                    '[{"id":"I1","name":"Origin A","country":"DE"},'
                    '{"id":"I2","name":"Partner A","country":"PL"}]'
                ),
            },
            {
                "publication_date": "2024-05-02",
                "institution_affiliations_json": (
                    '[{"id":"I3","name":"Origin B","country":"FR"},'
                    '{"id":"I4","name":"Partner B","country":"ES"}]'
                ),
            },
        ]

        with (
            patch.object(app_module.st, "text_input", return_value=""),
            patch.object(app_module.st, "plotly_chart") as plotly_chart,
        ):
            app_module.render_institution_network(
                rows,
                "2024-01-01",
                "2024-12-31",
                selected_institution_id="I1",
                selected_institution_ids=("I1", "I3"),
                theme_type="light",
            )

        figure = plotly_chart.call_args.args[0]
        self.assertEqual(
            {annotation.text for annotation in figure.layout.scene.annotations},
            {"Origin A (DE)", "Partner A (PL)", "Origin B (FR)", "Partner B (ES)"},
        )
        self.assertEqual(len(figure.data[:-1]), 2)

    def test_institution_network_explains_missing_affiliation_pairs(self) -> None:
        rows = [
            {
                "publication_date": "2024-05-01",
                "source": "viadrina-opus",
                "institution_affiliations_json": "[]",
            }
        ]

        with (
            patch.object(app_module.st, "caption") as caption,
            patch.object(app_module.st, "info") as info,
        ):
            app_module.render_institution_network(
                rows,
                "2024-01-01",
                "2024-12-31",
                selected_institution_id=None,
            )

        self.assertIn("0 of 1", caption.call_args.args[0])
        self.assertIn("OAI-PMH Dublin Core", caption.call_args.args[0])
        self.assertIn("two structured institution identifiers", info.call_args.args[0])

    def test_result_charts_give_network_the_full_combined_result(self) -> None:
        all_rows = [
            {"title": "DSpace record", "source": "swps-share"},
            {"title": "OAI record", "source": "viadrina-opus"},
        ]
        focused_rows = [all_rows[0]]

        with (
            patch.object(app_module, "render_sdg_pie_chart"),
            patch.object(app_module, "render_institution_network") as network,
            patch.object(app_module, "render_author_oa_chart"),
            patch.object(app_module, "render_oa_ring_chart"),
            patch.object(app_module, "render_oa_status_chart"),
            patch.object(app_module, "render_publication_type_chart"),
        ):
            app_module.render_result_charts(
                all_rows,
                focused_rows,
                "DSpace record",
                from_date="2024-01-01",
                to_date="2024-12-31",
                institution_id="I1",
                institution_ids=("I1", "I2"),
            )

        self.assertIs(network.call_args.args[0], all_rows)
        self.assertEqual(
            network.call_args.kwargs["selected_institution_ids"],
            ("I1", "I2"),
        )

    def test_network_label_filter_keeps_match_and_selected_origin(self) -> None:
        affiliations = (
            '[{"id":"I1","name":"Primary University","country":"DE"},'
            '{"id":"I2","name":"Partner Alpha","country":"PL"},'
            '{"id":"I3","name":"Partner Beta","country":"FR"}]'
        )
        rows = [
            {
                "publication_date": "2024-05-01",
                "institution_affiliations_json": affiliations,
            }
        ]

        with (
            patch.object(app_module.st, "text_input", return_value="alpha"),
            patch.object(app_module.st, "caption"),
            patch.object(app_module.st, "plotly_chart") as plotly_chart,
        ):
            app_module.render_institution_network(
                rows,
                "2024-01-01",
                "2024-12-31",
                selected_institution_id="I1",
                theme_type="light",
            )

        figure = plotly_chart.call_args.args[0]
        visible_labels = {
            annotation.text for annotation in figure.layout.scene.annotations
        }
        self.assertEqual(
            visible_labels,
            {"Primary University (DE)", "Partner Alpha (PL)"},
        )
        self.assertEqual(len(figure.data[:-1]), 1)

    def test_network_label_filter_keeps_canvas_when_nothing_matches(self) -> None:
        rows = [
            {
                "publication_date": "2024-05-01",
                "institution_affiliations_json": (
                    '[{"id":"I1","name":"Primary University","country":"DE"},'
                    '{"id":"I2","name":"Partner University","country":"PL"}]'
                ),
            }
        ]

        with (
            patch.object(app_module.st, "text_input", return_value="no match"),
            patch.object(app_module.st, "plotly_chart") as plotly_chart,
        ):
            app_module.render_institution_network(
                rows,
                "2024-01-01",
                "2024-12-31",
                selected_institution_id="I1",
                theme_type="light",
            )

        plotly_chart.assert_called_once()
        figure = plotly_chart.call_args.args[0]
        self.assertEqual(figure.layout.height, 650)
        self.assertEqual(figure.layout.uirevision, "institution-network")
        self.assertEqual(figure.layout.scene.uirevision, "institution-network")
        self.assertEqual(len(figure.data), 1)
        self.assertEqual(figure.data[0].type, "mesh3d")
        self.assertEqual(
            {annotation.text for annotation in figure.layout.scene.annotations},
            {"Primary University (DE)"},
        )
        self.assertTrue(
            any(
                "No connected institution labels match" in annotation.text
                for annotation in figure.layout.annotations
            )
        )
        self.assertEqual(
            plotly_chart.call_args.kwargs["key"],
            "institution_network_chart",
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


class OutputFilenameTests(unittest.TestCase):
    def test_short_selections_keep_the_full_descriptive_name(self) -> None:
        name = app_module.build_output_filename(
            ["openalex"],
            "https://openalex.org/I123",
            ("article",),
            "aurora-sdg-multi",
            "2024-01-01",
            "2024-12-31",
            10,
        )

        self.assertEqual(
            name,
            "openalex_I123_article_aurora-sdg-multi_2024-01-01_to2024-12-31_n10.csv",
        )

    def test_missing_optional_segments_stay_omitted(self) -> None:
        name = app_module.build_output_filename(
            ["openalex"],
            None,
            (),
            "skip",
            "2024-01-01",
            None,
            None,
        )

        self.assertEqual(name, "openalex_all_all_no-sdg_2024-01-01.csv")

    def test_long_source_and_type_lists_stay_within_the_length_cap(self) -> None:
        name = app_module.build_output_filename(
            [
                "openalex",
                "swps-share",
                "mruni-cris",
                "aegean-hellanicus",
                "viadrina-opus",
                "paris8-hal",
                "nbu-eprints",
            ],
            "https://openalex.org/I36685595",
            (
                "article",
                "book",
                "book-chapter",
                "proceedings-article",
                "report",
                "dissertation",
                "dataset",
                "review",
                "preprint",
                "other",
            ),
            "aurora-sdg-multi",
            "2015-01-01",
            "2026-09-08",
            5000,
        )

        self.assertLessEqual(len(name), app_module.MAX_EXPORT_FILENAME_LENGTH)
        self.assertTrue(name.endswith(".csv"))
        # The trailing filter segments survive truncation.
        self.assertIn(
            "_aurora-sdg-multi_2015-01-01_to2026-09-08_n5000.csv", name
        )
        self.assertIn("I36685595", name)
        # Dropped ids are noted instead of silently vanishing.
        self.assertRegex(name, r"\+\d+")

    def test_extreme_lists_do_not_exceed_the_cap(self) -> None:
        name = app_module.build_output_filename(
            [f"source-{index}" for index in range(40)],
            "https://openalex.org/I42",
            tuple(f"type-{index}" for index in range(40)),
            "aurora-sdg-multi",
            "2000-01-01",
            "2026-12-31",
            1,
        )

        self.assertLessEqual(len(name), app_module.MAX_EXPORT_FILENAME_LENGTH)
        self.assertTrue(name.endswith(".csv"))
        self.assertFalse(name.startswith("_"))
        # The Excel twin is at most one character longer (.xlsx vs .csv).
        self.assertLessEqual(
            len(name.replace(".csv", ".xlsx")),
            app_module.MAX_EXPORT_FILENAME_LENGTH + 1,
        )


if __name__ == "__main__":
    unittest.main()
