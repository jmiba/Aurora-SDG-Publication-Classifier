"""Publication-source adapters and deterministic cross-source normalization."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from html import unescape
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlparse

import requests

from request_utils import request_with_backoff

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_PER_PAGE = 200
DSpaceProgressHook = Optional[Callable[[str], None]]
CancelCheck = Optional[Callable[[], bool]]
OPEN_OA_STATUSES = {"diamond", "gold", "hybrid", "green", "bronze", "open"}
DSPACE_ABSTRACT_PREFIXES = ("dc.abstract", "dc.description.abstract")
OAI_NAMESPACE = "http://www.openarchives.org/OAI/2.0/"
OAI_DC_NAMESPACE = "http://www.openarchives.org/OAI/2.0/oai_dc/"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
XML_LANG_ATTRIBUTE = "{http://www.w3.org/XML/1998/namespace}lang"
OAI_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
DEFAULT_OAI_PUBLICATION_TYPES = (
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
)


class SourceFetchCancelled(Exception):
    """Raised when a source fetch is cancelled by the caller."""


def end_of_month(value: date) -> date:
    """Return the final calendar day of the month containing value."""
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


@dataclass(frozen=True)
class DSpaceSource:
    """Configuration for one DSpace REST API installation."""

    id: str
    label: str
    base_url: str
    configuration: str = "default"
    scope: Optional[str] = None
    entity_types: Tuple[str, ...] = ("Article", "Book", "Artistic")
    openalex_institution_id: Optional[str] = None
    ror_id: Optional[str] = None

    @property
    def search_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/discover/search/objects"

    @property
    def public_base_url(self) -> str:
        return re.sub(r"/server/api/?$", "", self.base_url.rstrip("/"))

    @property
    def openalex_query_id(self) -> Optional[str]:
        """Return the preferred identifier for querying this institution in OpenAlex."""
        return self.openalex_institution_id or self.ror_id


@dataclass(frozen=True)
class OaiPmhSource:
    """Configuration for one public OAI-PMH metadata provider."""

    id: str
    label: str
    base_url: str
    metadata_prefix: str = "oai_dc"
    set_spec: Optional[str] = None
    publication_types: Tuple[str, ...] = DEFAULT_OAI_PUBLICATION_TYPES
    openalex_institution_id: Optional[str] = None
    ror_id: Optional[str] = None

    @property
    def openalex_query_id(self) -> Optional[str]:
        """Return the preferred identifier for querying this institution in OpenAlex."""
        return self.openalex_institution_id or self.ror_id


WorkTypeSelection = Optional[Union[str, Sequence[str]]]


def _valid_openalex_institution_id(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if re.fullmatch(r"(?:https?://openalex\.org/)?I[A-Z0-9]+", text, flags=re.I):
        return text
    return None


def _valid_ror_id(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if re.fullmatch(r"https?://ror\.org/[0-9a-z]{9}", text, flags=re.I):
        return text
    return None


def parse_dspace_sources(raw_sources: Any) -> List[DSpaceSource]:
    """Parse DSpace source dictionaries from TOML/Streamlit secrets data."""
    if not raw_sources:
        return []
    if isinstance(raw_sources, Mapping):
        candidates: Iterable[Any] = [raw_sources]
    elif isinstance(raw_sources, (list, tuple)):
        candidates = raw_sources
    else:
        return []

    sources: List[DSpaceSource] = []
    seen_ids = set()
    for candidate in candidates:
        try:
            values = dict(candidate)
        except (TypeError, ValueError):
            continue
        if values.get("enabled", True) is False:
            continue
        source_id = str(values.get("id") or "").strip().lower()
        label = str(values.get("label") or source_id).strip()
        base_url = str(values.get("base_url") or "").strip().rstrip("/")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", source_id):
            continue
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            continue
        if source_id in seen_ids:
            continue
        raw_entity_types = values.get("entity_types") or ("Article", "Book", "Artistic")
        entity_types = tuple(
            str(value).strip()
            for value in raw_entity_types
            if str(value).strip()
        )
        sources.append(
            DSpaceSource(
                id=source_id,
                label=label or source_id,
                base_url=base_url,
                configuration=str(values.get("configuration") or "default").strip(),
                scope=str(values.get("scope") or "").strip() or None,
                entity_types=entity_types or ("Article", "Book", "Artistic"),
                openalex_institution_id=_valid_openalex_institution_id(
                    values.get("openalex_institution_id")
                ),
                ror_id=_valid_ror_id(values.get("ror_id")),
            )
        )
        seen_ids.add(source_id)
    return sources


def parse_oai_sources(raw_sources: Any) -> List[OaiPmhSource]:
    """Parse OAI-PMH source dictionaries from TOML/Streamlit secrets data."""
    if not raw_sources:
        return []
    if isinstance(raw_sources, Mapping):
        candidates: Iterable[Any] = [raw_sources]
    elif isinstance(raw_sources, (list, tuple)):
        candidates = raw_sources
    else:
        return []

    sources: List[OaiPmhSource] = []
    seen_ids = set()
    for candidate in candidates:
        try:
            values = dict(candidate)
        except (TypeError, ValueError):
            continue
        if values.get("enabled", True) is False:
            continue
        source_id = str(values.get("id") or "").strip().lower()
        label = str(values.get("label") or source_id).strip()
        base_url = str(values.get("base_url") or "").strip().rstrip("/")
        metadata_prefix = str(values.get("metadata_prefix") or "oai_dc").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", source_id):
            continue
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            continue
        if not metadata_prefix or any(char.isspace() for char in metadata_prefix):
            continue
        if source_id in seen_ids:
            continue
        raw_types = values.get("publication_types") or DEFAULT_OAI_PUBLICATION_TYPES
        publication_types = tuple(
            dict.fromkeys(
                re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
                for value in raw_types
                if str(value).strip()
            )
        )
        sources.append(
            OaiPmhSource(
                id=source_id,
                label=label or source_id,
                base_url=base_url,
                metadata_prefix=metadata_prefix,
                set_spec=str(values.get("set") or values.get("set_spec") or "").strip()
                or None,
                publication_types=publication_types or DEFAULT_OAI_PUBLICATION_TYPES,
                openalex_institution_id=_valid_openalex_institution_id(
                    values.get("openalex_institution_id")
                ),
                ror_id=_valid_ror_id(values.get("ror_id")),
            )
        )
        seen_ids.add(source_id)
    return sources


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    decoded = unescape(str(value))
    without_tags = re.sub(r"<[^>]+>", " ", decoded)
    return re.sub(r"\s+", " ", without_tags).strip()


def _metadata_values(metadata: Mapping[str, Any], key: str) -> List[str]:
    values = metadata.get(key) or []
    if not isinstance(values, list):
        values = [values]
    result: List[str] = []
    for entry in values:
        value = entry.get("value") if isinstance(entry, Mapping) else entry
        cleaned = _clean_text(value)
        if cleaned:
            result.append(cleaned)
    return result


def normalize_doi(value: Any) -> str:
    """Return a lower-case DOI token only when it has a valid DOI shape."""
    text = _clean_text(value).strip().rstrip(".,;)")
    text = re.sub(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", "", text, flags=re.I)
    match = re.search(r"10\.\d{4,9}/\S+", text, flags=re.I)
    if not match:
        return ""
    return match.group(0).rstrip(".,;)").lower()


def _extract_doi(metadata: Mapping[str, Any]) -> str:
    for key in ("dc.identifier.doi", "dc.identifier.uri", "dc.identifier.weblink"):
        for value in _metadata_values(metadata, key):
            doi = normalize_doi(value)
            if doi:
                return doi
    return ""


def _abstract_from_metadata(metadata: Mapping[str, Any], language: str) -> str:
    ordered_keys: List[str] = []
    if language:
        ordered_keys.extend(
            f"{prefix}.{language.lower()}" for prefix in DSPACE_ABSTRACT_PREFIXES
        )
    for suffix in ("en", "pl", "author", ""):
        ordered_keys.extend(
            f"{prefix}.{suffix}" if suffix else prefix
            for prefix in DSPACE_ABSTRACT_PREFIXES
        )
    abstract_keys = sorted(
        key
        for key in metadata
        if any(key == prefix or key.startswith(f"{prefix}.") for prefix in DSPACE_ABSTRACT_PREFIXES)
    )
    ordered_keys.extend(key for key in abstract_keys if key not in ordered_keys)
    for key in ordered_keys:
        values = _metadata_values(metadata, key)
        if values:
            return max(values, key=len)
    return ""


def _stable_affiliation_id(source_id: str, name: str) -> str:
    normalized = _normalize_match_text(name)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"dspace-affiliation:{source_id}:{digest}"


def _dspace_type(entity_type: str, metadata: Mapping[str, Any]) -> str:
    raw_type = (_metadata_values(metadata, "dc.type") or [""])[0]
    compact = re.sub(r"[^a-z0-9]", "", raw_type.lower())
    mappings = {
        "journalarticle": "article",
        "article": "article",
        "monography": "book",
        "monograph": "book",
        "book": "book",
        "monographychapter": "book-chapter",
        "monographchapter": "book-chapter",
        "bookchapter": "book-chapter",
    }
    if compact in mappings:
        return mappings[compact]
    if entity_type.lower() == "artistic":
        return "artistic-work"
    if entity_type.lower() == "article":
        return "article"
    if entity_type.lower() == "book":
        return "book"
    return re.sub(r"[^a-z0-9]+", "-", entity_type.lower()).strip("-") or "other"


def _dspace_oa(metadata: Mapping[str, Any]) -> Tuple[Optional[bool], str]:
    rights = " ".join(_metadata_values(metadata, "dc.rights")).lower()
    if any(token in rights for token in ("closedaccess", "closed access")):
        return False, "closed"
    if any(
        token in rights
        for token in ("cc-by", "cc by", "creative commons", "openaccess", "open access")
    ):
        return True, "open"
    return None, "unknown"


def reconcile_oa_pair(is_oa: Any, oa_status: Any) -> Tuple[Optional[bool], str]:
    """Return an internally consistent Open Access boolean/status pair."""
    status = str(oa_status or "unknown").strip().lower() or "unknown"
    if is_oa is True:
        return True, status if status in OPEN_OA_STATUSES else "open"
    if is_oa is False:
        return False, "closed"
    if status in OPEN_OA_STATUSES:
        return True, status
    if status == "closed":
        return False, "closed"
    return None, "unknown"


def normalize_dspace_object(search_object: Mapping[str, Any], source: DSpaceSource) -> Dict[str, Any]:
    """Normalize one DSpace search result without fetching thumbnails or bitstreams."""
    item = ((search_object.get("_embedded") or {}).get("indexableObject") or {})
    metadata = item.get("metadata") or {}
    item_id = str(item.get("uuid") or item.get("id") or "").strip()
    if not item_id:
        return {}
    entity_type = str(item.get("entityType") or "").strip()
    title_values = _metadata_values(metadata, "dc.title")
    title = _clean_text(item.get("name")) or (title_values[0] if title_values else "")
    date_values = _metadata_values(metadata, "dc.date.issued")
    publication_date = date_values[0] if date_values else ""
    author_values = _metadata_values(metadata, "dc.contributor.author")
    affiliation_names = list(dict.fromkeys(_metadata_values(metadata, "dc.affiliation")))
    affiliations = [
        {
            "id": _stable_affiliation_id(source.id, name),
            "name": name,
            "country": "",
        }
        for name in affiliation_names
    ]
    language_values = _metadata_values(metadata, "dc.language")
    language = language_values[0] if language_values else ""
    abstract = _abstract_from_metadata(metadata, language)
    doi_token = _extract_doi(metadata)
    uri_values = _metadata_values(metadata, "dc.identifier.uri")
    handle = str(item.get("handle") or "").strip()
    record_url = uri_values[0] if uri_values else ""
    if not record_url and handle:
        record_url = f"{source.public_base_url}/handle/{handle}"
    is_oa, oa_status = _dspace_oa(metadata)
    source_record_key = f"dspace:{source.id}:{item_id}"
    return {
        "publication_key": source_record_key,
        "source": source.id,
        "source_label": source.label,
        "source_record_id": item_id,
        "source_record_key": source_record_key,
        "record_url": record_url,
        "openalex_id": "",
        "title": title,
        "publication_date": publication_date,
        "doi": f"https://doi.org/{doi_token}" if doi_token else "",
        "type": _dspace_type(entity_type, metadata),
        "language": language,
        "is_oa": is_oa,
        "oa_status": oa_status,
        "authors": "; ".join(author_values),
        "institutions": "; ".join(affiliation_names),
        "institution_ids": "; ".join(aff["id"] for aff in affiliations),
        "institution_countries": "; ".join("" for _ in affiliations),
        "institution_names_raw": "; ".join(affiliation_names),
        "institution_affiliations_json": json.dumps(affiliations, ensure_ascii=False),
        "abstract": abstract,
        "_raw_record": dict(item),
    }


def _oai_metadata_values(record: ET.Element) -> Dict[str, List[Dict[str, str]]]:
    values: Dict[str, List[Dict[str, str]]] = {}
    metadata = record.find(f"{{{OAI_NAMESPACE}}}metadata")
    if metadata is None:
        return values
    dc_container = metadata.find(f"{{{OAI_DC_NAMESPACE}}}dc")
    if dc_container is None:
        dc_container = next(iter(metadata), None)
    if dc_container is None:
        return values
    for element in dc_container:
        name = element.tag.rsplit("}", 1)[-1].lower()
        text = _clean_text("".join(element.itertext()))
        if not text:
            continue
        values.setdefault(name, []).append(
            {
                "value": text,
                "language": str(element.attrib.get(XML_LANG_ATTRIBUTE) or "").lower(),
            }
        )
    return values


def _oai_values(metadata: Mapping[str, Sequence[Mapping[str, str]]], key: str) -> List[str]:
    return [str(entry.get("value") or "") for entry in metadata.get(key) or [] if entry.get("value")]


def _preferred_oai_description(
    metadata: Mapping[str, Sequence[Mapping[str, str]]],
) -> str:
    descriptions = list(metadata.get("description") or [])
    english = [
        str(entry.get("value") or "")
        for entry in descriptions
        if str(entry.get("language") or "").lower() in {"en", "eng"}
    ]
    candidates = english or [str(entry.get("value") or "") for entry in descriptions]
    return max((value for value in candidates if value), key=len, default="")


def _normalize_oai_author(value: str) -> str:
    return re.sub(
        r"\s*;\s*https?://orcid\.org/[0-9X-]+\s*$",
        "",
        _clean_text(value),
        flags=re.I,
    )


def _oai_type(values: Sequence[str], set_specs: Sequence[str]) -> str:
    candidates = [*values, *(spec.split(":", 1)[1] for spec in set_specs if spec.startswith("doc-type:"))]
    mappings = {
        "article": "article",
        "journalarticle": "article",
        "contributiontoperiodical": "article",
        "book": "book",
        "monograph": "book",
        "monography": "book",
        "editedcollection": "book",
        "bookpart": "book-chapter",
        "bookchapter": "book-chapter",
        "monographchapter": "book-chapter",
        "conferenceobject": "proceedings-article",
        "conferencepaper": "proceedings-article",
        "proceedingsarticle": "proceedings-article",
        "workingpaper": "report",
        "report": "report",
        "phdthesis": "dissertation",
        "doctoralthesis": "dissertation",
        "masterthesis": "dissertation",
        "bachelorthesis": "dissertation",
        "researchdata": "dataset",
        "dataset": "dataset",
        "recension": "review",
        "review": "review",
        "preprint": "preprint",
        "other": "other",
        "periodical": "other",
        "periodicalpart": "other",
    }
    for value in candidates:
        compact = re.sub(r"[^a-z0-9]", "", value.lower().removeprefix("doc-type:"))
        if compact in mappings:
            return mappings[compact]
    return "other"


def _oai_record_url(identifiers: Sequence[str]) -> str:
    urls = [value for value in identifiers if re.match(r"https?://", value, flags=re.I)]
    for value in urls:
        if "/frontdoor/" in value.lower():
            return value
    for value in urls:
        lowered = value.lower()
        if not any(token in lowered for token in ("doi.org/", "nbn-resolving.org/", "/files/")):
            return value
    return urls[0] if urls else ""


def _oai_oa(metadata: Mapping[str, Sequence[Mapping[str, str]]], set_specs: Sequence[str]) -> Tuple[Optional[bool], str]:
    rights = " ".join(_oai_values(metadata, "rights")).lower()
    if "open_access" in set_specs or any(
        token in rights
        for token in (
            "openaccess",
            "open access",
            "creativecommons.org/licenses/",
            "creativecommons.org/publicdomain/",
        )
    ):
        return True, "open"
    if any(token in rights for token in ("closedaccess", "closed access", "restrictedaccess")):
        return False, "closed"
    return None, "unknown"


def normalize_oai_record(record: ET.Element, source: OaiPmhSource) -> Dict[str, Any]:
    """Normalize one OAI-PMH Dublin Core record into the shared publication contract."""
    header = record.find(f"{{{OAI_NAMESPACE}}}header")
    if header is None or header.attrib.get("status") == "deleted":
        return {}
    identifier = _clean_text(header.findtext(f"{{{OAI_NAMESPACE}}}identifier"))
    if not identifier:
        return {}
    datestamp = _clean_text(header.findtext(f"{{{OAI_NAMESPACE}}}datestamp"))
    set_specs = [
        _clean_text(element.text)
        for element in header.findall(f"{{{OAI_NAMESPACE}}}setSpec")
        if _clean_text(element.text)
    ]
    metadata = _oai_metadata_values(record)
    titles = _oai_values(metadata, "title")
    dates = _oai_values(metadata, "date")
    identifiers = _oai_values(metadata, "identifier")
    doi_token = next((normalize_doi(value) for value in identifiers if normalize_doi(value)), "")
    authors = [
        author
        for author in (_normalize_oai_author(value) for value in _oai_values(metadata, "creator"))
        if author
    ]
    language_values = _oai_values(metadata, "language")
    is_oa, oa_status = _oai_oa(metadata, set_specs)
    source_record_key = f"oai:{source.id}:{identifier}"
    raw_record = {
        "identifier": identifier,
        "datestamp": datestamp,
        "set_specs": set_specs,
        "metadata": metadata,
    }
    return {
        "publication_key": source_record_key,
        "source": source.id,
        "source_label": source.label,
        "source_record_id": identifier,
        "source_record_key": source_record_key,
        "record_url": _oai_record_url(identifiers),
        "openalex_id": "",
        "title": titles[0] if titles else "",
        "publication_date": dates[0] if dates else "",
        "doi": f"https://doi.org/{doi_token}" if doi_token else "",
        "type": _oai_type(_oai_values(metadata, "type"), set_specs),
        "language": language_values[0] if language_values else "",
        "is_oa": is_oa,
        "oa_status": oa_status,
        "authors": "; ".join(dict.fromkeys(authors)),
        "institutions": "",
        "institution_ids": "",
        "institution_countries": "",
        "institution_names_raw": "",
        "institution_affiliations_json": "[]",
        "abstract": _preferred_oai_description(metadata),
        "_raw_record": raw_record,
    }


def _reconstruct_openalex_abstract(inverted_index: Any) -> str:
    """Rebuild abstract text from an OpenAlex ``abstract_inverted_index``.

    Tokens are placed at their index positions and joined in position order.
    Malformed entries are skipped, except tokens that land on a position that
    already holds text: those are concatenated instead of overwriting, so a
    duplicated position never silently drops text.
    """
    if not isinstance(inverted_index, Mapping) or not inverted_index:
        return ""
    positioned: Dict[int, str] = {}
    for token, positions in inverted_index.items():
        if not isinstance(positions, list):
            continue
        token_text = "" if token is None else str(token).strip()
        if not token_text:
            continue
        for position in positions:
            if not isinstance(position, int) or position < 0:
                continue
            existing = positioned.get(position)
            positioned[position] = f"{existing} {token_text}" if existing else token_text
    return " ".join(positioned[index] for index in sorted(positioned)).strip()


def normalize_openalex_work(work: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize an OpenAlex work into the shared publication record contract."""
    openalex_id = str(work.get("id") or "").strip()
    if not openalex_id:
        return {}
    source_record_id = openalex_id.rstrip("/").split("/")[-1]
    authors: List[str] = []
    affiliations: List[Dict[str, str]] = []
    for authorship in work.get("authorships") or []:
        author_name = ((authorship.get("author") or {}).get("display_name") or "").strip()
        if author_name:
            authors.append(author_name)
        for institution in authorship.get("institutions") or []:
            name = str(institution.get("display_name") or "").strip()
            affiliations.append(
                {
                    "id": str(institution.get("id") or "").strip(),
                    "name": name,
                    "country": str(institution.get("country_code") or "").upper(),
                }
            )
    unique_affiliations: List[Dict[str, str]] = []
    seen_affiliations = set()
    for affiliation in affiliations:
        key = affiliation.get("id") or _normalize_match_text(affiliation.get("name") or "")
        if not key or key in seen_affiliations:
            continue
        seen_affiliations.add(key)
        unique_affiliations.append(affiliation)
    open_access = work.get("open_access") or {}
    doi_token = normalize_doi(work.get("doi"))
    source_record_key = f"openalex:{source_record_id}"
    return {
        "publication_key": source_record_key,
        "source": "openalex",
        "source_label": "OpenAlex",
        "source_record_id": source_record_id,
        "source_record_key": source_record_key,
        "record_url": openalex_id,
        "openalex_id": openalex_id,
        "title": work.get("display_name") or work.get("title") or "",
        "publication_date": work.get("publication_date") or "",
        "doi": f"https://doi.org/{doi_token}" if doi_token else "",
        "type": work.get("type") or "",
        "language": work.get("language") or "",
        "is_oa": open_access.get("is_oa"),
        "oa_status": open_access.get("oa_status") or "unknown",
        "authors": "; ".join(authors),
        "institutions": "; ".join(
            dict.fromkeys(aff["name"] for aff in unique_affiliations if aff.get("name"))
        ),
        "institution_ids": "; ".join(
            aff["id"] for aff in unique_affiliations if aff.get("id")
        ),
        "institution_countries": "; ".join(
            aff["country"] for aff in unique_affiliations
        ),
        "institution_names_raw": "; ".join(
            aff["name"] for aff in unique_affiliations
        ),
        "institution_affiliations_json": json.dumps(unique_affiliations, ensure_ascii=False),
        "abstract": _reconstruct_openalex_abstract(work.get("abstract_inverted_index")),
        "_raw_record": dict(work),
    }


