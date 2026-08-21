"""Unit tests for generic DSpace normalization, deduplication, and caching."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import cache_db
import openalex_sdg
from publication_sources import (
    DSpaceSource,
    OaiPmhSource,
    _reconstruct_openalex_abstract,
    deduplicate_publications,
    end_of_month,
    fetch_dspace_records,
    fetch_oai_records,
    normalize_dspace_object,
    normalize_oai_record,
    normalize_openalex_work,
    parse_dspace_sources,
    parse_oai_sources,
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


def oai_record_xml(
    *,
    identifier: str,
    datestamp: str,
    metadata: str,
    set_specs: tuple[str, ...] = (),
    deleted: bool = False,
) -> str:
    status = ' status="deleted"' if deleted else ""
    sets = "".join(f"<setSpec>{value}</setSpec>" for value in set_specs)
    metadata_container = "" if deleted else f"<metadata>{metadata}</metadata>"
    return (
        f"<record><header{status}><identifier>{identifier}</identifier>"
        f"<datestamp>{datestamp}</datestamp>{sets}</header>{metadata_container}</record>"
    )


def oai_response_xml(
    records: list[str],
    *,
    token: str = "",
    complete_list_size: int | None = None,
) -> bytes:
    size = (
        f' completeListSize="{complete_list_size}"'
        if complete_list_size is not None
        else ""
    )
    token_xml = f"<resumptionToken{size}>{token}</resumptionToken>" if token or size else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        f"<ListRecords>{''.join(records)}{token_xml}</ListRecords>"
        "</OAI-PMH>"
    ).encode("utf-8")


def oai_dc_xml(fields: str) -> str:
    return (
        '<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"{fields}</oai_dc:dc>"
    )


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}
        self.content = payload if isinstance(payload, bytes) else b""

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

    def test_parse_generic_oai_sources(self) -> None:
        sources = parse_oai_sources(
            [
                {
                    "id": "viadrina-opus",
                    "label": "Viadrina OPUS",
                    "base_url": "https://opus.example/oai/",
                    "metadata_prefix": "oai_dc",
                    "set": "open_access",
                    "publication_types": ["Article", "Book-Part"],
                    "openalex_institution_id": "https://openalex.org/I254029264",
                    "ror_id": "https://ror.org/02msan859",
                },
                {
                    "id": "disabled",
                    "base_url": "https://disabled.example/oai",
                    "enabled": False,
                },
            ]
        )

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].base_url, "https://opus.example/oai")
        self.assertEqual(sources[0].set_spec, "open_access")
        self.assertEqual(sources[0].publication_types, ("article", "book-part"))
        self.assertEqual(sources[0].openalex_query_id, "https://openalex.org/I254029264")

    def test_normalize_oai_dc_record(self) -> None:
        source = OaiPmhSource(
            id="viadrina-opus",
            label="Viadrina OPUS",
            base_url="https://opus.example/oai",
        )
        xml = oai_record_xml(
            identifier="oai:kobv.de-opus4-euv:382",
            datestamp="2026-02-19",
            set_specs=("doc-type:Book", "open_access"),
            metadata=oai_dc_xml(
                """
                <dc:title xml:lang="de">Was sind Polenstudien?</dc:title>
                <dc:creator>Flade, Falk; https://orcid.org/0000-0002-6019-3604</dc:creator>
                <dc:description xml:lang="de">Kurze deutsche Zusammenfassung.</dc:description>
                <dc:description xml:lang="en">A richer English abstract for classification.</dc:description>
                <dc:date>2017-12-15</dc:date>
                <dc:type>book</dc:type>
                <dc:identifier>https://opus.example/frontdoor/index/index/docId/382</dc:identifier>
                <dc:identifier>https://doi.org/10.11584/IPS.5</dc:identifier>
                <dc:identifier>https://opus.example/files/382/book.pdf</dc:identifier>
                <dc:language>mul</dc:language>
                <dc:rights>info:eu-repo/semantics/openAccess</dc:rights>
                """
            ),
        )
        root = ET.fromstring(oai_response_xml([xml]))
        record = root.find(
            ".//{http://www.openarchives.org/OAI/2.0/}record"
        )
        self.assertIsNotNone(record)

        normalized = normalize_oai_record(record, source)  # type: ignore[arg-type]

        self.assertEqual(normalized["source_record_id"], "oai:kobv.de-opus4-euv:382")
        self.assertEqual(normalized["type"], "book")
        self.assertEqual(normalized["doi"], "https://doi.org/10.11584/ips.5")
        self.assertEqual(normalized["record_url"], "https://opus.example/frontdoor/index/index/docId/382")
        self.assertEqual(normalized["authors"], "Flade, Falk")
        self.assertEqual(normalized["abstract"], "A richer English abstract for classification.")
        self.assertIs(normalized["is_oa"], True)

    def test_oai_harvest_follows_tokens_and_filters_publication_metadata_locally(self) -> None:
        source = OaiPmhSource(
            id="example-oai",
            label="Example OAI",
            base_url="https://repo.example/oai",
        )
        article = oai_record_xml(
            identifier="oai:example:article",
            datestamp="2026-08-01",
            set_specs=("doc-type:Article",),
            metadata=oai_dc_xml(
                "<dc:title>Current article</dc:title>"
                "<dc:creator>Doe, Jane</dc:creator>"
                "<dc:date>2024-05-10</dc:date>"
                "<dc:type>article</dc:type>"
            ),
        )
        deleted = oai_record_xml(
            identifier="oai:example:deleted",
            datestamp="2026-08-02",
            metadata="",
            deleted=True,
        )
        old_book = oai_record_xml(
            identifier="oai:example:book",
            datestamp="2026-08-03",
            set_specs=("doc-type:Book",),
            metadata=oai_dc_xml(
                "<dc:title>Old book</dc:title>"
                "<dc:date>2020</dc:date>"
                "<dc:type>book</dc:type>"
            ),
        )
        session = FakeSession(
            [
                oai_response_xml([article, deleted], token="next-page", complete_list_size=3),
                oai_response_xml([old_book]),
            ]
        )

        records, total = fetch_oai_records(
            session,
            source,
            from_date="2024-01-01",
            to_date="2024-12-31",
            work_type=["article", "book"],
            user_agent="test-agent",
        )

        self.assertEqual([record["title"] for record in records], ["Current article"])
        self.assertEqual(total, 3)
        self.assertEqual(
            session.calls[0]["params"],
            {"verb": "ListRecords", "metadataPrefix": "oai_dc"},
        )
        self.assertEqual(
            session.calls[1]["params"],
            {"verb": "ListRecords", "resumptionToken": "next-page"},
        )

    def test_oai_harvest_rejects_repeated_resumption_token(self) -> None:
        source = OaiPmhSource(
            id="looping-oai",
            label="Looping OAI",
            base_url="https://repo.example/oai",
        )
        session = FakeSession(
            [
                oai_response_xml([], token="same-token"),
                oai_response_xml([], token="same-token"),
            ]
        )

        with self.assertRaisesRegex(ValueError, "repeated an OAI-PMH resumption token"):
            fetch_oai_records(
                session,
                source,
                from_date="2024-01-01",
                to_date="2024-12-31",
                work_type=None,
                user_agent="test-agent",
            )

    def test_oai_only_publication_type_is_not_sent_to_openalex(self) -> None:
        source = OaiPmhSource(
            id="example-oai",
            label="Example OAI",
            base_url="https://repo.example/oai",
        )
        with (
            patch.object(openalex_sdg, "fetch_openalex_records") as fetch_openalex,
            patch.object(
                openalex_sdg,
                "fetch_oai_records",
                return_value=([], 0),
            ) as fetch_oai,
        ):
            rows, _ = openalex_sdg.fetch_publications_with_sdg(
                include_openalex=True,
                dspace_sources=[],
                oai_sources=[source],
                institution_id="https://openalex.org/I254029264",
                from_date="2024-01-01",
                to_date="2024-12-31",
                work_type=["preprint"],
                model="skip",
                user_agent="test-agent",
            )

        self.assertEqual(rows, [])
        fetch_openalex.assert_not_called()
        fetch_oai.assert_called_once()

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

    def test_openalex_abstract_reconstruction_joins_tokens_by_position(self) -> None:
        self.assertEqual(
            _reconstruct_openalex_abstract({"Study": [0], "solar": [1], "energy": [2]}),
            "Study solar energy",
        )
        self.assertEqual(
            _reconstruct_openalex_abstract({"energy": [2], "solar": [1], "Study": [0]}),
            "Study solar energy",
        )
        self.assertEqual(
            _reconstruct_openalex_abstract({"a": [0], "b": [2]}),
            "a b",
        )

    def test_openalex_abstract_reconstruction_concatenates_duplicate_positions(self) -> None:
        self.assertEqual(
            _reconstruct_openalex_abstract(
                {"alpha": [0], "beta": [0], "gamma": [1]}
            ),
            "alpha beta gamma",
        )
        self.assertEqual(
            _reconstruct_openalex_abstract({"alpha": [0, 0], "gamma": [1]}),
            "alpha alpha gamma",
        )
        self.assertEqual(
            _reconstruct_openalex_abstract({"alpha": [0, 1], "gamma": [2]}),
            "alpha alpha gamma",
        )

    def test_openalex_abstract_reconstruction_skips_malformed_entries(self) -> None:
        self.assertEqual(_reconstruct_openalex_abstract(None), "")
        self.assertEqual(_reconstruct_openalex_abstract(["not", "a", "mapping"]), "")
        self.assertEqual(_reconstruct_openalex_abstract({}), "")
        self.assertEqual(
            _reconstruct_openalex_abstract(
                {
                    "ok": [0],
                    "not-a-list": "no-positions",
                    "float": [1.5],
                    "negative": [-1],
                    None: [2],
                    "": [3],
                }
            ),
            "ok",
        )

    def test_duplicate_positions_survive_openalex_work_normalization(self) -> None:
        work = normalize_openalex_work(
            {
                "id": "https://openalex.org/W-DUP-POS",
                "title": "Duplicate position work",
                "publication_date": "2024-01-01",
                "doi": "10.1234/dup-pos",
                "type": "article",
                "abstract_inverted_index": {
                    "alpha": [0],
                    "beta": [0],
                    "gamma": [1],
                },
                "authorships": [],
            }
        )
        self.assertEqual(work["abstract"], "alpha beta gamma")

    def test_documented_aurora_response_is_formatted(self) -> None:
        formatted = openalex_sdg.format_sdg_predictions(
            {
                "model": "aurora-sdg-multi",
                "predictions": [
                    {
                        "prediction": 0.21,
                        "sdg": {"code": "13", "name": "Climate action"},
                    },
                    {
                        "prediction": 0.84,
                        "sdg": {"code": "4", "name": "Quality Education"},
                    },
                    {"prediction": "not-a-number", "sdg": {"code": "1"}},
                    None,
                ],
            }
        )

        self.assertEqual(
            formatted,
            "84% SDG 4 (Quality Education)\n21% SDG 13 (Climate action)",
        )

    def test_undocumented_aurora_response_shapes_are_rejected(self) -> None:
        unsupported = [
            [{"label": "SDG 4 (Quality Education)", "score": 0.84}],
            {"labels": ["SDG 4"], "scores": [0.84]},
            {"4": 0.84},
            {"results": [{"sdg": "4", "score": 0.84}]},
        ]

        for response in unsupported:
            with self.subTest(response=response):
                self.assertEqual(openalex_sdg.format_sdg_predictions(response), "")

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

        software_filter = openalex_sdg.make_filter(
            "https://openalex.org/I123",
            "2023-01-01",
            ["software"],
            "2026-08-31",
        )
        self.assertIn("type:software", software_filter)

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

    def test_aurora_retries_are_globally_rate_limited(self) -> None:
        session = Mock()
        session.post.side_effect = [
            FakeResponse({}, status_code=429),
            FakeResponse({"predictions": [{"prediction": 0.9}]}),
        ]
        limiter = Mock()

        with patch.object(openalex_sdg.time, "sleep"):
            prediction, note = openalex_sdg.classify_text_aurora(
                "aurora-sdg-multi",
                "Classification input",
                session=session,
                aurora_base_url="https://aurora.example/classify",
                retries=2,
                pause=0,
                request_limiter=limiter,
            )

        self.assertEqual(prediction, {"predictions": [{"prediction": 0.9}]})
        self.assertEqual(note, "")
        self.assertEqual(limiter.wait.call_count, 2)

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

    def test_wal_cache_uses_normal_synchronous_mode(self) -> None:
        connection = cache_db._get_conn()

        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(synchronous, 1)
        self.assertEqual(str(journal_mode).lower(), "wal")

    def test_title_only_classification_is_marked_low_confidence(self) -> None:
        publication = {
            "publication_key": "openalex:W-TITLE-ONLY",
            "source": "openalex",
            "source_record_id": "W-TITLE-ONLY",
            "source_record_key": "openalex:W-TITLE-ONLY",
            "title": "A title long enough to classify without an abstract",
            "abstract": "",
        }
        prediction = {
            "predictions": [
                {
                    "prediction": 0.9,
                    "sdg": {"code": "4", "name": "Quality Education"},
                }
            ]
        }
        with (
            patch.object(openalex_sdg, "get_cached_work", return_value=None),
            patch.object(openalex_sdg, "get_cached_sdg_result", return_value=None),
            patch.object(
                openalex_sdg,
                "classify_text_aurora",
                return_value=(prediction, ""),
            ),
            patch.object(openalex_sdg, "upsert_work"),
            patch.object(openalex_sdg, "upsert_sdg_result") as upsert_sdg,
        ):
            result = openalex_sdg._enrich_and_classify_publication(
                publication,
                session_factory=Mock,
                model="aurora-sdg-multi",
                user_agent="test-agent",
                semantic_scholar_api_key=None,
                enable_google_scholar=False,
                serpapi_api_key=None,
                aurora_limiter=Mock(),
                cancel_event=threading.Event(),
                aurora_base_url="https://aurora.example/classify",
            )

        self.assertEqual(
            result.row["sdg_note"],
            "low_confidence:title_only_no_abstract",
        )
        self.assertEqual(
            upsert_sdg.call_args.kwargs["sdg_note"],
            "low_confidence:title_only_no_abstract",
        )

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

    def test_publications_are_enriched_concurrently_with_stable_output_order(self) -> None:
        older = normalize_openalex_work(
            {
                "id": "https://openalex.org/W-OLDER-PARALLEL",
                "title": "Older parallel publication",
                "publication_date": "2024-01-01",
                "doi": "10.1234/older-parallel",
                "type": "article",
                "authorships": [],
            }
        )
        newer = normalize_openalex_work(
            {
                "id": "https://openalex.org/W-NEWER-PARALLEL",
                "title": "Newer parallel publication",
                "publication_date": "2025-01-01",
                "doi": "10.1234/newer-parallel",
                "type": "article",
                "authorships": [],
            }
        )
        barrier = threading.Barrier(2, timeout=3)
        worker_thread_ids = set()
        worker_session_ids = set()
        tracking_lock = threading.Lock()
        progress_thread_ids = []
        main_thread_id = threading.get_ident()

        def semantic_abstract(doi, *, session, api_key, on_auth_error=None):
            with tracking_lock:
                worker_thread_ids.add(threading.get_ident())
                worker_session_ids.add(id(session))
            barrier.wait()
            return f"Abstract for {doi}"

        def progress_callback(done, expected, message):
            progress_thread_ids.append(threading.get_ident())

        with (
            patch.object(
                openalex_sdg,
                "fetch_openalex_records",
                return_value=([older, newer], 2),
            ),
            patch.object(
                openalex_sdg,
                "get_abstract_from_semantic_scholar",
                side_effect=semantic_abstract,
            ),
        ):
            rows, stats = openalex_sdg.fetch_publications_with_sdg(
                include_openalex=True,
                dspace_sources=[],
                institution_id="https://openalex.org/I1",
                from_date="2023-01-01",
                to_date="2026-08-31",
                work_type="article",
                model="skip",
                enable_google_scholar=False,
                progress_callback=progress_callback,
            )

        self.assertEqual(
            [row["title"] for row in rows],
            ["Newer parallel publication", "Older parallel publication"],
        )
        self.assertEqual(stats.total_processed, 2)
        self.assertEqual(stats.ss_abstract_retrieved, 2)
        self.assertEqual(len(worker_thread_ids), 2)
        self.assertEqual(len(worker_session_ids), 2)
        self.assertTrue(progress_thread_ids)
        self.assertEqual(set(progress_thread_ids), {main_thread_id})

    def test_rejected_semantic_scholar_key_disables_later_requests(self) -> None:
        publications = [
            normalize_openalex_work(
                {
                    "id": f"https://openalex.org/W-SEMANTIC-AUTH-{index}",
                    "title": f"Semantic Scholar auth test {index}",
                    "publication_date": f"2025-01-0{index}",
                    "doi": f"10.1234/semantic-auth-{index}",
                    "type": "article",
                    "authorships": [],
                }
            )
            for index in (1, 2)
        ]

        def reject_key(doi, *, session, api_key, on_auth_error=None):
            self.assertEqual(api_key, "stale-key")
            self.assertIsNotNone(on_auth_error)
            on_auth_error(401)
            return None

        with (
            patch.object(openalex_sdg, "ENRICHMENT_MAX_WORKERS", 1),
            patch.object(
                openalex_sdg,
                "fetch_openalex_records",
                return_value=(publications, 2),
            ),
            patch.object(
                openalex_sdg,
                "get_abstract_from_semantic_scholar",
                side_effect=reject_key,
            ) as semantic_scholar,
            patch.object(
                openalex_sdg,
                "get_abstract_from_serpapi_google_scholar",
                return_value="Fallback abstract",
            ) as google_scholar,
        ):
            rows, stats = openalex_sdg.fetch_publications_with_sdg(
                include_openalex=True,
                dspace_sources=[],
                institution_id="https://openalex.org/I1",
                from_date="2023-01-01",
                to_date="2026-08-31",
                work_type="article",
                model="skip",
                semantic_scholar_api_key="stale-key",
                enable_google_scholar=True,
                serpapi_api_key="serpapi-key",
            )

        self.assertEqual(semantic_scholar.call_count, 1)
        self.assertEqual(google_scholar.call_count, 2)
        self.assertEqual(stats.semantic_scholar_auth_error_status, 401)
        self.assertEqual(stats.gs_abstract_retrieved, 2)
        self.assertEqual([row["abstract"] for row in rows], ["Fallback abstract"] * 2)

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
            patch.object(openalex_sdg, "upsert_work") as upsert,
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
        upsert.assert_not_called()

    def test_classified_publication_is_written_once_before_sdg_result(self) -> None:
        publication = normalize_openalex_work(
            {
                "id": "https://openalex.org/W-CLASSIFY-ONCE",
                "title": "Classify once",
                "publication_date": "2024-01-01",
                "doi": "10.1234/classify-once",
                "type": "article",
                "abstract_inverted_index": {"Classify": [0], "once": [1]},
                "authorships": [],
            }
        )
        prediction = {
            "predictions": [
                {"sdg": {"code": "4", "name": "Quality Education"}, "prediction": 0.9}
            ]
        }
        write_order = []

        def record_work(row):
            write_order.append("work")
            cache_db.upsert_work(row)

        def record_sdg_result(**kwargs):
            write_order.append("sdg")
            cache_db.upsert_sdg_result(**kwargs)

        with (
            patch.object(
                openalex_sdg,
                "fetch_openalex_records",
                return_value=([publication], 1),
            ),
            patch.object(
                openalex_sdg,
                "classify_text_aurora",
                return_value=(prediction, ""),
            ),
            patch.object(openalex_sdg, "upsert_work", side_effect=record_work) as upsert,
            patch.object(
                openalex_sdg,
                "upsert_sdg_result",
                side_effect=record_sdg_result,
            ),
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

        self.assertEqual(rows[0]["sdg_formatted"], "90% SDG 4 (Quality Education)")
        self.assertEqual(upsert.call_count, 1)
        self.assertEqual(write_order, ["work", "sdg"])

    def test_work_cache_change_detects_only_new_provenance_or_richer_abstract(self) -> None:
        cached = {
            "publication_key": "doi:10.1234/new-provenance",
            "source_record_keys": "openalex:W-PROVENANCE",
            "abstract": "Stable abstract",
        }
        incoming = {
            **cached,
            "source_record_keys": (
                "openalex:W-PROVENANCE; dspace:example:repository-item"
            ),
            "_source_records": [
                {"source_record_key": "openalex:W-PROVENANCE"},
                {"source_record_key": "dspace:example:repository-item"},
            ],
        }

        self.assertTrue(
            openalex_sdg._work_cache_changed(incoming, cached, "Stable abstract")
        )
        self.assertFalse(
            openalex_sdg._work_cache_changed(cached, cached, "Stable abstract")
        )
        self.assertTrue(
            openalex_sdg._work_cache_changed(
                cached,
                cached,
                "A substantially richer abstract",
            )
        )

    def test_sdg_cache_hit_with_new_provenance_writes_work_once(self) -> None:
        abstract = "Stable abstract"
        publication_key = "doi:10.1234/new-provenance"
        cached = {
            "publication_key": publication_key,
            "source_record_keys": "openalex:W-PROVENANCE",
            "title": "New provenance",
            "abstract": abstract,
        }
        incoming = {
            **cached,
            "source_record_keys": (
                "openalex:W-PROVENANCE; dspace:example:repository-item"
            ),
            "_source_records": [
                {"source_record_key": "openalex:W-PROVENANCE"},
                {"source_record_key": "dspace:example:repository-item"},
            ],
        }
        cached_prediction = {
            "predictions": [
                {"sdg": {"code": "4", "name": "Quality Education"}, "prediction": 0.9}
            ]
        }

        with (
            patch.object(openalex_sdg, "get_cached_work", return_value=cached),
            patch.object(
                openalex_sdg,
                "get_cached_sdg_result",
                return_value={
                    "text_hash": openalex_sdg._hash_classification_text(abstract),
                    "sdg_response": json.dumps(cached_prediction),
                    "sdg_formatted": "90% SDG 4 (Quality Education)",
                    "sdg_note": "",
                },
            ),
            patch.object(openalex_sdg, "classify_text_aurora") as classify,
            patch.object(openalex_sdg, "upsert_work") as upsert,
        ):
            result = openalex_sdg._enrich_and_classify_publication(
                incoming,
                session_factory=Mock,
                model="aurora-sdg-multi",
                user_agent="test-agent",
                semantic_scholar_api_key=None,
                enable_google_scholar=False,
                serpapi_api_key=None,
                aurora_limiter=Mock(),
                cancel_event=threading.Event(),
            )

        self.assertEqual(result.row["sdg_formatted"], "90% SDG 4 (Quality Education)")
        classify.assert_not_called()
        upsert.assert_called_once()

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
