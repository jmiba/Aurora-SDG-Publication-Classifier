"""Utility functions for fetching OpenAlex works and running Aurora SDG classification."""

from __future__ import annotations

import json
import hashlib
import logging
import re
import threading
import time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html import unescape
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import requests

try:
    from scholarly import ProxyGenerator, scholarly  # type: ignore
except Exception:  # pragma: no cover - optional dependency at runtime
    ProxyGenerator = None
    scholarly = None

from cache_db import (
    get_cached_sdg_result,
    get_cached_work,
    upsert_sdg_result,
    upsert_work,
)
from publication_sources import (
    DSpaceSource,
    SourceFetchCancelled,
    WorkTypeSelection,
    deduplicate_publications,
    fetch_dspace_records,
    fetch_openalex_records,
)
from request_utils import request_with_backoff

# ------------------ CONFIG ------------------
BASE_WORKS = "https://api.openalex.org/works"
BASE_INSTITUTIONS = "https://api.openalex.org/institutions"
AURORA_BASE = "https://aurora-sdg.labs.vu.nl/classifier/classify"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract"
SERPAPI_GS_API = "https://serpapi.com/search" # New constant for SerpApi Google Scholar API


PER_PAGE = 200  # OpenAlex max
ENRICHMENT_MAX_WORKERS = 8
AURORA_MIN_INTERVAL_SECONDS = 0.12
DEFAULT_FROM_DATE = "2023-01-01"
DEFAULT_USER_AGENT = "OpenAlex+Aurora SDG fetcher (mailto:you@example.com)"

AURORA_MODELS = [
    ("aurora-sdg", "Aurora SDG mBERT (single-label, slower)"),
    ("aurora-sdg-multi", "Aurora SDG multi-label mBERT (fast)"),
    ("elsevier-sdg-multi", "Elsevier SDG multi-label mBERT (fast)"),
    ("osdg", "OSDG model (multi-label, 15 languages)"),
    ("skip", "Skip SDG classification (no Aurora API calls)"),
]

MIN_WORDS_BY_MODEL = {"osdg": 50}
HTML_TAG_RE = re.compile(r"<[^>]+>")
# --------------------------------------------

ProgressHook = Optional[Callable[[int, Optional[int], str], None]]


class FetchCancelled(Exception):
    """Raised when the fetch loop is cancelled by the user."""

    pass


@dataclass
class FetchStats:
    total_expected: Optional[int]
    total_processed: int
    openalex_abstract_missing: int
    ss_abstract_retrieved: int
    gs_abstract_retrieved: int
    total_abstracts_available: int = 0
    source_abstract_missing: int = 0
    total_source_records: int = 0
    duplicates_removed: int = 0
    sources_queried: List[str] = field(default_factory=list)


@dataclass
class _PublicationEnrichment:
    row: Dict[str, Any]
    title: str
    source_abstract_missing: int = 0
    openalex_abstract_missing: int = 0
    ss_abstract_retrieved: int = 0
    gs_abstract_retrieved: int = 0
    total_abstracts_available: int = 0


