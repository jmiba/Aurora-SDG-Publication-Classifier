"""Unit tests for generic DSpace normalization, deduplication, and caching."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import cache_db
import openalex_sdg
from publication_sources import (
    DSpaceSource,
    deduplicate_publications,
    end_of_month,
    fetch_dspace_records,
    normalize_dspace_object,
    normalize_openalex_work,
    parse_dspace_sources,
    reconcile_oa_pair,
)


def dspace_search_object(
    *,
    item_id: str,
    entity_type: str,
    title: str,
    metadata: dict,
) -> dict:
    return {
        "_embedded": {
            "indexableObject": {
                "id": item_id,
                "uuid": item_id,
                "name": title,
                "handle": f"repo/{item_id}",
                "entityType": entity_type,
                "type": "item",
                "metadata": metadata,
                "_links": {
                    # Adapters must not follow or expose media links.
                    "thumbnail": {"href": f"https://repo.example/thumbnail/{item_id}"},
                    "bundles": {"href": f"https://repo.example/bundles/{item_id}"},
                },
            }
        }
    }


def metadata_entry(value: str) -> list:
    return [{"value": value}]


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, payloads: list):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append({"url": url, "params": dict(params), "headers": dict(headers), "timeout": timeout})
        return FakeResponse(self.payloads.pop(0))


class PublicationSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = DSpaceSource(
            id="example",
            label="Example Repository",
            base_url="https://repo.example/server/api",
        )

    def test_parse_multiple_generic_dspace_sources(self) -> None:
        sources = parse_dspace_sources(
            [
                {
                    "id": "source-one",
                    "label": "Source One",
                    "base_url": "https://one.example/server/api",
                    "entity_types": ["Article"],
                    "openalex_institution_id": "https://openalex.org/I123456",
                    "ror_id": "https://ror.org/012345678",
                },
                {
                    "id": "source-two",
                    "label": "Source Two",
                    "base_url": "https://two.example/server/api",
                    "enabled": True,
                },
                {
                    "id": "disabled",
                    "base_url": "https://disabled.example/server/api",
                    "enabled": False,
                },
            ]
        )
        self.assertEqual([source.id for source in sources], ["source-one", "source-two"])
        self.assertEqual(sources[0].entity_types, ("Article",))
        self.assertEqual(
            sources[0].openalex_institution_id,
            "https://openalex.org/I123456",
        )
        self.assertEqual(sources[0].ror_id, "https://ror.org/012345678")
        self.assertEqual(sources[0].openalex_query_id, "https://openalex.org/I123456")

    def test_invalid_institution_identifiers_are_not_loaded(self) -> None:
        source = parse_dspace_sources(
            {
                "id": "invalid-identifiers",
                "base_url": "https://repo.example/server/api",
                "openalex_institution_id": "not-an-openalex-id",
                "ror_id": "not-a-ror-id",
            }
        )[0]

        self.assertIsNone(source.openalex_institution_id)
        self.assertIsNone(source.ror_id)
        self.assertIsNone(source.openalex_query_id)

    def test_normalize_article_without_exposing_artwork_or_thumbnail_media(self) -> None:
        record = dspace_search_object(
            item_id="article-1",
            entity_type="Article",
            title="A useful article",
            metadata={
                "dc.abstract.en": metadata_entry("English abstract"),
                "dc.contributor.author": metadata_entry("Doe, Jane"),
                "dc.date.issued": metadata_entry("2024-04"),
                "dc.identifier.doi": metadata_entry("invalid-prefix"),
                "dc.identifier.weblink": metadata_entry("https://doi.org/10.1234/Example.DOI"),
                "dc.identifier.uri": metadata_entry("https://repo.example/handle/repo/1"),
                "dc.language": metadata_entry("en"),
                "dc.rights": metadata_entry("CC-BY"),
                "dc.type": metadata_entry("JournalArticle"),
                "dc.affiliation": metadata_entry("Example University"),
            },
        )
        normalized = normalize_dspace_object(record, self.source)
        self.assertEqual(normalized["type"], "article")
        self.assertEqual(normalized["doi"], "https://doi.org/10.1234/example.doi")
        self.assertEqual(normalized["is_oa"], True)
        self.assertNotIn("thumbnail", normalized)
        self.assertNotIn("image", normalized)
        self.assertNotIn("bundles", normalized)

    def test_book_chapter_and_multilingual_artistic_abstracts(self) -> None:
        book = normalize_dspace_object(
            dspace_search_object(
                item_id="book-1",
                entity_type="Book",
                title="Chapter",
                metadata={
                    "dc.type": metadata_entry("MonographyChapter"),
                    "dc.date.issued": metadata_entry("2025"),
                    "dc.rights": metadata_entry("ClosedAccess"),
                },
            ),
            self.source,
        )
        artwork = normalize_dspace_object(
            dspace_search_object(
                item_id="art-1",
                entity_type="Artistic",
                title="Artwork documentation",
                metadata={
                    "dc.abstract.author": metadata_entry("Author description"),
                    "dc.abstract.en": metadata_entry("English description"),
                    "dc.abstract.pl": metadata_entry("Polski opis"),
                    "dc.date.issued": metadata_entry("2023-09-15"),
                    "dc.type": metadata_entry("ArtProjectAuthor"),
                },
            ),
            self.source,
        )
        self.assertEqual(book["type"], "book-chapter")
        self.assertEqual(book["is_oa"], False)
        self.assertEqual(artwork["type"], "artistic-work")
        self.assertEqual(artwork["abstract"], "English description")
        self.assertEqual(artwork["language"], "")

    def test_standard_dspace_description_abstract_is_normalized(self) -> None:
        record = normalize_dspace_object(
            dspace_search_object(
                item_id="standard-abstract",
                entity_type="Article",
                title="Standard DSpace metadata",
                metadata={
                    "dc.description.abstract": metadata_entry(
                        "Abstract stored under the standard DSpace key."
                    ),
                    "dc.date.issued": metadata_entry("2024"),
                },
            ),
            self.source,
        )

        self.assertEqual(
            record["abstract"], "Abstract stored under the standard DSpace key."
        )

    def test_top_level_aurora_list_response_is_formatted(self) -> None:
        formatted = openalex_sdg.format_sdg_predictions(
            [
                {"label": "SDG 4 (Quality Education)", "score": 0.84},
                {"label": "malformed", "score": "not-a-number"},
                None,
            ]
        )

        self.assertEqual(formatted, "84% SDG 4 (Quality Education)")

    def test_exact_doi_records_are_automatically_merged_with_provenance(self) -> None:
        openalex = normalize_openalex_work(
            {
                "id": "https://openalex.org/W1",
                "title": "Shared publication",
                "publication_date": "2024-01-01",
                "doi": "https://doi.org/10.1234/shared",
                "type": "article",
                "authorships": [{"author": {"display_name": "Jane Doe"}, "institutions": []}],
                "open_access": {"is_oa": None},
            }
        )
        dspace = normalize_dspace_object(
            dspace_search_object(
                item_id="item-1",
                entity_type="Article",
                title="Shared publication",
                metadata={
                    "dc.identifier.doi": metadata_entry("10.1234/shared"),
                    "dc.contributor.author": metadata_entry("Doe, Jane"),
                    "dc.date.issued": metadata_entry("2024"),
                    "dc.abstract.en": metadata_entry("The longer repository abstract."),
                    "dc.type": metadata_entry("JournalArticle"),
                },
            ),
            self.source,
        )
        merged = deduplicate_publications([openalex, dspace])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["publication_key"], "doi:10.1234/shared")
        self.assertEqual(merged[0]["source_count"], 2)
        self.assertEqual(merged[0]["abstract"], "The longer repository abstract.")
        provenance = json.loads(merged[0]["source_provenance_json"])
        self.assertEqual({entry["source"] for entry in provenance}, {"openalex", "example"})

    def test_conflicting_oa_sources_produce_a_consistent_open_pair(self) -> None:
        openalex = normalize_openalex_work(
            {
                "id": "https://openalex.org/W-OA",
                "title": "Shared OA publication",
                "publication_date": "2024-01-01",
                "doi": "10.1234/shared-oa",
                "type": "article",
                "authorships": [],
                "open_access": {"is_oa": False, "oa_status": "closed"},
            }
        )
        dspace = normalize_dspace_object(
            dspace_search_object(
                item_id="open-item",
                entity_type="Article",
                title="Shared OA publication",
                metadata={
                    "dc.identifier.doi": metadata_entry("10.1234/shared-oa"),
                    "dc.date.issued": metadata_entry("2024"),
                    "dc.rights": metadata_entry("CC-BY"),
                },
            ),
            self.source,
        )

        merged = deduplicate_publications([openalex, dspace])[0]

        self.assertIs(merged["is_oa"], True)
        self.assertEqual(merged["oa_status"], "open")
        self.assertEqual(reconcile_oa_pair(False, "gold"), (False, "closed"))

    def test_dspace_pagination_uses_no_media_embeds(self) -> None:
        def payload(page_number: int, item_id: str) -> dict:
            return {
                "_embedded": {
                    "searchResult": {
                        "page": {
                            "number": page_number,
                            "size": 1,
                            "totalPages": 2,
                            "totalElements": 2,
                        },
                        "_embedded": {
                            "objects": [
                                dspace_search_object(
                                    item_id=item_id,
                                    entity_type="Article",
                                    title=f"Article {item_id}",
                                    metadata={
                                        "dc.date.issued": metadata_entry("2024"),
                                        "dc.type": metadata_entry("JournalArticle"),
                                    },
                                )
                            ]
                        },
                    }
                }
            }

        session = FakeSession([payload(0, "one"), payload(1, "two")])
        records, total = fetch_dspace_records(
            session,
            DSpaceSource(
                id="article-only",
                label="Article Repository",
                base_url="https://repo.example/server/api",
                entity_types=("Article",),
            ),
            from_date="2023-01-01",
            to_date="2026-08-31",
            work_type="article",
            user_agent="test-agent",
            page_size=1,
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(total, 2)
        self.assertEqual([call["params"]["page"] for call in session.calls], [0, 1])
        for call in session.calls:
            serialized = json.dumps(call["params"]).lower()
            self.assertNotIn("embed", serialized)
            self.assertNotIn("thumbnail", serialized)
            self.assertNotIn("metrics", serialized)

    def test_dspace_limit_collects_candidates_from_each_entity_type(self) -> None:
        payloads = []
        for entity_type in ("Article", "Book", "Artistic"):
            payloads.append(
                {
                    "_embedded": {
                        "searchResult": {
                            "page": {
                                "number": 0,
                                "size": 1,
                                "totalPages": 1,
                                "totalElements": 1,
                            },
                            "_embedded": {
                                "objects": [
                                    dspace_search_object(
                                        item_id=entity_type.lower(),
                                        entity_type=entity_type,
                                        title=entity_type,
                                        metadata={
                                            "dc.date.issued": metadata_entry("2024")
                                        },
                                    )
                                ]
                            },
                        }
                    }
                }
            )
        session = FakeSession(payloads)

        records, _ = fetch_dspace_records(
            session,
            self.source,
            from_date="2023-01-01",
            to_date="2026-08-31",
            work_type=None,
            user_agent="test-agent",
            limit_rows=1,
            page_size=1,
        )

        self.assertEqual(len(records), 3)
        self.assertEqual(
            [call["params"]["f.entityType"] for call in session.calls],
            ["Article,equals", "Book,equals", "Artistic,equals"],
        )

    def test_book_and_chapter_selection_uses_one_dspace_book_query(self) -> None:
        payload = {
            "_embedded": {
                "searchResult": {
                    "page": {
                        "number": 0,
                        "size": 100,
                        "totalPages": 1,
                        "totalElements": 2,
                    },
                    "_embedded": {
                        "objects": [
                            dspace_search_object(
                                item_id="book",
                                entity_type="Book",
                                title="A monograph",
                                metadata={"dc.type": metadata_entry("Monography")},
                            ),
                            dspace_search_object(
                                item_id="chapter",
                                entity_type="Book",
                                title="A chapter",
                                metadata={"dc.type": metadata_entry("MonographyChapter")},
                            ),
                        ]
                    },
                }
            }
        }
        session = FakeSession([payload])

        records, _ = fetch_dspace_records(
            session,
            self.source,
            from_date="2023-01-01",
            to_date="2026-08-31",
            work_type=["book", "book-chapter"],
            user_agent="test-agent",
        )

        self.assertEqual({record["type"] for record in records}, {"book", "book-chapter"})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["params"]["f.entityType"], "Book,equals")

    def test_openalex_filter_combines_selected_types_with_or(self) -> None:
        filter_value = openalex_sdg.make_filter(
            "https://openalex.org/I123",
            "2023-01-01",
            ["article", "book", "book-chapter"],
            "2026-08-31",
        )

        self.assertIn("institutions.id:I123", filter_value)
        self.assertIn("type:article|book|book-chapter", filter_value)

    def test_serpapi_rejects_similar_but_different_title(self) -> None:
        response = FakeResponse(
            {
                "organic_results": [
                    {
                        "title": "Climate change and health",
                        "snippet": "This belongs to a different publication.",
                    },
                    {
                        "title": "Climate change",
                        "snippet": "This is the correct abstract.",
                        "publication_info": {
                            "summary": "A Researcher - Example Journal, 2024"
                        },
                    },
                ]
            }
        )
        session = Mock()
        session.get.return_value = response

        abstract = openalex_sdg.get_abstract_from_serpapi_google_scholar(
            "Climate change",
            "A Researcher",
            api_key="test-key",
            session=session,
            publication_year="2024-04-01",
        )

        self.assertEqual(abstract, "This is the correct abstract.")

    def test_serpapi_rejects_conflicting_doi_or_year(self) -> None:
        response = FakeResponse(
            {
                "organic_results": [
                    {
                        "title": "Climate change",
                        "doi": "https://doi.org/10.1000/wrong",
                        "snippet": "Wrong DOI.",
                        "publication_info": {"summary": "Example Journal, 2024"},
                    },
                    {
                        "title": "Climate change",
                        "doi": "10.1000/correct",
                        "snippet": "Wrong year.",
                        "publication_info": {"summary": "Example Journal, 2023"},
                    },
                ]
            }
        )
        session = Mock()
        session.get.return_value = response

        abstract = openalex_sdg.get_abstract_from_serpapi_google_scholar(
            "Climate change",
            "A Researcher",
            api_key="test-key",
            session=session,
            doi="https://doi.org/10.1000/correct",
            publication_year="2024",
        )

        self.assertIsNone(abstract)

    def test_serpapi_accepts_matching_normalized_doi_and_year(self) -> None:
        response = FakeResponse(
            {
                "organic_results": [
                    {
                        "title": "Climate change",
                        "link": "https://doi.org/10.1000/CORRECT",
                        "snippet": "Verified abstract.",
                        "publication_info": {"summary": "Example Journal, 2024"},
                    }
                ]
            }
        )
        session = Mock()
        session.get.return_value = response

        abstract = openalex_sdg.get_abstract_from_serpapi_google_scholar(
            "Climate change",
            "A Researcher",
            api_key="test-key",
            session=session,
            doi="doi:10.1000/correct",
            publication_year="2024-04-01",
        )

        self.assertEqual(abstract, "Verified abstract.")

    def test_mixed_openalex_and_ror_institutions_use_separate_queries(self) -> None:
        with patch.object(
            openalex_sdg,
            "fetch_openalex_records",
            return_value=([], 0),
        ) as fetch_openalex:
            _, stats = openalex_sdg.fetch_publications_with_sdg(
                include_openalex=True,
                dspace_sources=[],
                institution_id="https://openalex.org/I123",
                extra_institution_ids=["https://ror.org/012345678"],
                from_date="2023-01-01",
                to_date="2026-08-31",
                work_type=["article", "book"],
                model="skip",
                enable_google_scholar=False,
            )

        self.assertEqual(fetch_openalex.call_count, 2)
        filters = [call.kwargs["filter_value"] for call in fetch_openalex.call_args_list]
        self.assertTrue(any("institutions.id:I123" in value for value in filters))
        self.assertTrue(
            any("institutions.ror:https://ror.org/012345678" in value for value in filters)
        )
        self.assertEqual(stats.sources_queried, ["OpenAlex"])

    def test_end_of_month_including_leap_year(self) -> None:
        self.assertEqual(end_of_month(date(2024, 2, 1)), date(2024, 2, 29))
        self.assertEqual(end_of_month(date(2025, 2, 1)), date(2025, 2, 28))


class CacheMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        cache_db.close_connection()
        self.original_path = cache_db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        cache_db.DB_PATH = Path(self.temp_dir.name) / "cache.sqlite3"

    def tearDown(self) -> None:
        cache_db.close_connection()
        cache_db.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_v2_cache_keeps_source_records_and_text_hash(self) -> None:
        publication = {
            "publication_key": "doi:10.1234/shared",
            "source": "openalex; example",
            "source_record_id": "W1; item-1",
            "source_record_keys": "openalex:W1; dspace:example:item-1",
            "record_url": "https://openalex.org/W1",
            "title": "Shared publication",
            "abstract": "Shared abstract",
            "source_count": 2,
            "_source_records": [
                {
                    "source": "openalex",
                    "source_label": "OpenAlex",
                    "source_record_id": "W1",
                    "source_record_key": "openalex:W1",
                    "record_url": "https://openalex.org/W1",
                    "raw_record": {"id": "https://openalex.org/W1"},
                },
                {
                    "source": "example",
                    "source_label": "Example Repository",
                    "source_record_id": "item-1",
                    "source_record_key": "dspace:example:item-1",
                    "record_url": "https://repo.example/handle/item-1",
                    "raw_record": {"uuid": "item-1"},
                },
            ],
        }
        cache_db.upsert_work(publication)
        cache_db.upsert_sdg_result(
            publication_key=publication["publication_key"],
            model="aurora-sdg-multi",
            sdg_response={"predictions": []},
            sdg_formatted="",
            sdg_note="",
            text_hash="abc123",
        )
        cached = cache_db.get_cached_publication(publication["publication_key"])
        sdg = cache_db.get_cached_sdg_result(publication["publication_key"], "aurora-sdg-multi")
        self.assertEqual(cached["source_count"], 2)
        self.assertEqual(sdg["text_hash"], "abc123")

        conn = sqlite3.connect(cache_db.DB_PATH)
        try:
            source_count = conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]
            legacy_tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('works', 'sdg_results')"
                )
            }
        finally:
            conn.close()
        self.assertEqual(source_count, 2)
        self.assertEqual(legacy_tables, {"works", "sdg_results"})

    def test_schema_repairs_a_preexisting_contradictory_oa_pair(self) -> None:
        publication = {
            "publication_key": "doi:10.1234/oa-repair",
            "source": "openalex",
            "source_record_id": "W-OA-REPAIR",
            "source_record_key": "openalex:W-OA-REPAIR",
            "title": "OA repair",
            "is_oa": True,
            "oa_status": "open",
        }
        cache_db.upsert_work(publication)
        conn = sqlite3.connect(cache_db.DB_PATH)
        try:
            conn.execute(
                "UPDATE canonical_works SET oa_status = 'closed' WHERE publication_key = ?",
                (publication["publication_key"],),
            )
            conn.commit()
        finally:
            conn.close()

        cache_db.close_connection()
        repaired = cache_db.get_cached_publication(publication["publication_key"])

        self.assertEqual(repaired["is_oa"], 1)
        self.assertEqual(repaired["oa_status"], "open")

    def test_narrower_run_preserves_provenance_and_richer_abstract(self) -> None:
        publication = {
            "publication_key": "doi:10.1234/accumulated",
            "source": "openalex; example",
            "source_record_id": "W-ACC; repository-item",
            "source_record_keys": "openalex:W-ACC; dspace:example:repository-item",
            "record_url": "https://openalex.org/W-ACC",
            "record_urls": (
                "https://openalex.org/W-ACC; "
                "https://repo.example/handle/repository-item"
            ),
            "title": "Accumulated publication",
            "abstract": "The substantially richer abstract from an earlier combined run.",
            "source_count": 2,
            "source_provenance_json": json.dumps(
                [
                    {
                        "source": "openalex",
                        "source_label": "OpenAlex",
                        "source_record_id": "W-ACC",
                        "source_record_key": "openalex:W-ACC",
                        "record_url": "https://openalex.org/W-ACC",
                    },
                    {
                        "source": "example",
                        "source_label": "Example Repository",
                        "source_record_id": "repository-item",
                        "source_record_key": "dspace:example:repository-item",
                        "record_url": "https://repo.example/handle/repository-item",
                    },
                ]
            ),
            "_source_records": [
                {
                    "source": "openalex",
                    "source_label": "OpenAlex",
                    "source_record_id": "W-ACC",
                    "source_record_key": "openalex:W-ACC",
                    "record_url": "https://openalex.org/W-ACC",
                },
                {
                    "source": "example",
                    "source_label": "Example Repository",
                    "source_record_id": "repository-item",
                    "source_record_key": "dspace:example:repository-item",
                    "record_url": "https://repo.example/handle/repository-item",
                },
            ],
        }
        cache_db.upsert_work(publication)
        cache_db.upsert_work(
            {
                "publication_key": publication["publication_key"],
                "source": "openalex",
                "source_record_id": "W-ACC",
                "source_record_keys": "openalex:W-ACC",
                "record_url": "https://openalex.org/W-ACC",
                "title": "Accumulated publication",
                "abstract": "Short abstract.",
                "source_count": 1,
                "_source_records": [publication["_source_records"][0]],
            }
        )

        cached = cache_db.get_cached_publication(publication["publication_key"])
        provenance = json.loads(cached["source_provenance_json"])

        self.assertEqual(cached["source_count"], 2)
        self.assertEqual({entry["source"] for entry in provenance}, {"openalex", "example"})
        self.assertEqual(cached["abstract"], publication["abstract"])

    def test_global_limit_uses_recency_instead_of_source_order(self) -> None:
        openalex = normalize_openalex_work(
            {
                "id": "https://openalex.org/W-OLDER",
                "title": "Older OpenAlex record",
                "publication_date": "2023-01-01",
                "type": "article",
                "authorships": [],
            }
        )
        dspace = normalize_dspace_object(
            dspace_search_object(
                item_id="newer-dspace",
                entity_type="Article",
                title="Newer DSpace record",
                metadata={"dc.date.issued": metadata_entry("2025-01-01")},
            ),
            DSpaceSource(
                id="example",
                label="Example Repository",
                base_url="https://repo.example/server/api",
                entity_types=("Article",),
            ),
        )
        with (
            patch.object(
                openalex_sdg, "fetch_openalex_records", return_value=([openalex], 1)
            ),
            patch.object(
                openalex_sdg, "fetch_dspace_records", return_value=([dspace], 1)
            ),
        ):
            rows, stats = openalex_sdg.fetch_publications_with_sdg(
                include_openalex=True,
                dspace_sources=[
                    DSpaceSource(
                        id="example",
                        label="Example Repository",
                        base_url="https://repo.example/server/api",
                        entity_types=("Article",),
                    )
                ],
                institution_id="https://openalex.org/I1",
                from_date="2023-01-01",
                to_date="2026-08-31",
                work_type="article",
                model="skip",
                limit_rows=1,
                enable_google_scholar=False,
            )

        self.assertEqual(stats.total_source_records, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "example")

    def test_legacy_cache_is_copied_additively(self) -> None:
        conn = sqlite3.connect(cache_db.DB_PATH)
        try:
            conn.executescript(
                """
                CREATE TABLE works (
                    openalex_id TEXT PRIMARY KEY, title TEXT, publication_date TEXT,
                    doi TEXT, type TEXT, language TEXT, is_oa INTEGER, oa_status TEXT,
                    authors TEXT, institutions TEXT, institution_affiliations_json TEXT,
                    abstract TEXT, raw_json TEXT, updated_at TEXT
                );
                CREATE TABLE sdg_results (
                    openalex_id TEXT NOT NULL, model TEXT NOT NULL, sdg_response TEXT,
                    sdg_formatted TEXT, sdg_note TEXT, classified_at TEXT,
                    PRIMARY KEY (openalex_id, model)
                );
                """
            )
            conn.execute(
                """
                INSERT INTO works (
                    openalex_id, title, publication_date, doi, type, authors, abstract
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "https://openalex.org/WLEGACY",
                    "Legacy publication",
                    "2024-01-01",
                    "https://doi.org/10.1234/legacy",
                    "article",
                    "Doe, Jane",
                    "Legacy abstract",
                ),
            )
            conn.execute(
                """
                INSERT INTO sdg_results (
                    openalex_id, model, sdg_response, sdg_formatted, sdg_note
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "https://openalex.org/WLEGACY",
                    "aurora-sdg-multi",
                    "{}",
                    "",
                    "legacy",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        migrated = cache_db.get_cached_publication("doi:10.1234/legacy")
        migrated_sdg = cache_db.get_cached_sdg_result(
            "doi:10.1234/legacy", "aurora-sdg-multi"
        )
        self.assertEqual(migrated["openalex_id"], "https://openalex.org/WLEGACY")
        self.assertEqual(migrated_sdg["text_hash"], "")

        conn = sqlite3.connect(cache_db.DB_PATH)
        try:
            legacy_count = conn.execute("SELECT COUNT(*) FROM works").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(legacy_count, 1)

    def test_automated_multi_source_run_deduplicates_before_classification(self) -> None:
        openalex = normalize_openalex_work(
            {
                "id": "https://openalex.org/W1",
                "title": "Shared publication",
                "publication_date": "2024-01-01",
                "doi": "10.1234/shared",
                "type": "article",
                "abstract_inverted_index": {"Shared": [0], "abstract": [1]},
                "authorships": [{"author": {"display_name": "Jane Doe"}, "institutions": []}],
                "open_access": {"is_oa": True, "oa_status": "gold"},
            }
        )
        dspace = normalize_dspace_object(
            dspace_search_object(
                item_id="item-1",
                entity_type="Article",
                title="Shared publication",
                metadata={
                    "dc.identifier.doi": metadata_entry("10.1234/shared"),
                    "dc.contributor.author": metadata_entry("Doe, Jane"),
                    "dc.date.issued": metadata_entry("2024"),
                    "dc.abstract.en": metadata_entry("A longer shared repository abstract."),
                    "dc.type": metadata_entry("JournalArticle"),
                },
            ),
            DSpaceSource(
                id="example",
                label="Example Repository",
                base_url="https://repo.example/server/api",
                entity_types=("Article",),
            ),
        )
        prediction = {
            "predictions": [
                {"sdg": {"code": "4", "name": "Quality Education"}, "prediction": 0.9}
            ]
        }
        with (
            patch.object(openalex_sdg, "fetch_openalex_records", return_value=([openalex], 1)),
            patch.object(openalex_sdg, "fetch_dspace_records", return_value=([dspace], 1)),
            patch.object(openalex_sdg, "classify_text_aurora", return_value=(prediction, "")) as classify,
        ):
            rows, stats = openalex_sdg.fetch_publications_with_sdg(
                include_openalex=True,
                dspace_sources=[
                    DSpaceSource(
                        id="example",
                        label="Example Repository",
                        base_url="https://repo.example/server/api",
                        entity_types=("Article",),
                    )
                ],
                institution_id="https://openalex.org/I1",
                from_date="2023-01-01",
                to_date="2026-08-31",
                work_type="article",
                model="aurora-sdg-multi",
                enable_google_scholar=False,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(stats.total_source_records, 2)
            self.assertEqual(stats.duplicates_removed, 1)
            self.assertEqual(rows[0]["source_count"], 2)
            classify.assert_called_once()

            classify.reset_mock()
            second_rows, _ = openalex_sdg.fetch_publications_with_sdg(
                include_openalex=True,
                dspace_sources=[
                    DSpaceSource(
                        id="example",
                        label="Example Repository",
                        base_url="https://repo.example/server/api",
                        entity_types=("Article",),
                    )
                ],
                institution_id="https://openalex.org/I1",
                from_date="2023-01-01",
                to_date="2026-08-31",
                work_type="article",
                model="aurora-sdg-multi",
                enable_google_scholar=False,
            )
            self.assertEqual(second_rows[0]["sdg_formatted"], "90% SDG 4 (Quality Education)")
            classify.assert_not_called()

    def test_richer_cached_abstract_is_used_instead_of_shorter_source_text(self) -> None:
        source_publication = normalize_openalex_work(
            {
                "id": "https://openalex.org/W-RICHER",
                "title": "Preserve richer abstract",
                "publication_date": "2024-01-01",
                "doi": "10.1234/richer",
                "type": "article",
                "abstract_inverted_index": {"Short": [0], "abstract": [1]},
                "authorships": [],
            }
        )
        source_publication = deduplicate_publications([source_publication])[0]
        richer_abstract = (
            "This substantially richer cached abstract must remain the classification input."
        )
        cached_publication = dict(source_publication)
        cached_publication["abstract"] = richer_abstract
        cache_db.upsert_work(cached_publication)
        prediction = {
            "predictions": [
                {"sdg": {"code": "4", "name": "Quality Education"}, "prediction": 0.9}
            ]
        }
        cache_db.upsert_sdg_result(
            publication_key=source_publication["publication_key"],
            model="aurora-sdg-multi",
            sdg_response=prediction,
            sdg_formatted="90% SDG 4 (Quality Education)",
            text_hash=openalex_sdg._hash_classification_text(richer_abstract),
        )

        with (
            patch.object(
                openalex_sdg,
                "fetch_openalex_records",
                return_value=([source_publication], 1),
            ),
            patch.object(openalex_sdg, "classify_text_aurora") as classify,
        ):
            rows, _ = openalex_sdg.fetch_publications_with_sdg(
                include_openalex=True,
                dspace_sources=[],
                institution_id="https://openalex.org/I1",
                from_date="2023-01-01",
                to_date="2026-08-31",
                work_type="article",
                model="aurora-sdg-multi",
                enable_google_scholar=False,
            )

        self.assertEqual(rows[0]["abstract"], richer_abstract)
        classify.assert_not_called()

    def test_transient_classification_failure_is_retried(self) -> None:
        publication = normalize_openalex_work(
            {
                "id": "https://openalex.org/W-RETRY",
                "title": "Retry classification",
                "publication_date": "2024-01-01",
                "doi": "10.1234/retry",
                "type": "article",
                "abstract_inverted_index": {"Retry": [0], "abstract": [1]},
                "authorships": [],
                "open_access": {"is_oa": True, "oa_status": "gold"},
            }
        )
        prediction = {
            "predictions": [
                {"sdg": {"code": "4", "name": "Quality Education"}, "prediction": 0.9}
            ]
        }
        with (
            patch.object(
                openalex_sdg,
                "fetch_openalex_records",
                return_value=([publication], 1),
            ),
            patch.object(
                openalex_sdg,
                "classify_text_aurora",
                side_effect=[(None, "http_error:503"), (prediction, "")],
            ) as classify,
        ):
            kwargs = {
                "include_openalex": True,
                "dspace_sources": [],
                "institution_id": "https://openalex.org/I1",
                "from_date": "2023-01-01",
                "to_date": "2026-08-31",
                "work_type": "article",
                "model": "aurora-sdg-multi",
                "enable_google_scholar": False,
            }
            first_rows, _ = openalex_sdg.fetch_publications_with_sdg(**kwargs)
            failed_cache = cache_db.get_cached_sdg_result(
                "doi:10.1234/retry", "aurora-sdg-multi"
            )
            second_rows, _ = openalex_sdg.fetch_publications_with_sdg(**kwargs)

        self.assertEqual(first_rows[0]["sdg_note"], "http_error:503")
        self.assertIsNone(failed_cache)
        self.assertEqual(
            second_rows[0]["sdg_formatted"], "90% SDG 4 (Quality Education)"
        )
        self.assertEqual(classify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
