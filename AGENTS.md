# AGENTS.md

## Repository Purpose

This repository contains a Streamlit application for exploring institution publications from OpenAlex, enriching missing abstracts, classifying them with Aurora SDG models, caching results locally in SQLite, and exporting CSV or XLSX outputs.

## Persistent Context

Use the global OKF memory bundle at `/Users/jmittelbach/.codex/memories/okf-memory/` as supplemental context for durable user preferences and long-lived working context.

Start with `/Users/jmittelbach/.codex/memories/okf-memory/index.md`, then read only the files relevant to the task:

- `profile.md` for stable user preferences and long-term context
- `workspace.md` for local environment assumptions
- `conventions.md` for maintenance rules
- `current-focus.md` only when active cross-repo priorities matter
- `log.md` only when recent memory changes are relevant

Treat the OKF bundle as user-maintained background context.
Do not treat it as a source of truth for current external facts.
Explicit user instructions and repository-specific guidance in this file take precedence over the global memory bundle.

## Working Rules

- Keep changes scoped to the Streamlit app, fetch and cache flow, export logic, or scholarly fallback behavior relevant to the task.
- Preserve the current dependency choices unless the task explicitly requires dependency work. In particular, keep `httpx<0.28.0` and `free-proxy==1.0.6` unless you verify a replacement path for `scholarly` proxy behavior.
- Treat `.streamlit/secrets.toml` as sensitive local configuration. Do not print, commit, rotate, or rewrite secrets unless the user explicitly asks.
- Use `.streamlit/secrets.sample.toml` as the template when documenting or extending secret configuration.
- Treat `cache.sqlite3` and its `-shm` and `-wal` companion files as runtime data. Do not delete, reset, or normalize them unless the task explicitly asks for cache maintenance.
- The git worktree may already be dirty because of cache churn. Do not revert unrelated cache changes.

## Setup And Run

- Create a virtual environment with `python3 -m venv .venv`
- Activate it with `source .venv/bin/activate`
- Install dependencies with `pip install -r requirements.txt`
- Run the app with `streamlit run app.py`

## Verification

- Prefer the smallest verification step that matches the change.
- For syntax and import-level checks, run `python3 -m py_compile app.py cache_db.py openalex_sdg.py test_scholarly_freeproxies.py`
- For the scholarly proxy path, use `python3 test_scholarly_freeproxies.py "test query" --max-results 3`
- For Streamlit UI or end-to-end data flow changes, start the app and exercise the affected path manually.
- In the final report, state clearly which checks you ran and which you did not run.

## Change Hygiene

- Keep README updates aligned with actual behavior when user-facing workflows, configuration, or outputs change.
- Avoid broad refactors unless they are required to complete the task safely.
- Do not commit generated outputs or environment-local artifacts unless the user explicitly asks for them.
