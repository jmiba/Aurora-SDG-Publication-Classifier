"""SQLite cache for source records, canonical publications, and SDG results."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from publication_sources import publication_deduplication_key, reconcile_oa_pair


DB_PATH = Path("cache.sqlite3")
_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_conn() -> sqlite3.Connection:
    """Return a global SQLite connection, initializing schema if needed."""
    global _CONN
    if _CONN is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL;")
        _CONN.execute("PRAGMA synchronous=FULL;")
        _CONN.execute("PRAGMA foreign_keys=ON;")
        _init_schema(_CONN)
    return _CONN


def close_connection() -> None:
    """Close the process-level connection, primarily for clean shutdown and tests."""
    global _CONN
    if _CONN is not None:
        _CONN.close()
        _CONN = None


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create current tables and non-destructively import the legacy cache."""
    with conn:
        # Legacy tables remain intact so existing cache files stay recoverable.
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
        legacy_columns = {row["name"] for row in conn.execute("PRAGMA table_info(works)")}
        if "institution_affiliations_json" not in legacy_columns:
            conn.execute("ALTER TABLE works ADD COLUMN institution_affiliations_json TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_works (
                publication_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_record_id TEXT,
                source_record_keys TEXT,
                record_url TEXT,
                record_urls TEXT,
                openalex_id TEXT,
                title TEXT,
                publication_date TEXT,
                doi TEXT,
                type TEXT,
                language TEXT,
                is_oa INTEGER,
                oa_status TEXT,
                authors TEXT,
                institutions TEXT,
                institution_ids TEXT,
                institution_countries TEXT,
                institution_names_raw TEXT,
                institution_affiliations_json TEXT,
                abstract TEXT,
                source_count INTEGER,
                source_provenance_json TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_records (
                source_record_key TEXT PRIMARY KEY,
                publication_key TEXT NOT NULL,
                source TEXT NOT NULL,
                source_label TEXT,
                source_record_id TEXT NOT NULL,
                record_url TEXT,
                raw_json TEXT,
                fetched_at TEXT NOT NULL,
                FOREIGN KEY (publication_key) REFERENCES canonical_works(publication_key)
                    ON UPDATE CASCADE ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_source_records_publication
            ON source_records(publication_key)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sdg_results_v2 (
                publication_key TEXT NOT NULL,
                model TEXT NOT NULL,
                text_hash TEXT NOT NULL DEFAULT '',
                sdg_response TEXT,
                sdg_formatted TEXT,
                sdg_note TEXT,
                classified_at TEXT NOT NULL,
                PRIMARY KEY (publication_key, model),
                FOREIGN KEY (publication_key) REFERENCES canonical_works(publication_key)
                    ON UPDATE CASCADE ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sdg_results_v2_model
            ON sdg_results_v2(model)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        legacy_import = conn.execute(
            "SELECT value FROM cache_meta WHERE key = 'legacy_import_v2'"
        ).fetchone()
        if not legacy_import:
            _migrate_legacy_rows(conn)
            conn.execute(
                "INSERT INTO cache_meta(key, value) VALUES ('legacy_import_v2', ?)",
                (_now(),),
            )
        _repair_oa_consistency(conn)


def _repair_oa_consistency(conn: sqlite3.Connection) -> None:
    """Repair contradictory Open Access pairs written by earlier app versions."""
    open_statuses = "'diamond', 'gold', 'hybrid', 'green', 'bronze', 'open'"
    conn.execute(
        f"""
        UPDATE canonical_works
        SET oa_status = 'open'
        WHERE is_oa = 1
          AND lower(trim(COALESCE(oa_status, ''))) NOT IN ({open_statuses})
        """
    )
    conn.execute(
        f"""
        UPDATE canonical_works
        SET oa_status = 'closed'
        WHERE is_oa = 0
          AND lower(trim(COALESCE(oa_status, ''))) <> 'closed'
        """
    )
    conn.execute(
        f"""
        UPDATE canonical_works
        SET is_oa = 1
        WHERE is_oa IS NULL
          AND lower(trim(COALESCE(oa_status, ''))) IN ({open_statuses})
        """
    )
    conn.execute(
        """
        UPDATE canonical_works
        SET is_oa = 0
        WHERE is_oa IS NULL
          AND lower(trim(COALESCE(oa_status, ''))) = 'closed'
        """
    )


def _migrate_legacy_rows(conn: sqlite3.Connection) -> None:
    """Copy legacy OpenAlex rows into v2 tables without altering legacy data."""
    legacy_rows = conn.execute("SELECT * FROM works WHERE openalex_id IS NOT NULL").fetchall()
    now = _now()
    openalex_to_publication: Dict[str, str] = {}
    for legacy_row in legacy_rows:
        row = dict(legacy_row)
        openalex_id = str(row.get("openalex_id") or "").strip()
        if not openalex_id:
            continue
        source_record_id = openalex_id.rstrip("/").split("/")[-1]
        source_record_key = f"openalex:{source_record_id}"
        row.update(
            {
                "source": "openalex",
                "source_record_key": source_record_key,
                "source_record_id": source_record_id,
            }
        )
        publication_key = publication_deduplication_key(row) or source_record_key
        openalex_to_publication[openalex_id] = publication_key
        conn.execute(
            """
            INSERT OR IGNORE INTO canonical_works (
                publication_key, source, source_record_id, source_record_keys,
                record_url, record_urls, openalex_id, title, publication_date,
                doi, type, language, is_oa, oa_status, authors, institutions,
                institution_affiliations_json, abstract, source_count,
                source_provenance_json, updated_at
            ) VALUES (?, 'openalex', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                publication_key,
                source_record_id,
                source_record_key,
                openalex_id,
                openalex_id,
                openalex_id,
                row.get("title"),
                row.get("publication_date"),
                row.get("doi"),
                row.get("type"),
                row.get("language"),
                row.get("is_oa"),
                row.get("oa_status"),
                row.get("authors"),
                row.get("institutions"),
                row.get("institution_affiliations_json"),
                row.get("abstract"),
                json.dumps(
                    [
                        {
                            "source": "openalex",
                            "source_label": "OpenAlex",
                            "source_record_id": source_record_id,
                            "source_record_key": source_record_key,
                            "record_url": openalex_id,
                        }
                    ],
                    ensure_ascii=False,
                ),
                row.get("updated_at") or now,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO source_records (
                source_record_key, publication_key, source, source_label,
                source_record_id, record_url, raw_json, fetched_at
            ) VALUES (?, ?, 'openalex', 'OpenAlex', ?, ?, ?, ?)
            """,
            (
                source_record_key,
                publication_key,
                source_record_id,
                openalex_id,
                row.get("raw_json"),
                row.get("updated_at") or now,
            ),
        )

    for legacy_sdg in conn.execute("SELECT * FROM sdg_results").fetchall():
        row = dict(legacy_sdg)
        publication_key = openalex_to_publication.get(str(row.get("openalex_id") or ""))
        if not publication_key:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO sdg_results_v2 (
                publication_key, model, text_hash, sdg_response,
                sdg_formatted, sdg_note, classified_at
            ) VALUES (?, ?, '', ?, ?, ?, ?)
            """,
            (
                publication_key,
                row.get("model"),
                row.get("sdg_response"),
                row.get("sdg_formatted"),
                row.get("sdg_note"),
                row.get("classified_at") or now,
            ),
        )


def get_cached_publication(publication_key: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM canonical_works WHERE publication_key = ?",
        (publication_key.strip(),),
    ).fetchone()
    return dict(row) if row else None


def get_cached_work(publication_key: str) -> Optional[Dict[str, Any]]:
    """Backward-compatible alias for canonical publication lookup."""
    return get_cached_publication(publication_key)


def _split_semicolon(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def _merge_semicolon(left: Any, right: Any) -> str:
    return "; ".join(dict.fromkeys([*_split_semicolon(left), *_split_semicolon(right)]))


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        decoded = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _merge_json_records(left: Any, right: Any, key_fields: tuple[str, ...]) -> str:
    merged: list[Any] = []
    positions: Dict[str, int] = {}
    for entry in [*_json_list(left), *_json_list(right)]:
        if not isinstance(entry, Mapping):
            continue
        key = next((str(entry.get(field) or "").strip() for field in key_fields if entry.get(field)), "")
        if key and key in positions:
            merged[positions[key]] = dict(entry)
        else:
            if key:
                positions[key] = len(merged)
            merged.append(dict(entry))
    return json.dumps(merged, ensure_ascii=False)


def _canonical_payload(
    row: Mapping[str, Any], existing: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Build a canonical row without discarding richer data from earlier runs."""
    publication_key = str(row.get("publication_key") or "").strip()
    if not publication_key:
        raise ValueError("publication_key is required")
    is_oa, oa_status = reconcile_oa_pair(row.get("is_oa"), row.get("oa_status"))
    payload = {
        "publication_key": publication_key,
        "source": row.get("source") or "unknown",
        "source_record_id": row.get("source_record_id"),
        "source_record_keys": row.get("source_record_keys") or row.get("source_record_key"),
        "record_url": row.get("record_url"),
        "record_urls": row.get("record_urls") or row.get("record_url"),
        "openalex_id": row.get("openalex_id"),
        "title": row.get("title"),
        "publication_date": row.get("publication_date"),
        "doi": row.get("doi"),
        "type": row.get("type"),
        "language": row.get("language"),
        "is_oa": int(is_oa) if is_oa is not None else None,
        "oa_status": oa_status,
        "authors": row.get("authors"),
        "institutions": row.get("institutions"),
        "institution_ids": row.get("institution_ids"),
        "institution_countries": row.get("institution_countries"),
        "institution_names_raw": row.get("institution_names_raw"),
        "institution_affiliations_json": row.get("institution_affiliations_json"),
        "abstract": row.get("abstract"),
        "source_count": row.get("source_count") or 1,
        "source_provenance_json": row.get("source_provenance_json"),
        "updated_at": _now(),
    }
    if not existing:
        return payload

    existing_oa, existing_status = reconcile_oa_pair(
        existing.get("is_oa"), existing.get("oa_status")
    )
    if True in (existing_oa, is_oa):
        payload["is_oa"] = 1
        open_statuses = [
            status
            for status in (existing_status, oa_status)
            if status not in {"closed", "unknown"}
        ]
        payload["oa_status"] = next(
            (status for status in open_statuses if status != "open"),
            open_statuses[0] if open_statuses else "open",
        )
    elif False in (existing_oa, is_oa):
        payload["is_oa"] = 0
        payload["oa_status"] = "closed"
    else:
        payload["is_oa"] = None
        payload["oa_status"] = "unknown"

    for field in (
        "record_url",
        "openalex_id",
        "publication_date",
        "doi",
        "type",
        "language",
    ):
        if not payload.get(field):
            payload[field] = existing.get(field)
    for field in ("title", "abstract", "authors"):
        if len(str(existing.get(field) or "")) > len(str(payload.get(field) or "")):
            payload[field] = existing.get(field)
    for field in (
        "source",
        "source_record_id",
        "source_record_keys",
        "record_urls",
        "institutions",
        "institution_ids",
        "institution_countries",
        "institution_names_raw",
    ):
        payload[field] = _merge_semicolon(existing.get(field), payload.get(field))
    payload["institution_affiliations_json"] = _merge_json_records(
        existing.get("institution_affiliations_json"),
        payload.get("institution_affiliations_json"),
        ("id", "name"),
    )
    payload["source_provenance_json"] = _merge_json_records(
        existing.get("source_provenance_json"),
        payload.get("source_provenance_json"),
        ("source_record_key",),
    )
    payload["source_count"] = max(
        int(existing.get("source_count") or 0), int(payload.get("source_count") or 0), 1
    )
    return payload


def _execute_upsert_publication(
    conn: sqlite3.Connection, payload: Mapping[str, Any]
) -> None:
    columns = ", ".join(payload)
    placeholders = ", ".join(f":{column}" for column in payload)
    update_clause = ", ".join(
        f"{column}=excluded.{column}" for column in payload if column != "publication_key"
    )
    conn.execute(
        f"""
        INSERT INTO canonical_works ({columns}) VALUES ({placeholders})
        ON CONFLICT(publication_key) DO UPDATE SET {update_clause}
        """,
        payload,
    )


def upsert_publication(row: Mapping[str, Any]) -> None:
    """Create or enrich a canonical publication without reducing cached metadata."""
    with _LOCK:
        conn = _get_conn()
        with conn:
            publication_key = str(row.get("publication_key") or "").strip()
            existing_row = conn.execute(
                "SELECT * FROM canonical_works WHERE publication_key = ?", (publication_key,)
            ).fetchone()
            payload = _canonical_payload(row, dict(existing_row) if existing_row else None)
            _execute_upsert_publication(conn, payload)


def _source_record_payload(
    publication_key: str, source_record: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    source_record_key = str(source_record.get("source_record_key") or "").strip()
    source_record_id = str(source_record.get("source_record_id") or "").strip()
    if not source_record_key or not source_record_id:
        return None
    raw_record = source_record.get("raw_record") or source_record.get("_raw_record")
    return {
        "source_record_key": source_record_key,
        "publication_key": publication_key,
        "source": source_record.get("source") or "unknown",
        "source_label": source_record.get("source_label"),
        "source_record_id": source_record_id,
        "record_url": source_record.get("record_url"),
        "raw_json": json.dumps(raw_record, ensure_ascii=False) if raw_record else None,
        "fetched_at": _now(),
    }


def _execute_upsert_source_record(
    conn: sqlite3.Connection, payload: Mapping[str, Any]
) -> None:
    conn.execute(
        """
        INSERT INTO source_records (
            source_record_key, publication_key, source, source_label,
            source_record_id, record_url, raw_json, fetched_at
        ) VALUES (
            :source_record_key, :publication_key, :source, :source_label,
            :source_record_id, :record_url, :raw_json, :fetched_at
        )
        ON CONFLICT(source_record_key) DO UPDATE SET
            publication_key=excluded.publication_key,
            source=excluded.source,
            source_label=excluded.source_label,
            source_record_id=excluded.source_record_id,
            record_url=excluded.record_url,
            raw_json=excluded.raw_json,
            fetched_at=excluded.fetched_at
        """,
        payload,
    )


def _sync_canonical_provenance(conn: sqlite3.Connection, publication_key: str) -> None:
    """Make the canonical source summary match all persisted source records."""
    rows = conn.execute(
        """
        SELECT source_record_key, source, source_label, source_record_id, record_url
        FROM source_records
        WHERE publication_key = ?
        ORDER BY source_record_key
        """,
        (publication_key,),
    ).fetchall()
    if not rows:
        return
    provenance = [dict(record) for record in rows]
    sources = list(dict.fromkeys(record["source"] for record in provenance if record["source"]))
    source_ids = list(
        dict.fromkeys(record["source_record_id"] for record in provenance if record["source_record_id"])
    )
    source_keys = [record["source_record_key"] for record in provenance]
    record_urls = list(
        dict.fromkeys(record["record_url"] for record in provenance if record["record_url"])
    )
    conn.execute(
        """
        UPDATE canonical_works
        SET source = ?, source_record_id = ?, source_record_keys = ?,
            record_url = ?, record_urls = ?, source_count = ?,
            source_provenance_json = ?, updated_at = ?
        WHERE publication_key = ?
        """,
        (
            "; ".join(sources),
            "; ".join(source_ids),
            "; ".join(source_keys),
            record_urls[0] if record_urls else None,
            "; ".join(record_urls),
            len(sources),
            json.dumps(provenance, ensure_ascii=False),
            _now(),
            publication_key,
        )
    )


def upsert_source_record(publication_key: str, source_record: Mapping[str, Any]) -> None:
    payload = _source_record_payload(publication_key, source_record)
    if not payload:
        return
    with _LOCK:
        conn = _get_conn()
        with conn:
            previous = conn.execute(
                "SELECT publication_key FROM source_records WHERE source_record_key = ?",
                (payload["source_record_key"],),
            ).fetchone()
            _execute_upsert_source_record(conn, payload)
            _sync_canonical_provenance(conn, publication_key)
            if previous and previous["publication_key"] != publication_key:
                _sync_canonical_provenance(conn, previous["publication_key"])


def upsert_work(row: Mapping[str, Any], raw_record: Optional[Mapping[str, Any]] = None) -> None:
    """Persist one canonical work and its source records in a single transaction."""
    publication_key = str(row.get("publication_key") or "")
    source_records = row.get("_source_records") or []
    if not source_records and row.get("source_record_key"):
        source_records = [
            {
                "source_record_key": row.get("source_record_key"),
                "source_record_id": row.get("source_record_id"),
                "source": row.get("source"),
                "source_label": row.get("source_label"),
                "record_url": row.get("record_url"),
                "raw_record": raw_record,
            }
        ]
    with _LOCK:
        conn = _get_conn()
        with conn:
            existing_row = conn.execute(
                "SELECT * FROM canonical_works WHERE publication_key = ?", (publication_key,)
            ).fetchone()
            payload = _canonical_payload(row, dict(existing_row) if existing_row else None)
            _execute_upsert_publication(conn, payload)
            previous_publication_keys = set()
            for source_record in source_records:
                source_payload = _source_record_payload(publication_key, source_record)
                if source_payload:
                    previous = conn.execute(
                        "SELECT publication_key FROM source_records WHERE source_record_key = ?",
                        (source_payload["source_record_key"],),
                    ).fetchone()
                    if previous and previous["publication_key"] != publication_key:
                        previous_publication_keys.add(previous["publication_key"])
                    _execute_upsert_source_record(conn, source_payload)
            _sync_canonical_provenance(conn, publication_key)
            for previous_key in previous_publication_keys:
                _sync_canonical_provenance(conn, previous_key)


def get_cached_sdg_result(publication_key: str, model: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT text_hash, sdg_response, sdg_formatted, sdg_note, classified_at
        FROM sdg_results_v2
        WHERE publication_key = ? AND model = ?
        """,
        (publication_key.strip(), model.strip()),
    ).fetchone()
    return dict(row) if row else None


def upsert_sdg_result(
    publication_key: Optional[str] = None,
    model: str = "",
    sdg_response: Optional[Any] = None,
    sdg_formatted: str = "",
    sdg_note: str = "",
    text_hash: str = "",
    *,
    openalex_id: Optional[str] = None,
) -> None:
    """Persist a classification for a canonical publication and input text hash."""
    key = str(publication_key or openalex_id or "").strip()
    if not key:
        raise ValueError("publication_key is required")
    payload = {
        "publication_key": key,
        "model": model.strip(),
        "text_hash": text_hash,
        "sdg_response": json.dumps(sdg_response, ensure_ascii=False)
        if sdg_response is not None
        else None,
        "sdg_formatted": sdg_formatted,
        "sdg_note": sdg_note,
        "classified_at": _now(),
    }
    with _LOCK:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO sdg_results_v2 (
                publication_key, model, text_hash, sdg_response,
                sdg_formatted, sdg_note, classified_at
            ) VALUES (
                :publication_key, :model, :text_hash, :sdg_response,
                :sdg_formatted, :sdg_note, :classified_at
            )
            ON CONFLICT(publication_key, model) DO UPDATE SET
                text_hash=excluded.text_hash,
                sdg_response=excluded.sdg_response,
                sdg_formatted=excluded.sdg_formatted,
                sdg_note=excluded.sdg_note,
                classified_at=excluded.classified_at
            """,
            payload,
        )
        conn.commit()


__all__ = [
    "close_connection",
    "get_cached_publication",
    "get_cached_sdg_result",
    "get_cached_work",
    "upsert_publication",
    "upsert_sdg_result",
    "upsert_source_record",
    "upsert_work",
]