def _ensure_not_cancelled(cancel_check: CancelCheck) -> None:
    if cancel_check and cancel_check():
        raise SourceFetchCancelled()


def _request_json(
    session: requests.Session,
    url: str,
    *,
    params: Mapping[str, Any],
    headers: Mapping[str, str],
    retries: int = 3,
) -> Dict[str, Any]:
    response = request_with_backoff(
        session,
        "get",
        url,
        params=params,
        headers=headers,
        timeout=(10, 60),
        retries=retries,
    )
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object from {url}")
    return data


def _request_xml(
    session: requests.Session,
    url: str,
    *,
    params: Mapping[str, Any],
    headers: Mapping[str, str],
    retries: int = 3,
) -> ET.Element:
    response = request_with_backoff(
        session,
        "get",
        url,
        params=params,
        headers=headers,
        timeout=(10, 60),
        retries=retries,
    )
    payload = response.content
    if len(payload) > OAI_MAX_RESPONSE_BYTES:
        raise ValueError(f"OAI-PMH response from {url} exceeds the safety limit")
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError(f"Unsafe XML declaration in OAI-PMH response from {url}")
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML response from {url}: {exc}") from exc


def _publication_date_bounds(value: Any) -> Optional[Tuple[date, date]]:
    match = re.search(r"(?<!\d)(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", str(value or ""))
    if not match:
        return None
    year = int(match.group(1))
    month_text = match.group(2)
    day_text = match.group(3)
    try:
        if not month_text:
            return date(year, 1, 1), date(year, 12, 31)
        month = int(month_text)
        if not day_text:
            return (
                date(year, month, 1),
                date(year, month, calendar.monthrange(year, month)[1]),
            )
        parsed = date(year, month, int(day_text))
        return parsed, parsed
    except ValueError:
        return None