class _RateLimiter:
    """Space request starts across workers without serializing response waits."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next_start - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_start = max(self._next_start, now) + self._min_interval


_SCHOLARLY_LOCK = threading.Lock()
_AURORA_RATE_LIMITER = _RateLimiter(AURORA_MIN_INTERVAL_SECONDS)


def too_short_for_model(model: str, text: str) -> bool:
    """Return True if the supplied text lacks the minimum word count for a model."""
    need = MIN_WORDS_BY_MODEL.get(model, 0)
    return need > 0 and len((text or "").split()) < need


def is_ror_url(value: str) -> bool:
    """Lightweight validation for ROR URLs (https://ror.org/XXXXXXXXX)."""
    return bool(re.match(r"^https?://ror\.org/[0-9a-z]{9}$", value.strip(), flags=re.I))

def is_openalex_institution_id(value: str) -> bool:
    """Return True if the value looks like an OpenAlex institution ID/URL."""
    return bool(
        re.match(r"^(https?://openalex\.org/)?I[A-Z0-9]+$", value.strip(), flags=re.I)
    )

def _normalize_institution_id(value: str) -> str:
    """Return the short OpenAlex institution ID token if present."""
    if is_openalex_institution_id(value):
        return value.strip().split("/")[-1]
    return value.strip()

def search_institutions_by_name(
    name: str, user_agent: str = DEFAULT_USER_AGENT, limit: int = 10
) -> List[dict]:
    """Query the OpenAlex institutions endpoint using the user's keyword."""
    params = {"search": name, "per-page": limit}
    headers = {"User-Agent": user_agent}
    response = requests.get(BASE_INSTITUTIONS, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json().get("results", [])

def fetch_institution_lineage(
    institution_id: str,
    user_agent: str = DEFAULT_USER_AGENT,
    retries: int = 2,
    pause: float = 0.4,
) -> List[str]:
    """Fetch the institution record to read its lineage IDs."""
    inst_token = _normalize_institution_id(institution_id)
    url = f"{BASE_INSTITUTIONS}/{inst_token}"
    headers = {"User-Agent": user_agent}
    try:
        resp = request_with_backoff(
            requests,
            "get",
            url,
            headers=headers,
            timeout=20,
            retries=retries,
            base=pause,
        )
        data = resp.json()
        lineage = data.get("lineage") or []
        if isinstance(lineage, list):
            return [str(_normalize_institution_id(item)) for item in lineage if item]
    except requests.RequestException:
        pass
    return []

def flatten_authors_and_institutions(authorships: Sequence[dict]) -> Tuple[str, str, List[dict]]:
    """
    Convert OpenAlex authorship structures into 'A; B' strings and collect structured affiliations.
    Each affiliation is a dict with id, name, and country.
    """
    if not authorships:
        return "", "", []
    author_names: List[str] = []
    all_insts: List[str] = []
    affiliations: List[dict] = []
    for author_entry in authorships:
        author = (author_entry.get("author") or {}).get("display_name") or ""
        if author:
            author_names.append(author)
        for inst in author_entry.get("institutions") or []:
            name = inst.get("display_name") or ""
            if name:
                all_insts.append(name)
            affiliations.append(
                {
                    "id": inst.get("id") or "",
                    "name": name,
                    "country": (inst.get("country_code") or "").upper(),
                }
            )
    seen = set()
    inst_names: List[str] = []
    for name in all_insts:
        if name not in seen:
            seen.add(name)
            inst_names.append(name)
    return "; ".join(author_names), "; ".join(inst_names), affiliations

def clean_html_fragment(text: str) -> str:
    """Strip HTML tags/entities and normalize whitespace."""
    if not text:
        return ""
    decoded = unescape(text)
    without_tags = HTML_TAG_RE.sub(" ", decoded)
    normalized = re.sub(r"\s+", " ", without_tags)
    return normalized.strip()

def _normalize_text_for_match(text: str) -> str:
    """Normalize and lowercase text for equality checks ignoring punctuation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text).lower()
    return re.sub(r"\s+", "", text).strip()

def _title_matches(query: str, candidate: str, *, threshold: float = 0.9) -> bool:
    """Return whether two normalized titles are equal or highly similar."""
    normalized_query = _normalize_text_for_match(query)
    normalized_candidate = _normalize_text_for_match(candidate)
    if not normalized_query or not normalized_candidate:
        return False
    if normalized_query == normalized_candidate:
        return True
    return SequenceMatcher(None, normalized_query, normalized_candidate).ratio() >= threshold

def _normalize_doi(value: object) -> str:
    """Return a comparable DOI token from a DOI value or URL."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, flags=re.I)
    if not match:
        return ""
    return match.group(0).rstrip(".,;:)]}").lower()

def _serpapi_result_doi(result: Mapping[str, Any]) -> str:
    """Extract a DOI when SerpApi exposes one in structured metadata or links."""
    publication_info = result.get("publication_info")
    candidates: List[object] = [result.get("doi"), result.get("link")]
    if isinstance(publication_info, Mapping):
        candidates.extend(
            [publication_info.get("doi"), publication_info.get("summary")]
        )
    resources = result.get("resources")
    if isinstance(resources, list):
        for resource in resources:
            if isinstance(resource, Mapping):
                candidates.extend([resource.get("doi"), resource.get("link")])
    for candidate in candidates:
        normalized = _normalize_doi(candidate)
        if normalized:
            return normalized
    return ""

def _publication_year(value: object) -> str:
    """Extract a four-digit publication year from a date or metadata string."""
    match = re.search(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)", str(value or ""))
    return match.group(0) if match else ""

def _serpapi_result_year(result: Mapping[str, Any]) -> str:
    """Extract a publication year from a SerpApi Scholar result when present."""
    publication_info = result.get("publication_info")
    candidates: List[object] = [result.get("year"), result.get("publication_year")]
    if isinstance(publication_info, Mapping):
        candidates.extend(
            [publication_info.get("year"), publication_info.get("summary")]
        )
    for candidate in candidates:
        year = _publication_year(candidate)
        if year:
            return year
    return ""

def _normalize_author_token(name: str) -> str:
    """Produce a stable author token (surname or first token if comma style)."""
    if not name:
        return ""
    clean = unicodedata.normalize("NFKD", name)
    clean = "".join(ch for ch in clean if not unicodedata.combining(ch))
    has_comma = "," in clean
    clean = re.sub(r"[^\w\s]", " ", clean).lower()
    parts = clean.split()
    if not parts:
        return ""
    return parts[0] if has_comma else parts[-1]

def get_abstract_from_serpapi_google_scholar(
    title: str,
    authors: str,
    api_key: Optional[str],
    session: requests.Session,
    doi: Optional[str] = None,
    publication_year: Optional[str] = None,
    retries: int = 3,
    pause: float = 0.5,
) -> Optional[str]:
    """Fetch an abstract from a closely matching Google Scholar result."""
    if not api_key or not title:
        return None

    query = f"{title} {authors}" if authors else title
    if not query:
        return None # Should not happen if title is present but just in case

    params = {
        "engine": "google_scholar",
        "q": query,
        "api_key": api_key,
        "hl": "en", # Host language for results
        "num": 5, # Number of results, usually enough to find the paper
    }

    try:
        resp = request_with_backoff(
            session,
            "get",
            SERPAPI_GS_API,
            params=params,
            timeout=20,
            retries=retries,
            base=pause,
        )
        data = resp.json()
        expected_doi = _normalize_doi(doi)
        expected_year = _publication_year(publication_year)

        for result in data.get("organic_results", []):
            result_title = result.get("title")
            if not result_title or not _title_matches(title, result_title):
                continue
            result_doi = _serpapi_result_doi(result)
            if result_doi and result_doi != expected_doi:
                continue
            result_year = _serpapi_result_year(result)
            if result_year and result_year != expected_year:
                continue
            abstract = result.get("snippet")
            if abstract:
                logging.info("SerpApi Google Scholar abstract retrieved for '%s'", title)
                return clean_html_fragment(abstract)

        logging.info("Serpapi Google Scholar abstract not found for '%s' (no matching results with snippets)", title)
    except requests.RequestException as exc:
        logging.warning("Serpapi call failed for '%s': %s", title, exc)
    return None

def get_abstract_from_scholarly(
    title: str,
    authors: str,
    retries: int = 2,
    pause: float = 1.0,
) -> Optional[str]:
    """Fetch an abstract via scholarly using FreeProxies when SerpApi is unavailable."""
    if not title or scholarly is None or ProxyGenerator is None:
        return None

    query = f"{title} {authors}" if authors else title
    if not query:
        return None

    try:
        pg = ProxyGenerator()
        proxy_ok = pg.FreeProxies()
        if proxy_ok:
            scholarly.use_proxy(pg)
    except Exception as exc:  # pragma: no cover - network/proxy dependent
        logging.warning("scholarly FreeProxies setup failed: %s", exc)

    target_title = _normalize_text_for_match(title)
    for attempt in range(1, retries + 1):
        try:
            results = scholarly.search_pubs(query)  # type: ignore[arg-type]
            for _ in range(5):  # look at a few candidates
                try:
                    candidate = next(results)
                except StopIteration:
                    break
                cand_title = candidate.get("bib", {}).get("title") or candidate.get("name")
                if not cand_title:
                    continue
                norm_candidate = _normalize_text_for_match(cand_title)
                if not (norm_candidate.startswith(target_title) or target_title.startswith(norm_candidate)):
                    continue
                try:
                    filled = scholarly.fill(candidate)  # type: ignore[arg-type]
                except Exception as fill_exc:  # pragma: no cover - external call
                    logging.debug("scholarly.fill failed: %s", fill_exc)
                    continue
                abstract = (
                    filled.get("abstract")
                    or (filled.get("bib") or {}).get("abstract")
                )
                if abstract:
                    logging.info("scholarly abstract retrieved for '%s'", title)
                    return clean_html_fragment(abstract)
            return None
        except Exception as exc:  # pragma: no cover - external call
            logging.warning("scholarly search failed (attempt %s): %s", attempt, exc)
            if attempt == retries:
                return None
            time.sleep(pause * attempt)
    return None

def abbreviate_authors(value: str) -> str:
    """Return compact 'First Author et al.' preview for UI progress messages."""
    if not value:
        return ""
    authors = [part.strip() for part in value.split(";") if part.strip()]
    if not authors:
        return ""
    if len(authors) == 1:
        return authors[0]
    return f"{authors[0]} et al."

def make_filter(
    institution_id: str,
    from_date: Optional[str],
    work_type: WorkTypeSelection,
    to_date: Optional[str] = None,
    extra_institution_ids: Optional[Sequence[str]] = None,
) -> str:
    inst_ids = [_normalize_institution_id(institution_id)]
    for item in extra_institution_ids or []:
        norm = _normalize_institution_id(str(item))
        if norm and norm not in inst_ids:
            inst_ids.append(norm)
    ids: List[str] = []
    rors: List[str] = []
    for inst in inst_ids:
        if is_openalex_institution_id(inst):
            ids.append(inst.split("/")[-1])
        elif is_ror_url(inst):
            rors.append(inst)
    if ids:
        inst_filter = f"institutions.id:{'|'.join(ids)}"
    elif rors:
        inst_filter = f"institutions.ror:{'|'.join(rors)}"
    else:
        raise ValueError("At least one valid OpenAlex institution or ROR ID is required")
    parts = [
        inst_filter,
        "is_paratext:false",
    ]
    if from_date:
        parts.append(f"from_publication_date:{from_date}")
    if to_date:
        parts.append(f"to_publication_date:{to_date}")
    if work_type:
        selected_types = [work_type] if isinstance(work_type, str) else list(work_type)
        selected_types = list(
            dict.fromkeys(str(value).strip() for value in selected_types if str(value).strip())
        )
        if selected_types:
            parts.append(f"type:{'|'.join(selected_types)}")
    return ",".join(parts)

def classify_text_aurora(
    model: str,
    text: str,
    session: requests.Session,
    user_agent: str = DEFAULT_USER_AGENT,
    retries: int = 3,
    pause: float = 0.4,
    request_limiter: Optional[_RateLimiter] = None,
) -> Tuple[Optional[Any], str]:
    """
    Calls Aurora SDG classifier via POST, returns (json or None, note string).
    note is "" on success, or an explanation like "http_error:429" / "empty json".
    """
    if not text:
        return None, "no text"
    url = f"{AURORA_BASE}/{model}"
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {"text": text}
    try:
        resp = request_with_backoff(
            session,
            "post",
            url,
            headers=headers,
            data=json.dumps(payload),
            timeout=60,
            retries=retries,
            base=pause,
            _before_request=request_limiter.wait if request_limiter else None,
        )
        data = resp.json()
        if not data:
            return None, "empty json"
        return data, ""
    except requests.RequestException as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        return None, f"http_error:{code}"

def get_abstract_from_semantic_scholar(
    doi: str,
    session: Optional[requests.Session] = None,
    api_key: Optional[str] = None,
    retries: int = 3,
    pause: float = 0.5,
) -> Optional[str]:
    """
    Fetches abstract from Semantic Scholar using DOI.
    Returns abstract string or None on failure.
    """
    if not doi:
        return None
    cleaned_doi = doi.replace("https://doi.org/", "")
    url = SEMANTIC_SCHOLAR_API.format(doi=cleaned_doi)
    headers = {"Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    try:
        resp = request_with_backoff(
            session or requests,
            "get",
            url,
            headers=headers,
            timeout=20,
            retries=retries,
            base=pause,
        )
        data = resp.json()
        return data.get("abstract")
    except requests.RequestException:
        return None

def format_sdg_predictions(sdg_json: Optional[Any]) -> str:
    """
    Returns '\n'-joined strings like "84% SDG 10 (Reduced inequalities)".
    Handles multiple API variants.
    """

    def fmt_line(score, code, name):
        code_str = str(code).strip()
        name_str = (name or f"SDG {code_str}").strip()
        if name_str.lower().startswith("sdg "):
            return "{pct:.0f}% {label}".format(pct=score * 100, label=name_str)
        return "{pct:.0f}% SDG {code} ({name})".format(
            pct=score * 100, code=code_str, name=name_str
        )

    if not sdg_json:
        return ""

    items: List[Tuple[float, str, str]] = []

    preds = sdg_json.get("predictions") if isinstance(sdg_json, Mapping) else None
    if isinstance(preds, list) and preds:
        for entry in preds:
            if not isinstance(entry, Mapping):
                continue
            sdg = entry.get("sdg") or {}
            if not isinstance(sdg, Mapping):
                continue
            code = sdg.get("code")
            name = sdg.get("name")
            score = entry.get("prediction")
            if code is None or score is None:
                continue
            try:
                items.append((float(score), code, name))
            except (TypeError, ValueError):
                continue

    if not items and isinstance(sdg_json, list):
        for entry in sdg_json:
            if not isinstance(entry, Mapping):
                continue
            label = entry.get("label")
            score = entry.get("score")
            if label is None or score is None:
                continue
            match = re.search(r"\bSDG\s*(\d+)", str(label), flags=re.I)
            code = match.group(1) if match else ""
            try:
                items.append((float(score), code, str(label)))
            except (TypeError, ValueError):
                continue

    if (
        not items
        and isinstance(sdg_json, Mapping)
        and "labels" in sdg_json
        and "scores" in sdg_json
    ):
        labels = sdg_json.get("labels") or []
        scores = sdg_json.get("scores") or []
        for label, score in zip(labels, scores):
            match = re.search(r"\bSDG\s*(\d+)", str(label), flags=re.I)
            code = match.group(1) if match else ""
            try:
                items.append((float(score), code, str(label)))
            except (TypeError, ValueError):
                continue

    if not items and isinstance(sdg_json, Mapping):
        numeric_keys = [key for key in sdg_json.keys() if str(key).isdigit()]
        if numeric_keys:
            for key in numeric_keys:
                try:
                    items.append((float(sdg_json[key]), key, None))
                except (TypeError, ValueError):
                    continue

    if (
        not items
        and isinstance(sdg_json, Mapping)
        and isinstance(sdg_json.get("results"), list)
    ):
        for entry in sdg_json["results"]:
            if not isinstance(entry, Mapping):
                continue
            code = entry.get("sdg") or entry.get("code")
            score = entry.get("score") or entry.get("prediction")
            name = entry.get("name") or entry.get("label")
            if code is None or score is None:
                continue
            try:
                items.append((float(score), code, name))
            except (TypeError, ValueError):
                continue

    if not items:
        return ""

    items.sort(key=lambda item: item[0], reverse=True)
    return "\n".join(fmt_line(score, code, name) for score, code, name in items)

def sanitize_filename(value: str) -> str:
    """Strip unsafe characters so the filename can be used on most OSes."""
    value = unicodedata.normalize("NFKD", value)
    value = re.sub(r"[^\w\-\.]+", "_", value, flags=re.UNICODE)
    return value.strip("_")

def _hash_classification_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _publication_recency_key(publication: Mapping[str, Any]) -> Tuple[str, str]:
    """Return a source-independent key that places newer publications first."""
    return (
        str(publication.get("publication_date") or ""),
        str(publication.get("publication_key") or ""),
    )


def _enrich_and_classify_publication(
    publication_value: Mapping[str, Any],
    *,
    session_factory: Callable[[], requests.Session],
    model: str,
    user_agent: str,
    semantic_scholar_api_key: Optional[str],
    enable_google_scholar: bool,
    serpapi_api_key: Optional[str],
    aurora_limiter: _RateLimiter,
    cancel_event: threading.Event,
) -> _PublicationEnrichment:
    """Enrich and classify one publication using the current worker's session."""

    def ensure_worker_active() -> None:
        if cancel_event.is_set():
            raise FetchCancelled()

    ensure_worker_active()
    publication = dict(publication_value)
    publication_key = str(publication.get("publication_key") or "")
    title = str(publication.get("title") or "")
    authors_str = str(publication.get("authors") or "")
    doi = str(publication.get("doi") or "")
    session = session_factory()
    result = _PublicationEnrichment(row={}, title=title)

    cached_work = get_cached_work(publication_key) if publication_key else None
    cached_abstract = clean_html_fragment(str((cached_work or {}).get("abstract") or ""))
    abstract_text = clean_html_fragment(str(publication.get("abstract") or ""))
    source_abstract_missing = not bool(abstract_text)
    if len(cached_abstract) > len(abstract_text):
        abstract_text = cached_abstract
    if source_abstract_missing:
        result.source_abstract_missing = 1
        if "openalex" in str(publication.get("source") or "").split("; "):
            result.openalex_abstract_missing = 1
        if not abstract_text and doi:
            ensure_worker_active()
            semantic_abstract = get_abstract_from_semantic_scholar(
                doi,
                session=session,
                api_key=semantic_scholar_api_key,
            )
            if semantic_abstract:
                abstract_text = clean_html_fragment(semantic_abstract)
                result.ss_abstract_retrieved = 1
    if enable_google_scholar and not abstract_text:
        ensure_worker_active()
        if serpapi_api_key:
            google_abstract = get_abstract_from_serpapi_google_scholar(
                title,
                authors_str,
                api_key=serpapi_api_key,
                session=session,
                doi=doi,
                publication_year=str(publication.get("publication_date") or ""),
            )
        else:
            # scholarly mutates a module-level proxy/client, so only this fallback is
            # serialized; SerpApi and all other network stages remain parallel.
            with _SCHOLARLY_LOCK:
                ensure_worker_active()
                google_abstract = get_abstract_from_scholarly(title, authors_str)
        if google_abstract:
            abstract_text = clean_html_fragment(google_abstract)
            result.gs_abstract_retrieved = 1

    if abstract_text:
        result.total_abstracts_available = 1
    abstract_updated = bool(abstract_text and abstract_text != cached_abstract)
    text_for_sdg = abstract_text or title
    text_hash = _hash_classification_text(text_for_sdg)
    sdg_json: Optional[Any] = None
    sdg_note = ""
    sdg_formatted = ""
    cached_sdg_entry: Optional[Dict[str, Any]] = None
    reused_sdg = False

    if model == "skip":
        sdg_note = "skipped: user selected 'skip'"
    elif too_short_for_model(model, text_for_sdg):
        sdg_note = f"skipped: {model} requires >={MIN_WORDS_BY_MODEL[model]} words"
    else:
        cached_sdg_entry = get_cached_sdg_result(publication_key, model)
        cached_hash = str((cached_sdg_entry or {}).get("text_hash") or "")
        cached_response = str((cached_sdg_entry or {}).get("sdg_response") or "")
        should_reuse = bool(cached_response.strip()) and (
            cached_hash == text_hash or (not cached_hash and not abstract_updated)
        )
        if should_reuse:
            reused_sdg = True
            raw_json = cached_response
            if raw_json:
                try:
                    sdg_json = json.loads(raw_json)
                except json.JSONDecodeError:
                    sdg_json = None
            sdg_formatted = cached_sdg_entry.get("sdg_formatted") or ""
            sdg_note = cached_sdg_entry.get("sdg_note") or ""
            if not sdg_formatted and sdg_json:
                sdg_formatted = format_sdg_predictions(sdg_json)
        else:
            ensure_worker_active()
            sdg_json, sdg_note = classify_text_aurora(
                model,
                text_for_sdg,
                session=session,
                user_agent=user_agent,
                request_limiter=aurora_limiter,
            )
            sdg_formatted = format_sdg_predictions(sdg_json) if sdg_json else ""
            # The canonical work must exist before its FK-bound SDG result.
            publication["abstract"] = abstract_text
            upsert_work(publication)
            if sdg_json is not None:
                upsert_sdg_result(
                    publication_key=publication_key,
                    model=model,
                    sdg_response=sdg_json,
                    sdg_formatted=sdg_formatted,
                    sdg_note=sdg_note,
                    text_hash=text_hash,
                )

    sdg_raw = json.dumps(sdg_json, ensure_ascii=False) if sdg_json is not None else ""
    if reused_sdg and not sdg_raw and cached_sdg_entry:
        sdg_raw = cached_sdg_entry.get("sdg_response") or ""

    row_data = {
        key: value for key, value in publication.items() if not str(key).startswith("_")
    }
    row_data.update(
        {
            "abstract": abstract_text,
            "sdg_model": model,
            "sdg_response": sdg_raw,
            "sdg_formatted": sdg_formatted,
            "sdg_note": sdg_note,
        }
    )
    publication_for_cache = dict(row_data)
    publication_for_cache["_source_records"] = publication.get("_source_records") or []
    upsert_work(publication_for_cache)
    result.row = row_data
    return result


def fetch_publications_with_sdg(
    *,
    include_openalex: bool,
    dspace_sources: Sequence[DSpaceSource],
    institution_id: Optional[str],
    from_date: str,
    work_type: WorkTypeSelection,
    model: str,
    to_date: Optional[str] = None,
    limit_rows: Optional[int] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    semantic_scholar_api_key: Optional[str] = None,
    enable_google_scholar: bool = True,
    serpapi_api_key: Optional[str] = None,
    extra_institution_ids: Optional[Sequence[str]] = None,
    progress_callback: ProgressHook = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[List[Dict[str, object]], FetchStats]:
    """Fetch selected sources, deduplicate, then enrich and classify once per work."""
    end_date = to_date or from_date
    stats = FetchStats(
        total_expected=None,
        total_processed=0,
        openalex_abstract_missing=0,
        ss_abstract_retrieved=0,
        gs_abstract_retrieved=0,
    )
    source_records: List[Dict[str, Any]] = []

    def ensure_not_cancelled() -> None:
        if cancel_check and cancel_check():
            raise FetchCancelled()

    def source_progress(message: str) -> None:
        ensure_not_cancelled()
        if progress_callback:
            progress_callback(0, None, message)

    with requests.Session() as session:
        try:
            if include_openalex:
                if not institution_id:
                    raise ValueError("institution_id is required when OpenAlex is selected")
                selected_types = (
                    [work_type]
                    if isinstance(work_type, str)
                    else list(work_type or [])
                )
                openalex_types = [
                    value for value in selected_types if value != "artistic-work"
                ]
                if not selected_types or openalex_types:
                    stats.sources_queried.append("OpenAlex")
                    institution_ids = [institution_id, *(extra_institution_ids or [])]
                    openalex_ids = list(
                        dict.fromkeys(
                            value
                            for value in institution_ids
                            if is_openalex_institution_id(value)
                        )
                    )
                    ror_ids = list(
                        dict.fromkeys(
                            value for value in institution_ids if is_ror_url(value)
                        )
                    )
                    for identifier_group in (openalex_ids, ror_ids):
                        if not identifier_group:
                            continue
                        openalex_filter = make_filter(
                            identifier_group[0],
                            from_date,
                            openalex_types or None,
                            end_date,
                            extra_institution_ids=identifier_group[1:],
                        )
                        openalex_records, _ = fetch_openalex_records(
                            session,
                            filter_value=openalex_filter,
                            work_type=openalex_types or None,
                            user_agent=user_agent,
                            limit_rows=limit_rows,
                            progress_callback=source_progress,
                            cancel_check=cancel_check,
                        )
                        source_records.extend(openalex_records)

            for source in dspace_sources:
                stats.sources_queried.append(source.label)
                records, _ = fetch_dspace_records(
                    session,
                    source,
                    from_date=from_date,
                    to_date=end_date,
                    work_type=work_type,
                    user_agent=user_agent,
                    limit_rows=limit_rows,
                    progress_callback=source_progress,
                    cancel_check=cancel_check,
                )
                source_records.extend(records)
        except SourceFetchCancelled as exc:
            raise FetchCancelled() from exc

        ensure_not_cancelled()
        stats.total_source_records = len(source_records)
        publications = deduplicate_publications(source_records)
        stats.duplicates_removed = len(source_records) - len(publications)
        publications.sort(key=_publication_recency_key, reverse=True)
        if limit_rows is not None:
            publications = publications[:limit_rows]
        stats.total_expected = len(publications)

        rows_by_index: List[Optional[Dict[str, Any]]] = [None] * len(publications)
        if publications:
            cancel_event = threading.Event()
            aurora_limiter = _AURORA_RATE_LIMITER
            worker_local = threading.local()
            worker_sessions: List[requests.Session] = []
            worker_sessions_lock = threading.Lock()

            def worker_session() -> requests.Session:
                worker_session_value = getattr(worker_local, "session", None)
                if worker_session_value is None:
                    worker_session_value = requests.Session()
                    worker_local.session = worker_session_value
                    with worker_sessions_lock:
                        worker_sessions.append(worker_session_value)
                return worker_session_value

            executor = ThreadPoolExecutor(
                max_workers=min(ENRICHMENT_MAX_WORKERS, len(publications)),
                thread_name_prefix="publication-enrichment",
            )
            pending = set()
            try:
                future_to_index = {}
                for index, publication in enumerate(publications):
                    ensure_not_cancelled()
                    future = executor.submit(
                        _enrich_and_classify_publication,
                        publication,
                        session_factory=worker_session,
                        model=model,
                        user_agent=user_agent,
                        semantic_scholar_api_key=semantic_scholar_api_key,
                        enable_google_scholar=enable_google_scholar,
                        serpapi_api_key=serpapi_api_key,
                        aurora_limiter=aurora_limiter,
                        cancel_event=cancel_event,
                    )
                    future_to_index[future] = index
                pending = set(future_to_index)

                while pending:
                    ensure_not_cancelled()
                    completed, pending = wait(
                        pending,
                        timeout=0.1,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in completed:
                        enrichment = future.result()
                        rows_by_index[future_to_index[future]] = enrichment.row
                        stats.source_abstract_missing += enrichment.source_abstract_missing
                        stats.openalex_abstract_missing += enrichment.openalex_abstract_missing
                        stats.ss_abstract_retrieved += enrichment.ss_abstract_retrieved
                        stats.gs_abstract_retrieved += enrichment.gs_abstract_retrieved
                        stats.total_abstracts_available += (
                            enrichment.total_abstracts_available
                        )
                        stats.total_processed += 1
                        if progress_callback:
                            detail = (
                                enrichment.title
                                if len(enrichment.title) <= 120
                                else f"{enrichment.title[:117]}..."
                            )
                            progress_callback(
                                stats.total_processed,
                                stats.total_expected,
                                detail,
                            )
            except BaseException:
                cancel_event.set()
                for future in pending:
                    future.cancel()
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
                for worker_session_value in worker_sessions:
                    worker_session_value.close()

        rows = [row for row in rows_by_index if row is not None]

    if progress_callback:
        progress_callback(stats.total_processed, stats.total_expected, "Completed")
    return rows, stats


def fetch_works_with_sdg(
    institution_id: str,
    from_date: str,
    work_type: Optional[str],
    model: str,
    to_date: Optional[str] = None,
    limit_rows: Optional[int] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    semantic_scholar_api_key: Optional[str] = None,
    enable_google_scholar: bool = True,
    serpapi_api_key: Optional[str] = None,
    extra_institution_ids: Optional[Sequence[str]] = None,
    progress_callback: ProgressHook = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[List[Dict[str, object]], FetchStats]:
    """Backward-compatible OpenAlex-only wrapper around the multi-source pipeline."""
    return fetch_publications_with_sdg(
        include_openalex=True,
        dspace_sources=[],
        institution_id=institution_id,
        from_date=from_date,
        work_type=work_type,
        model=model,
        to_date=to_date,
        limit_rows=limit_rows,
        user_agent=user_agent,
        semantic_scholar_api_key=semantic_scholar_api_key,
        enable_google_scholar=enable_google_scholar,
        serpapi_api_key=serpapi_api_key,
        extra_institution_ids=extra_institution_ids,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )


__all__ = [
    "AURORA_MODELS",
    "DEFAULT_FROM_DATE",
    "DEFAULT_USER_AGENT",
    "FetchCancelled",
    "FetchStats",
    "fetch_institution_lineage",
    "SEMANTIC_SCHOLAR_API",
    "SERPAPI_GS_API",
    "fetch_works_with_sdg",
    "fetch_publications_with_sdg",
    "format_sdg_predictions",
    "is_openalex_institution_id",
    "is_ror_url",
    "sanitize_filename",
    "search_institutions_by_name",
]
