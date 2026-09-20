# Architecture

A single-process Streamlit app that fetches institution publications, enriches missing abstracts, classifies them against the UN SDGs, caches results in SQLite, and exports CSV/XLSX.

The flow is [[architecture#Query configuration]] -> [[architecture#Fetch execution]] -> [[enrichment#Fetch pipeline]] -> [[exports#Preview and charts]] -> [[exports#Export]]. All network work runs in a background worker thread (see [[fetch-jobs#Fetch job registry]]); the Streamlit UI only polls progress.

## App shell and entry

The app entry point is [[app.py#main]], which configures the page, resolves secrets and sources, renders the query controls, and branches between "idle", "fetch in progress", and "completed result" states.

- [[app.py#APP_VERSION]] — version shown in the page caption and used in no other runtime path.
- [[app.py#QuerySelection]] — frozen dataclass carrying the validated, non-secret identity of one fetch run (sources, institutions, types, model, dates, limit, and the resolved service configuration).
- [[app.py#build_query_params]] — builds the stable, serializable parameter map stored with a completed result so the UI can detect stale results when controls change ([[app.py#_result_payload_matches_params]]).

## Secrets and configuration

Secrets are loaded once per process from Streamlit secrets, falling back to local TOML files, and are never shown in the UI.

- [[app.py#_load_secrets]] — process-level secret cache (Streamlit secrets first, then `.streamlit/secrets.toml`, then `~/.streamlit/secrets.toml`).
- [[app.py#get_secret_text]] / [[app.py#get_secret_bool]] — dotted-name secret access with placeholder ("none", "null") normalization.
- [[app.py#resolve_user_agent]] — the OpenAlex contact User-Agent; [[app.py#has_contact_user_agent]] validates that it carries a real `mailto:` contact before any OpenAlex fetch is allowed.
- [[app.py#resolve_aurora_base_url]], [[app.py#resolve_semantic_scholar_key]], [[app.py#resolve_serpapi_key]], [[app.py#resolve_google_scholar_enabled]] — service configuration resolvers.
- [[app.py#resolve_dspace_sources]] and [[app.py#resolve_oai_sources]] — merge the tracked registries (`dspace_sources.toml`, `oai_sources.toml`) with optional secrets-based additions/overrides; tracked entries are validated by [[sources#Source configuration parsing]].

## Query configuration

The UI renders source, institution, type, model, and date controls and assembles a `QuerySelection`. Configuration errors that must block a fetch are computed by [[app.py#query_configuration_errors]].

- [[app.py#render_source_selector]] — multi-select of OpenAlex plus configured DSpace/OAI sources.
- [[app.py#render_institution_selector]] — OpenAlex institution search by name or direct ROR/OpenAlex ID entry; results feed the lineage toggle.
- [[app.py#render_configured_institution_selector]] — uses institution identifiers attached to the selected repository sources instead of a manual search.
- [[app.py#render_publication_type_selector]], [[app.py#render_model_selector]], [[app.py#render_advanced_options]] — type, model, and date/limit controls.

## Fetch execution

Clicking the run button starts a background job and reruns the app; the progress panel polls the job without rerunning the whole script.

- [[app.py#begin_fetch]] — initializes session state for a new fetch.
- [[app.py#render_active_fetch]] — a `@st.fragment(run_every=...)` poller that reads the job snapshot, renders progress, and installs the terminal result or notice.
- [[app.py#request_fetch_cancel]] — sets both the UI flag and the worker's cancel event for the active job.
- [[app.py#request_cancel_for_changed_params]] — automatically cancels a running fetch when the query controls no longer match the active run.
- [[app.py#execute_publication_fetch]] — the worker entry: resolves lineage for the selected institutions, then calls [[enrichment#Fetch pipeline]] and builds the completed result payload.
- [[app.py#invalidate_stale_result]] — drops a completed result whose stored parameters no longer match the current controls.
