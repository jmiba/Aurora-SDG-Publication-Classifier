"""Simple SQLite-backed cache for OpenAlex works and SDG classifications."""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

DB_PATH = Path("cache.sqlite3")
_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    """Return a global SQLite connection, initializing schema if needed."""
    global _CONN
    if _CONN is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL;")
        _CONN.execute("PRAGMA synchronous=FULL;")
        _init_schema(_CONN)
    return _CONN


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create tables/indexes the first time the cache database is opened."""
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS works (
                openalex_id TEXT PRIMARY KEY,
                title TEXT,
                publication_date TEXT,
                doi TEXT,
                type TEXT,
                language TEXT,
                is_oa INTEGER,
                oa_status TEXT,
                authors TEXT,
                institutions TEXT,
                institution_affiliations_json TEXT,
                abstract TEXT,
                raw_json TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sdg_results (
                openalex_id TEXT NOT NULL,
                model TEXT NOT NULL,
                sdg_response TEXT,
                sdg_formatted TEXT,
                sdg_note TEXT,
                classified_at TEXT,
                PRIMARY KEY (openalex_id, model)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sdg_results_model
            ON sdg_results(model)
            """
        )
        # Add new column if missing
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(works)")}
        if "institution_affiliations_json" not in cols:
            conn.execute(
                "ALTER TABLE works ADD COLUMN institution_affiliations_json TEXT"
            )


def get_cached_work(openalex_id: str) -> Optional[Dict[str, Any]]:
    """Return cached work metadata for the given OpenAlex ID, if present."""
    conn = _get_conn()
    cur = conn.execute(
        "SELECT * FROM works WHERE openalex_id = ?", (openalex_id.strip(),)
    )
    row = cur.fetchone()
    return dict(row) if row else None


def upsert_work(row: Dict[str, Any], raw_record: Optional[Dict[str, Any]] = None) -> None:
    """Create/update a work row with minimal locking to avoid duplicate fetches."""
    conn = _get_conn()
    payload = {
        "openalex_id": row.get("openalex_id"),
        "title": row.get("title"),
        "publication_date": row.get("publication_date"),
        "doi": row.get("doi"),
        "type": row.get("type"),
        "language": row.get("language"),
        "is_oa": int(row.get("is_oa")) if row.get("is_oa") is not None else None,
        "oa_status": row.get("oa_status"),
        "authors": row.get("authors"),
        "institutions": row.get("institutions"),
        "institution_affiliations_json": row.get("institution_affiliations_json"),
        "abstract": row.get("abstract"),
        "raw_json": json.dumps(raw_record, ensure_ascii=False) if raw_record else None,
        "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    placeholders = ", ".join(["?"] * len(payload))
    columns = ", ".join(payload.keys())
    update_clause = ", ".join(f"{col}=excluded.{col}" for col in payload.keys())
    values = list(payload.values())
    with _LOCK:
        conn.execute(
            f"""
            INSERT INTO works ({columns})
            VALUES ({placeholders})
            ON CONFLICT(openalex_id) DO UPDATE SET
            {update_clause}
            """,
            values,
        )
        conn.commit()


def get_cached_sdg_result(openalex_id: str, model: str) -> Optional[Dict[str, Any]]:
    """Return cached SDG classification data for a work/model pair."""
    conn = _get_conn()
    cur = conn.execute(
        """
        SELECT sdg_response, sdg_formatted, sdg_note, classified_at
        FROM sdg_results
        WHERE openalex_id = ? AND model = ?
        """,
        (openalex_id.strip(), model.strip()),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def upsert_sdg_result(
    openalex_id: str,
    model: str,
    sdg_response: Optional[Dict[str, Any]],
    sdg_formatted: str,
    sdg_note: str,
) -> None:
    """Persist the SDG API payload so later runs can reuse it."""
    conn = _get_conn()
    payload = {
        "openalex_id": openalex_id.strip(),
        "model": model.strip(),
        "sdg_response": json.dumps(sdg_response, ensure_ascii=False)
        if sdg_response is not None
        else None,
        "sdg_formatted": sdg_formatted,
        "sdg_note": sdg_note,
        "classified_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    with _LOCK:
        conn.execute(
            """
            INSERT INTO sdg_results (openalex_id, model, sdg_response, sdg_formatted, sdg_note, classified_at)
            VALUES (:openalex_id, :model, :sdg_response, :sdg_formatted, :sdg_note, :classified_at)
            ON CONFLICT(openalex_id, model) DO UPDATE SET
                sdg_response = excluded.sdg_response,
                sdg_formatted = excluded.sdg_formatted,
                sdg_note = excluded.sdg_note,
                classified_at = excluded.classified_at
            """,
            payload,
        )
        conn.commit()
