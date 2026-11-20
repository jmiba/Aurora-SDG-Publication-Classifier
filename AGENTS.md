# AGENTS.md

## Repository Purpose

This repository contains a Streamlit application for exploring institution publications from OpenAlex, enriching missing abstracts, classifying them with the local OpenAlex SDG pipeline by default, caching results locally in SQLite, and exporting CSV or XLSX outputs.

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

%% lat:begin %%
# Before starting work

- Run `lat search` to find sections relevant to your task. Read them to understand the design intent before writing code.
- Run `lat expand` on user prompts to expand any `[[refs]]` — this resolves section names to file locations and provides context.

# Post-task checklist (REQUIRED — do not skip)

After EVERY task, before responding to the user:

- [ ] Update `lat.md/` if you added or changed any functionality, architecture, tests, or behavior
- [ ] Run `lat check` — all wiki links and code refs must pass
- [ ] Do not skip these steps. Do not consider your task done until both are complete.

---

# What is lat.md?

This project uses [lat.md](https://www.npmjs.com/package/lat.md) to maintain a structured knowledge graph of its architecture, design decisions, and test specs in the `lat.md/` directory. It is a set of cross-linked markdown files that describe **what** this project does and **why** — the domain concepts, key design decisions, business logic, and test specifications. Use it to ground your work in the actual architecture rather than guessing.

# Commands

```bash
lat locate "Section Name"      # find a section by name (exact, fuzzy)
lat refs "file#Section"        # find what references a section
lat search "natural language"  # semantic search across all sections
lat expand "user prompt text"  # expand [[refs]] to resolved locations
lat check                      # validate all links and code refs
```

Run `lat --help` when in doubt about available commands or options.

If `lat search` fails because no API key is configured, explain to the user that semantic search requires a key provided via `LAT_LLM_KEY` (direct value), `LAT_LLM_KEY_FILE` (path to key file), or `LAT_LLM_KEY_HELPER` (command that prints the key). Supported key prefixes: `sk-...` (OpenAI) or `vck_...` (Vercel). If the user doesn't want to set it up, use `lat locate` for direct lookups instead.

# Syntax primer

- **Section ids**: `lat.md/path/to/file#Heading#SubHeading` — full form uses project-root-relative path (e.g. `lat.md/tests/search#RAG Replay Tests`). Short form uses bare file name when unique (e.g. `search#RAG Replay Tests`, `cli#search#Indexing`).
- **Wiki links**: `[[target]]` or `[[target|alias]]` — cross-references between sections. Can also reference source code: `[[src/foo.ts#myFunction]]`.
- **Source code links**: Wiki links in `lat.md/` files can reference functions, classes, constants, and methods in TypeScript/JavaScript/Python/Rust/Go/C files. Use the full path: `[[src/config.ts#getConfigDir]]`, `[[src/server.ts#App#listen]]` (class method), `[[lib/utils.py#parse_args]]`, `[[src/lib.rs#Greeter#greet]]` (Rust impl method), `[[src/app.go#Greeter#Greet]]` (Go method), `[[src/app.h#Greeter]]` (C struct). `lat check` validates these exist.
- **Code refs**: `// @lat: [[section-id]]` (JS/TS/Rust/Go/C) or `# @lat: [[section-id]]` (Python) — ties source code to concepts

# Test specs

Key tests can be described as sections in `lat.md/` files (e.g. `tests.md`). Add frontmatter to require that every leaf section is referenced by a `// @lat:` or `# @lat:` comment in test code:

```markdown
---
lat:
  require-code-mention: true
---
# Tests

Authentication and authorization test specifications.

## User login

Verify credential validation and error handling for the login endpoint.

### Rejects expired tokens
Tokens past their expiry timestamp are rejected with 401, even if otherwise valid.

### Handles missing password
Login request without a password field returns 400 with a descriptive error.
```

Every section MUST have a description — at least one sentence explaining what the test verifies and why. Empty sections with just a heading are not acceptable. (This is a specific case of the general leading paragraph rule below.)

Each test in code should reference its spec with exactly one comment placed next to the relevant test — not at the top of the file:

```python
# @lat: [[tests#User login#Rejects expired tokens]]
def test_rejects_expired_tokens():
    ...

# @lat: [[tests#User login#Handles missing password]]
def test_handles_missing_password():
    ...
```

Do not duplicate refs. One `@lat:` comment per spec section, placed at the test that covers it. `lat check` will flag any spec section not covered by a code reference, and any code reference pointing to a nonexistent section.

# Section structure

Every section in `lat.md/` **must** have a leading paragraph — at least one sentence immediately after the heading, before any child headings or other block content. The first paragraph must be ≤250 characters (excluding `[[wiki link]]` content). This paragraph serves as the section's overview and is used in search results, command output, and RAG context — keeping it concise guarantees the section's essence is always captured.

```markdown
# Good Section

Brief overview of what this section documents and why it matters.

More detail can go in subsequent paragraphs, code blocks, or lists.

## Child heading

Details about this child topic.
```

```markdown
# Bad Section

## Child heading

Details about this child topic.
```

The second example is invalid because `Bad Section` has no leading paragraph. `lat check` validates this rule and reports errors for missing or overly long leading paragraphs.
%% lat:end %%