def _publication_date_overlaps(value: Any, from_date: str, to_date: str) -> bool:
    bounds = _publication_date_bounds(value)
    if bounds is None:
        return False
    try:
        requested_start = date.fromisoformat(from_date)
        requested_end = date.fromisoformat(to_date)
    except ValueError as exc:
        raise ValueError("Publication date range must use YYYY-MM-DD") from exc
    return bounds[1] >= requested_start and bounds[0] <= requested_end


def fetch_oai_records(
    session: requests.Session,
    source: OaiPmhSource,
    *,
    from_date: str,
    to_date: str,
    work_type: WorkTypeSelection,
    user_agent: str,
    limit_rows: Optional[int] = None,
    progress_callback: DSpaceProgressHook = None,
    cancel_check: CancelCheck = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Harvest and normalize one OAI-PMH source, then filter publication dates locally."""
    if source.metadata_prefix != "oai_dc":
        raise ValueError(
            f"{source.label} uses unsupported OAI-PMH metadata prefix "
            f"{source.metadata_prefix!r}; use 'oai_dc'"
        )
    selected_types = set(_selected_work_types(work_type))
    headers = {"User-Agent": user_agent, "Accept": "application/xml, text/xml"}
    records: List[Dict[str, Any]] = []
    resumption_token = ""
    page = 0
    complete_list_size: Optional[int] = None
    seen_tokens = set()

    while True:
        _ensure_not_cancelled(cancel_check)
        page += 1
        if progress_callback:
            progress_callback(f"Fetching {source.label}: OAI-PMH page {page}")
        params: Dict[str, Any]
        if resumption_token:
            params = {"verb": "ListRecords", "resumptionToken": resumption_token}
        else:
            params = {
                "verb": "ListRecords",
                "metadataPrefix": source.metadata_prefix,
            }
            if source.set_spec:
                params["set"] = source.set_spec

        root = _request_xml(session, source.base_url, params=params, headers=headers)
        error = root.find(f"{{{OAI_NAMESPACE}}}error")
        if error is not None:
            code = str(error.attrib.get("code") or "unknown")
            if code == "noRecordsMatch":
                return [], 0
            message = _clean_text(error.text) or "Unspecified OAI-PMH error"
            raise ValueError(f"{source.label} returned OAI-PMH {code}: {message}")

        list_records = root.find(f"{{{OAI_NAMESPACE}}}ListRecords")
        if list_records is None:
            raise ValueError(f"{source.label} returned no OAI-PMH ListRecords payload")
        for record_element in list_records.findall(f"{{{OAI_NAMESPACE}}}record"):
            _ensure_not_cancelled(cancel_check)
            normalized = normalize_oai_record(record_element, source)
            if not normalized:
                continue
            if selected_types and normalized.get("type") not in selected_types:
                continue
            if not _publication_date_overlaps(
                normalized.get("publication_date"), from_date, to_date
            ):
                continue
            records.append(normalized)

        token_element = list_records.find(f"{{{OAI_NAMESPACE}}}resumptionToken")
        if token_element is not None and complete_list_size is None:
            raw_size = token_element.attrib.get("completeListSize")
            if raw_size and raw_size.isdigit():
                complete_list_size = int(raw_size)
        resumption_token = _clean_text(token_element.text if token_element is not None else "")
        if not resumption_token:
            break
        if resumption_token in seen_tokens:
            raise ValueError(f"{source.label} repeated an OAI-PMH resumption token")
        seen_tokens.add(resumption_token)

    if limit_rows is not None:
        records.sort(
            key=lambda record: (_publication_date_bounds(record.get("publication_date")) or (date.min, date.min))[1],
            reverse=True,
        )
        records = records[:limit_rows]
    return records, complete_list_size if complete_list_size is not None else len(records)


def fetch_openalex_records(
    session: requests.Session,
    *,
    filter_value: str,
    work_type: WorkTypeSelection,
    user_agent: str,
    limit_rows: Optional[int] = None,
    progress_callback: DSpaceProgressHook = None,
    cancel_check: CancelCheck = None,
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Fetch and normalize OpenAlex records without enrichment or classification."""
    selected_types = _selected_work_types(work_type)
    if selected_types == ("artistic-work",):
        return [], 0
    params: Dict[str, Any] = {
        "filter": filter_value,
        "select": "id,display_name,title,publication_date,doi,abstract_inverted_index,type,language,open_access,authorships",
        "per-page": OPENALEX_PER_PAGE,
        "cursor": "*",
    }
    headers = {"User-Agent": user_agent}
    records: List[Dict[str, Any]] = []
    total_expected: Optional[int] = None
    while params.get("cursor"):
        _ensure_not_cancelled(cancel_check)
        if progress_callback:
            progress_callback("Fetching OpenAlex records")
        data = _request_json(session, OPENALEX_WORKS_URL, params=params, headers=headers)
        meta = data.get("meta") or {}
        if total_expected is None:
            total_expected = meta.get("count") if isinstance(meta, Mapping) else None
        for work in data.get("results") or []:
            normalized = normalize_openalex_work(work)
            if normalized:
                records.append(normalized)
            if limit_rows is not None and len(records) >= limit_rows:
                return records, total_expected
        next_cursor = meta.get("next_cursor") if isinstance(meta, Mapping) else None
        if not next_cursor:
            break
        params["cursor"] = next_cursor
        time.sleep(0.2)
    return records, total_expected


def _selected_work_types(work_type: WorkTypeSelection) -> Tuple[str, ...]:
    if not work_type:
        return ()
    if isinstance(work_type, str):
        return (work_type,)
    return tuple(dict.fromkeys(str(value).strip() for value in work_type if str(value).strip()))


def _entity_types_for_work_type(source: DSpaceSource, work_type: WorkTypeSelection) -> Tuple[str, ...]:
    selected_types = _selected_work_types(work_type)
    if not selected_types:
        return source.entity_types
    mappings = {
        "article": "Article",
        "book": "Book",
        "book-chapter": "Book",
        "artistic-work": "Artistic",
    }
    desired = {mappings.get(value, value) for value in selected_types}
    return tuple(
        entity_type
        for entity_type in source.entity_types
        if entity_type in desired
        or re.sub(r"[^a-z0-9]+", "-", entity_type.lower()).strip("-")
        in selected_types
    )


def fetch_dspace_records(
    session: requests.Session,
    source: DSpaceSource,
    *,
    from_date: str,
    to_date: str,
    work_type: WorkTypeSelection,
    user_agent: str,
    limit_rows: Optional[int] = None,
    page_size: int = 100,
    progress_callback: DSpaceProgressHook = None,
    cancel_check: CancelCheck = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Fetch one DSpace source page-by-page, without media or bitstream embeds."""
    records: List[Dict[str, Any]] = []
    total_expected = 0
    selected_types = set(_selected_work_types(work_type))
    headers = {"User-Agent": user_agent, "Accept": "application/hal+json, application/json"}
    for entity_type in _entity_types_for_work_type(source, work_type):
        page = 0
        entity_record_count = 0
        while True:
            _ensure_not_cancelled(cancel_check)
            if progress_callback:
                progress_callback(f"Fetching {source.label}: {entity_type}, page {page + 1}")
            params: Dict[str, Any] = {
                "configuration": source.configuration,
                "dsoType": "item",
                "page": page,
                "size": min(max(page_size, 1), 100),
                "sort": "dc.date.issued,DESC",
                "f.dateIssued": f"[{from_date} TO {to_date}],equals",
                "f.entityType": f"{entity_type},equals",
            }
            if source.scope:
                params["scope"] = source.scope
            data = _request_json(session, source.search_url, params=params, headers=headers)
            search_result = ((data.get("_embedded") or {}).get("searchResult") or {})
            page_info = search_result.get("page") or {}
            if page == 0:
                total_expected += int(page_info.get("totalElements") or 0)
            objects = ((search_result.get("_embedded") or {}).get("objects") or [])
            for search_object in objects:
                normalized = normalize_dspace_object(search_object, source)
                if not normalized:
                    continue
                if selected_types and normalized.get("type") not in selected_types:
                    continue
                records.append(normalized)
                entity_record_count += 1
                if limit_rows is not None and entity_record_count >= limit_rows:
                    break
            total_pages = int(page_info.get("totalPages") or 0)
            page += 1
            if (
                not objects
                or page >= total_pages
                or (limit_rows is not None and entity_record_count >= limit_rows)
            ):
                break
    return records, total_expected


def _normalize_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _first_author_token(authors: str) -> str:
    first = (authors or "").split(";", 1)[0].strip()
    if not first:
        return ""
    if "," in first:
        first = first.split(",", 1)[0]
    else:
        parts = first.split()
        first = parts[-1] if parts else ""
    return _normalize_match_text(first)


def publication_deduplication_key(record: Mapping[str, Any]) -> str:
    """Build a conservative, stable key for automatic cross-source deduplication."""
    doi = normalize_doi(record.get("doi"))
    if doi:
        return f"doi:{doi}"
    title = _normalize_match_text(str(record.get("title") or ""))
    year_match = re.match(r"(\d{4})", str(record.get("publication_date") or ""))
    year = year_match.group(1) if year_match else ""
    first_author = _first_author_token(str(record.get("authors") or ""))
    if title and year and first_author:
        digest = hashlib.sha256(f"{title}\n{year}\n{first_author}".encode("utf-8")).hexdigest()
        return f"metadata:{digest}"
    return str(record.get("source_record_key") or record.get("publication_key") or "")


def _split_semicolon(value: Any) -> List[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def _merge_semicolon(left: Any, right: Any) -> str:
    return "; ".join(dict.fromkeys([*_split_semicolon(left), *_split_semicolon(right)]))


def _type_score(value: str) -> int:
    return {
        "artistic-work": 5,
        "book-chapter": 5,
        "proceedings-article": 5,
        "article": 4,
        "book": 4,
        "other": 1,
        "": 0,
    }.get(value, 3)


def _merge_publication(base: Dict[str, Any], incoming: Mapping[str, Any]) -> None:
    for field in ("title", "publication_date", "language"):
        current = str(base.get(field) or "")
        candidate = str(incoming.get(field) or "")
        if len(candidate) > len(current):
            base[field] = candidate
    if len(str(incoming.get("abstract") or "")) > len(str(base.get("abstract") or "")):
        base["abstract"] = incoming.get("abstract") or ""
    if len(_split_semicolon(incoming.get("authors"))) > len(_split_semicolon(base.get("authors"))):
        base["authors"] = incoming.get("authors") or ""
    for field in ("institutions", "institution_ids", "institution_countries", "institution_names_raw"):
        base[field] = _merge_semicolon(base.get(field), incoming.get(field))
    if _type_score(str(incoming.get("type") or "")) > _type_score(str(base.get("type") or "")):
        base["type"] = incoming.get("type") or ""
    if not base.get("doi") and incoming.get("doi"):
        base["doi"] = incoming.get("doi")
    if not base.get("openalex_id") and incoming.get("openalex_id"):
        base["openalex_id"] = incoming.get("openalex_id")

    base_oa, current_status = reconcile_oa_pair(base.get("is_oa"), base.get("oa_status"))
    incoming_oa, incoming_status = reconcile_oa_pair(
        incoming.get("is_oa"), incoming.get("oa_status")
    )
    oa_values = [base_oa, incoming_oa]
    if True in oa_values:
        base["is_oa"] = True
        open_statuses = [
            status
            for status in (current_status, incoming_status)
            if status in OPEN_OA_STATUSES
        ]
        base["oa_status"] = next(
            (status for status in open_statuses if status != "open"),
            open_statuses[0] if open_statuses else "open",
        )
    elif False in oa_values:
        base["is_oa"] = False
        base["oa_status"] = "closed"
    else:
        base["is_oa"] = None
        base["oa_status"] = "unknown"

    try:
        current_affiliations = json.loads(base.get("institution_affiliations_json") or "[]")
    except json.JSONDecodeError:
        current_affiliations = []
    try:
        incoming_affiliations = json.loads(incoming.get("institution_affiliations_json") or "[]")
    except json.JSONDecodeError:
        incoming_affiliations = []
    seen = {aff.get("id") or _normalize_match_text(aff.get("name") or "") for aff in current_affiliations}
    for affiliation in incoming_affiliations:
        key = affiliation.get("id") or _normalize_match_text(affiliation.get("name") or "")
        if key and key not in seen:
            current_affiliations.append(affiliation)
            seen.add(key)
    base["institution_affiliations_json"] = json.dumps(current_affiliations, ensure_ascii=False)


def deduplicate_publications(records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Merge exact DOI or exact title/year/first-author matches with provenance."""
    grouped: Dict[str, Dict[str, Any]] = {}
    for record in records:
        key = publication_deduplication_key(record)
        if not key:
            continue
        source_record = {
            "source": record.get("source") or "",
            "source_label": record.get("source_label") or "",
            "source_record_id": record.get("source_record_id") or "",
            "source_record_key": record.get("source_record_key") or "",
            "record_url": record.get("record_url") or "",
            "raw_record": record.get("_raw_record") or {},
        }
        if key not in grouped:
            merged = dict(record)
            merged["publication_key"] = key
            merged["_source_records"] = [source_record]
            grouped[key] = merged
        else:
            merged = grouped[key]
            _merge_publication(merged, record)
            merged["_source_records"].append(source_record)

    publications: List[Dict[str, Any]] = []
    for merged in grouped.values():
        merged["is_oa"], merged["oa_status"] = reconcile_oa_pair(
            merged.get("is_oa"), merged.get("oa_status")
        )
        source_records = merged.get("_source_records") or []
        sources = list(dict.fromkeys(record["source"] for record in source_records if record["source"]))
        source_ids = [record["source_record_id"] for record in source_records if record["source_record_id"]]
        source_keys = [record["source_record_key"] for record in source_records if record["source_record_key"]]
        record_urls = [record["record_url"] for record in source_records if record["record_url"]]
        merged["source"] = "; ".join(sources)
        merged["source_record_id"] = "; ".join(dict.fromkeys(source_ids))
        merged["source_record_keys"] = "; ".join(dict.fromkeys(source_keys))
        merged["record_url"] = record_urls[0] if record_urls else ""
        merged["record_urls"] = "; ".join(dict.fromkeys(record_urls))
        merged["source_count"] = len(sources)
        merged["source_provenance_json"] = json.dumps(
            [
                {key: value for key, value in record.items() if key != "raw_record"}
                for record in source_records
            ],
            ensure_ascii=False,
        )
        merged.pop("_raw_record", None)
        publications.append(merged)
    return publications


__all__ = [
    "DSpaceSource",
    "OaiPmhSource",
    "SourceFetchCancelled",
    "deduplicate_publications",
    "end_of_month",
    "fetch_dspace_records",
    "fetch_oai_records",
    "fetch_openalex_records",
    "normalize_doi",
    "normalize_dspace_object",
    "normalize_oai_record",
    "normalize_openalex_work",
    "parse_dspace_sources",
    "parse_oai_sources",
    "publication_deduplication_key",
    "reconcile_oa_pair",
]
