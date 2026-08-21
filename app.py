import csv
import io
import itertools
import json
import math
import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
)

import altair as alt
import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from openalex_sdg import (
    AURORA_MODELS,
    DEFAULT_USER_AGENT,
    OPENALEX_WORK_TYPES,
    FetchCancelled,
    FetchStats,
    fetch_institution_lineage,
    fetch_publications_with_sdg,
    is_openalex_institution_id,
    is_ror_url,
    sanitize_filename,
    scholarly_fallback_available,
    search_institutions_by_name,
)
from publication_sources import (
    DSpaceSource,
    OaiPmhSource,
    end_of_month,
    parse_dspace_sources,
    parse_oai_sources,
)

SECRET_HTTP_USER_AGENT = "http_user_agent"
SECRET_AURORA_BASE_URL = "aurora_base_url"
SECRET_SEMANTIC_SCHOLAR_KEY = "semantic_scholar_api_key"
SECRET_GOOGLE_SCHOLAR_ENABLED = "google_scholar_enabled"
SECRET_DEFAULT_START = "advanced_options.default_from_date"
SECRET_SERPAPI_KEY = "serpapi_api_key" # New constant for SerpApi Key
_SECRETS: Dict[str, Any] = {}
PREVIEW_COLUMNS = [
    "record_url",
    "source",
    "authors",
    "title",
    "publication_date",
    "type",
    "doi",
    "institutions",
]
PREVIEW_PAGE_SIZE = 25
SPHERE_LATITUDE_STEPS = 16
SPHERE_LONGITUDE_STEPS = 32
MAX_SECONDARY_NETWORK_EDGES = 20
CSV_FIELDNAMES = [
    "publication_key",
    "source",
    "source_count",
    "source_record_id",
    "source_record_keys",
    "record_url",
    "record_urls",
    "openalex_id",
    "authors",
    "title",
    "publication_date",
    "doi",
    "type",
    "language",
    "is_oa",
    "oa_status",
    "institutions",
    "institution_ids",
    "institution_countries",
    "institution_names_raw",
    "abstract",
    "sdg_model",
    "sdg_response",
    "sdg_formatted",
    "sdg_note",
    "source_provenance_json",
]
RESULT_SESSION_KEY = "fetch_result"
RESULT_SCHEMA_VERSION = 3
SDG_THRESHOLD_PERCENT = 3.0
OA_STATUS_ORDER = ["diamond", "gold", "hybrid", "green", "bronze", "open", "closed", "unknown"]
OA_STATUS_COLORS = {
    "diamond": "#7dd3fc",
    "gold": "#facc15",
    "hybrid": "#efa046",
    "green": "#22c55e",
    "bronze": "#cd7f32",
    "closed": "#6b7280",
    "open": "#0c6b2f",
    "unknown": "#94a3b8",
}


@dataclass(frozen=True)
class QuerySelection:
    """Validated query and service configuration for one fetch run."""

    include_openalex: bool
    dspace_sources: Tuple[DSpaceSource, ...]
    institution_id: Optional[str]
    institution_ids: Tuple[str, ...]
    include_lineage: bool
    cached_lineage: Tuple[str, ...]
    publication_types: Tuple[str, ...]
    model: str
    from_date: str
    to_date: str
    limit_rows: Optional[int]
    user_agent: str
    semantic_scholar_api_key: Optional[str]
    google_scholar_enabled: bool
    serpapi_api_key: Optional[str]
    aurora_base_url: Optional[str]
    oai_sources: Tuple[OaiPmhSource, ...] = ()
SDG_COLORS = {
    "1": "#e5243b",   # No Poverty
    "2": "#dda63a",   # Zero Hunger
    "3": "#4c9f38",   # Good Health and Well-being
    "4": "#c5192d",   # Quality Education
    "5": "#ff3a21",   # Gender Equality
    "6": "#26bde2",   # Clean Water and Sanitation
    "7": "#fcc30b",   # Affordable and Clean Energy
    "8": "#a21942",   # Decent Work and Economic Growth
    "9": "#fd6925",   # Industry, Innovation and Infrastructure
    "10": "#dd1367",  # Reduced Inequalities
    "11": "#fd9d24",  # Sustainable Cities and Communities
    "12": "#bf8b2e",  # Responsible Consumption and Production
    "13": "#3f7e44",  # Climate Action
    "14": "#0a97d9",  # Life Below Water
    "15": "#56c02b",  # Life on Land
    "16": "#00689d",  # Peace, Justice and Strong Institutions
    "17": "#19486a",  # Partnerships for the Goals
}
RADIO_CHECKBOX_CSS = """
<style>
div[data-testid="stDataFrame"] div[role="checkbox"] input[type="checkbox"] {
    appearance: none;
    -webkit-appearance: none;
    width: 1rem;
    height: 1rem;
    border: 2px solid var(--primary-color);
    border-radius: 50%;
    position: relative;
    cursor: pointer;
}
div[data-testid="stDataFrame"] div[role="checkbox"] input[type="checkbox"]:checked {
    background-color: var(--primary-color);
}
div[data-testid="stDataFrame"] div[role="checkbox"] input[type="checkbox"]:checked::after {
    content: "";
    position: absolute;
    top: 0.15rem;
    left: 0.15rem;
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    background-color: white;
}
</style>
"""


def _load_secrets() -> Dict[str, Any]:
    """Load secrets from Streamlit or fallback TOML files once per process."""
    if _SECRETS:
        return _SECRETS
    try:
        for key, value in st.secrets.items():
            _SECRETS[key] = value
    except Exception:
        pass
    if _SECRETS:
        return _SECRETS
    candidate_paths = [
        Path(".streamlit/secrets.toml"),
        Path.home() / ".streamlit" / "secrets.toml",
    ]
    for path in candidate_paths:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
                if isinstance(data, dict):
                    _SECRETS.update(data)
        except Exception:
            continue
    return _SECRETS


def get_secret_text(name: str) -> Optional[str]:
    """Return a string secret (supports dotted section.key names)."""
    if "." in name:
        section, key = name.split(".", 1)
        raw_value = (_load_secrets().get(section) or {}).get(key)
    else:
        raw_value = _load_secrets().get(name)
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    return text


def get_secret_bool(name: str) -> Optional[bool]:
    """Interpret a secret value as a boolean if possible."""
    text = get_secret_text(name)
    if text is None:
        return None
    value = text.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def resolve_user_agent() -> Tuple[str, bool]:
    """Return the HTTP user agent and flag whether it came from secrets."""
    secret_value = get_secret_text(SECRET_HTTP_USER_AGENT)
    if secret_value:
        return secret_value.strip(), True
    return DEFAULT_USER_AGENT, False


def has_contact_user_agent(value: str) -> bool:
    """Return whether a User-Agent contains a non-placeholder contact email."""
    match = re.search(r"mailto:([^\s)]+@[^\s)]+)", value or "", flags=re.I)
    if not match:
        return False
    domain = match.group(1).rsplit("@", 1)[-1].lower()
    return domain not in {"example.com", "example.org", "example.net"} and not domain.endswith(
        ".invalid"
    )


def resolve_aurora_base_url() -> Optional[str]:
    """Read the configured Aurora classifier base URL."""
    value = get_secret_text(SECRET_AURORA_BASE_URL)
    return value.rstrip("/") if value else None


def resolve_semantic_scholar_key() -> Optional[str]:
    """Read the Semantic Scholar API key (if supplied)."""
    return get_secret_text(SECRET_SEMANTIC_SCHOLAR_KEY)


def resolve_google_scholar_enabled() -> bool:
    """Decide whether Google Scholar lookups are allowed."""
    value = get_secret_bool(SECRET_GOOGLE_SCHOLAR_ENABLED)
    if value is None:
        return False
    return value


def render_google_scholar_status(enabled: bool, serpapi_api_key: Optional[str]) -> None:
    """Describe the active Google Scholar lookup path."""
    if not enabled:
        return
    if serpapi_api_key:
        st.info(
            "Google Scholar abstract lookups enabled (via SerpApi).",
            icon=":material/check_circle:",
        )
    elif scholarly_fallback_available():
        st.warning(
            "Google Scholar abstract lookups enabled, but `serpapi_api_key` is not "
            "set. Using the optional scholarly free-proxy fallback (less reliable "
            "and slower)."
        )
    else:
        st.warning(
            "Google Scholar abstract lookups enabled, but neither `serpapi_api_key` "
            "nor the optional scholarly fallback is available. Lookups will be "
            "skipped; install `requirements-scholarly.txt` to enable the fallback."
        )

def resolve_serpapi_key() -> Optional[str]:
    """Read the SerpApi API key (if supplied)."""
    return get_secret_text(SECRET_SERPAPI_KEY)


def resolve_dspace_sources() -> List[DSpaceSource]:
    """Load tracked public sources, with optional secrets-based additions/overrides."""
    public_sources: List[DSpaceSource] = []
    config_path = Path("dspace_sources.toml")
    if config_path.is_file():
        try:
            with config_path.open("rb") as handle:
                public_config = tomllib.load(handle)
            public_sources = parse_dspace_sources(public_config.get("dspace_sources"))
        except (OSError, tomllib.TOMLDecodeError):
            public_sources = []
    secret_sources = parse_dspace_sources(_load_secrets().get("dspace_sources"))
    sources_by_id = {source.id: source for source in public_sources}
    sources_by_id.update({source.id: source for source in secret_sources})
    return list(sources_by_id.values())


def resolve_oai_sources() -> List[OaiPmhSource]:
    """Load tracked public OAI-PMH sources, with secrets-based additions/overrides."""
    public_sources: List[OaiPmhSource] = []
    config_path = Path("oai_sources.toml")
    if config_path.is_file():
        try:
            with config_path.open("rb") as handle:
                public_config = tomllib.load(handle)
            public_sources = parse_oai_sources(public_config.get("oai_sources"))
        except (OSError, tomllib.TOMLDecodeError):
            public_sources = []
    secret_sources = parse_oai_sources(_load_secrets().get("oai_sources"))
    sources_by_id = {source.id: source for source in public_sources}
    sources_by_id.update({source.id: source for source in secret_sources})
    return list(sources_by_id.values())


def build_preview_rows(
    rows: List[Dict[str, Any]],
    columns: List[str],
    limit: int = 20,
    offset: int = 0,
) -> List[Dict[str, str]]:
    """Create a lightweight list of dictionaries for the preview table."""
    preview: List[Dict[str, str]] = []
    subset = rows[offset : offset + max(limit, 0)]
    for row in subset:
        preview.append({col: str(row.get(col, "") or "") for col in columns})
    return preview


def abbreviate_authors(value: str) -> str:
    """Return 'First Author et al.' style preview for long author lists."""
    if not value:
        return ""
    authors = [part.strip() for part in value.split(";") if part.strip()]
    if not authors:
        return ""
    if len(authors) == 1:
        return authors[0]
    return f"{authors[0]} et al."


def parse_sdg_formatted(value: str) -> List[Tuple[str, float, str]]:
    """Parse the stored SDG formatted string into tuples."""
    entries: List[Tuple[str, float, str]] = []
    if not value:
        return entries
    for line in value.splitlines():
        match = re.search(r"(\d+(?:\.\d+)?)%\s*(?:SDG\s*)?(\d+)(?:\s*\(([^)]+)\))?", line, re.I)
        if not match:
            continue
        pct = float(match.group(1))
        code = match.group(2)
        name = match.group(3) or ""
        entries.append((code, pct, name))
    return entries


def aggregate_sdg_counts(rows: List[Dict[str, Any]]) -> List[Tuple[str, str, float]]:
    """Combine SDG percentages across all rows, normalize to 100%, keep codes for coloring."""
    totals: Dict[str, float] = {}
    labels: Dict[str, str] = {}
    for row in rows:
        formatted = row.get("sdg_formatted") or ""
        for code, pct, name in parse_sdg_formatted(formatted):
            if pct < SDG_THRESHOLD_PERCENT:
                continue
            label = f"SDG {code}"
            if name:
                label = f"{label} ({name})"
            labels.setdefault(code, label)
            totals[code] = totals.get(code, 0.0) + pct
    sorted_totals = sorted(totals.items(), key=lambda pair: pair[1], reverse=True)
    total_pct = sum(value for _, value in sorted_totals)
    if total_pct <= 0:
        return []
    return [
        (code, labels.get(code, f"SDG {code}"), (value / total_pct) * 100.0)
        for code, value in sorted_totals
    ]


def render_sdg_pie_chart(data: List[Tuple[str, str, float]], title: str):
    """Display an Altair donut chart summarizing SDG distribution."""
    if not data:
        st.info(f"No SDG predictions available for {title.lower()}.")
        return
    df = pd.DataFrame(data, columns=["code", "SDG", "Value"])
    label_by_code: Dict[str, str] = {}
    for code, label, _ in data:
        label_by_code.setdefault(code, label)

    def _code_sort_key(code: str) -> Tuple[int, str]:
        return (int(code), code) if code.isdigit() else (99, code)

    ordered_codes = sorted(label_by_code.keys(), key=_code_sort_key)
    domain = [label_by_code[code] for code in ordered_codes]
    colors = [SDG_COLORS.get(code, "#9ca3af") for code in ordered_codes]
    chart = (
        alt.Chart(df)
        .mark_arc(innerRadius=70)
        .encode(
            theta="Value",
            color=alt.Color(
                "SDG",
                scale=alt.Scale(domain=domain, range=colors),
                legend=alt.Legend(columns=1, labelLimit=300, titleLimit=300, title="Sustainable Development Goals"),
            ),
            tooltip=[
                alt.Tooltip("SDG", title="Sustainable Development Goal"),
                alt.Tooltip("Value", format=".1f", title="Concordance in %"),
            ],
        )
        .properties(width=1650, height=450, title=title)
    )
    st.altair_chart(chart, width="stretch")


def render_oa_ring_chart(rows: List[Dict[str, Any]]) -> None:
    """Show a ring chart without conflating missing OA metadata with closed access."""
    counts = {"Open access": 0, "Closed": 0, "Unknown": 0}
    truthy = {"1", "true", "yes", "y", "t"}
    for row in rows:
        is_oa = row.get("is_oa")
        if isinstance(is_oa, bool):
            counts["Open access" if is_oa else "Closed"] += 1
            continue
        if is_oa is None or is_oa == "":
            counts["Unknown"] += 1
            continue
        normalized = str(is_oa).strip().lower()
        if normalized in truthy:
            counts["Open access"] += 1
        elif normalized in {"0", "false", "no", "n", "f"}:
            counts["Closed"] += 1
        else:
            counts["Unknown"] += 1
    total = sum(counts.values())
    if total == 0:
        st.info("No records contain an `is_oa` flag.")
        return
    chart_df = pd.DataFrame(
        [
            {"label": label, "count": value, "share": value / total}
            for label, value in counts.items()
            if value > 0
        ]
    )

    colors = {"Open access": "#0c6b2f", "Closed": "#6b7280", "Unknown": "#94a3b8"}
    chart = (
        alt.Chart(chart_df)
        .mark_arc(innerRadius=70)
        .encode(
            theta=alt.Theta("count:Q", title="Publications"),
            color=alt.Color(
                "label:N",
                scale=alt.Scale(domain=list(colors.keys()), range=[colors[key] for key in colors]),
                legend=alt.Legend(title="Access status", columns=1),
            ),
            tooltip=[
                alt.Tooltip("label:N", title="Access"),
                alt.Tooltip("count:Q", title="Publications"),
                alt.Tooltip("share:Q", title="Share", format=".1%"),
            ],
        )
        .properties(width=1650, height=450, title="Open access vs closed")
    )
    st.altair_chart(chart, width="stretch")

@dataclass
class _SphereMeshGeometry:
    """Vertex and triangle arrays for all institution spheres in one Plotly mesh."""

    x: List[float]
    y: List[float]
    z: List[float]
    i: List[int]
    j: List[int]
    k: List[int]
    intensity: List[float]
    hover_text: List[str]


def _edge_endpoints_outside_spheres(
    start: Tuple[float, float, float],
    end: Tuple[float, float, float],
    start_radius: float,
    end_radius: float,
) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    """Clip a 3D edge so it meets the surfaces of its spherical nodes."""
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    delta_z = end[2] - start[2]
    distance = math.sqrt(delta_x**2 + delta_y**2 + delta_z**2)
    if distance <= 0:
        return start, end

    direction = (delta_x / distance, delta_y / distance, delta_z / distance)
    maximum_offset = distance * 0.35
    start_offset = min(maximum_offset, max(0.0, start_radius))
    end_offset = min(maximum_offset, max(0.0, end_radius))
    clipped_start = (
        start[0] + direction[0] * start_offset,
        start[1] + direction[1] * start_offset,
        start[2] + direction[2] * start_offset,
    )
    clipped_end = (
        end[0] - direction[0] * end_offset,
        end[1] - direction[1] * end_offset,
        end[2] - direction[2] * end_offset,
    )
    return clipped_start, clipped_end


def _build_sphere_mesh(
    node_positions: Mapping[str, Tuple[float, float, float]],
    node_radii: Mapping[str, float],
    node_degrees: Mapping[str, int],
    *,
    latitude_steps: int = SPHERE_LATITUDE_STEPS,
    longitude_steps: int = SPHERE_LONGITUDE_STEPS,
) -> _SphereMeshGeometry:
    """Build a compact multi-sphere triangle mesh with per-node hover metadata."""
    geometry = _SphereMeshGeometry([], [], [], [], [], [], [], [])
    for node in sorted(node_positions):
        center_x, center_y, center_z = node_positions[node]
        radius = node_radii[node]
        degree_value = node_degrees.get(node, 1)
        hover_text = f"{node} ({degree_value} co-affiliations)"
        vertex_offset = len(geometry.x)

        for latitude_index in range(latitude_steps + 1):
            latitude = -math.pi / 2 + math.pi * latitude_index / latitude_steps
            ring_radius = radius * math.cos(latitude)
            ring_z = center_z + radius * math.sin(latitude)
            for longitude_index in range(longitude_steps):
                longitude = 2 * math.pi * longitude_index / longitude_steps
                geometry.x.append(center_x + ring_radius * math.cos(longitude))
                geometry.y.append(center_y + ring_radius * math.sin(longitude))
                geometry.z.append(ring_z)
                geometry.intensity.append(float(degree_value))
                geometry.hover_text.append(hover_text)

        for latitude_index in range(latitude_steps):
            lower_ring = vertex_offset + latitude_index * longitude_steps
            upper_ring = lower_ring + longitude_steps
            for longitude_index in range(longitude_steps):
                next_longitude = (longitude_index + 1) % longitude_steps
                lower = lower_ring + longitude_index
                lower_next = lower_ring + next_longitude
                upper = upper_ring + longitude_index
                upper_next = upper_ring + next_longitude
                geometry.i.extend([lower, lower_next])
                geometry.j.extend([upper, upper])
                geometry.k.extend([lower_next, upper_next])
    return geometry


def _network_edge_groups(
    edge_counts: Mapping[Tuple[str, str], int],
    selected_label: str,
    min_secondary_weight: int,
) -> Tuple[Dict[Tuple[str, str], int], List[Tuple[Tuple[str, str], int]]]:
    """Split origin links from ranked links between the origin's partners."""
    return _network_edge_groups_for_origins(
        edge_counts,
        {selected_label},
        min_secondary_weight,
    )


def _network_edge_groups_for_origins(
    edge_counts: Mapping[Tuple[str, str], int],
    selected_labels: Set[str],
    min_secondary_weight: int,
) -> Tuple[Dict[Tuple[str, str], int], List[Tuple[Tuple[str, str], int]]]:
    """Split links from all selected origins and links between their partners."""
    primary_edges = {
        edge: weight
        for edge, weight in edge_counts.items()
        if selected_labels.intersection(edge)
    }
    neighbors: Set[str] = set()
    for a, b in primary_edges:
        neighbors.update((a, b))
    neighbors.difference_update(selected_labels)
    secondary_edges = sorted(
        (
            (edge, weight)
            for edge, weight in edge_counts.items()
            if edge[0] in neighbors
            and edge[1] in neighbors
            and weight >= min_secondary_weight
        ),
        key=lambda item: (-item[1], item[0]),
    )
    return primary_edges, secondary_edges


def _network_labels_matching_query(
    labels: Sequence[str], query: str
) -> Set[str]:
    """Return labels matching any comma-separated, case-insensitive substring."""
    terms = [term.strip().casefold() for term in query.split(",") if term.strip()]
    if not terms:
        return set(labels)
    return {
        label
        for label in labels
        if any(term in label.casefold() for term in terms)
    }


def _network_label_style(theme_type: str) -> Dict[str, Any]:
    """Return an accessible annotation palette for the active Streamlit theme."""
    if str(theme_type).casefold() == "dark":
        return {
            "font_color": "#f8fafc",
            "background_color": "rgba(15,23,42,0.88)",
            "border_color": "rgba(196,181,253,0.65)",
        }
    return {
        "font_color": "#111827",
        "background_color": "rgba(255,255,255,0.90)",
        "border_color": "rgba(77,31,227,0.55)",
    }


def render_institution_network(
    rows: List[Dict[str, Any]],
    start_date: str,
    end_date: str,
    selected_institution_id: Optional[str],
    max_nodes: int = 30,
    min_second_level_weight: int = 2,
    max_secondary_edges: int = MAX_SECONDARY_NETWORK_EDGES,
    theme_type: Optional[str] = None,
    selected_institution_ids: Sequence[str] = (),
) -> None:
    """Render a simple 3D co-affiliation network of institutions from the selected period."""
    if not rows:
        st.info("No publications available to display the co-affiliation network.")
        return
    df = pd.DataFrame(rows)
    if df.empty:
        st.info("No publications available to display the co-affiliation network.")
        return

    start_dt = pd.to_datetime(start_date, errors="coerce")
    end_dt = pd.to_datetime(end_date, errors="coerce")
    if pd.isna(start_dt) or pd.isna(end_dt):
        st.info("Unable to determine the selected time frame for the network.")
        return
    if "publication_date" in df.columns:
        df["pub_date"] = pd.to_datetime(df["publication_date"], errors="coerce")
    else:
        df["pub_date"] = pd.NaT
    df = df.dropna(subset=["pub_date"])
    if df.empty:
        st.info("No publications have valid dates for the network.")
        return
    df = df[(df["pub_date"] >= start_dt) & (df["pub_date"] <= end_dt)]
    if df.empty:
        st.info("No publications fall within the selected time frame for the network.")
        return

    edge_counts: Dict[Tuple[str, str], int] = {}
    inst_labels: Dict[str, str] = {}
    inst_countries: Dict[str, str] = {}
    eligible_publication_rows = 0
    for _, row in df.iterrows():
        aff_json = row.get("institution_affiliations_json")
        try:
            affiliations = json.loads(aff_json) if aff_json else []
        except Exception:
            affiliations = []
        # fallback: build from columns if json missing
        if not affiliations:
            raw_ids = str(row.get("institution_ids") or "").split(";")
            raw_names = str(row.get("institution_names_raw") or row.get("institutions") or "").split(";")
            raw_countries = str(row.get("institution_countries") or "").split(";")
            affiliations = []
            for idx, inst_id in enumerate(raw_ids):
                inst_id = inst_id.strip()
                if not inst_id:
                    continue
                name = raw_names[idx].strip() if idx < len(raw_names) else ""
                country = raw_countries[idx].strip() if idx < len(raw_countries) else ""
                affiliations.append({"id": inst_id, "name": name, "country": country})
        unique_ids = []
        for aff in affiliations:
            inst_id = aff.get("id") or ""
            if not inst_id:
                continue
            if inst_id not in unique_ids:
                unique_ids.append(inst_id)
            name = aff.get("name") or ""
            country = (aff.get("country") or "").upper()
            if name:
                inst_labels.setdefault(inst_id, name)
            if country:
                inst_countries.setdefault(inst_id, country)
        if len(unique_ids) < 2:
            continue
        eligible_publication_rows += 1
        for a, b in itertools.combinations(sorted(unique_ids), 2):
            edge_counts[(a, b)] = edge_counts.get((a, b), 0) + 1

    st.caption(
        "Network coverage: "
        f"{eligible_publication_rows:,} of {len(df):,} publications contain at "
        "least two institution identifiers. The graph uses the combined result "
        "set; records without affiliation pairs—including many OAI-PMH Dublin "
        "Core records—remain in the preview, other charts, and exports but cannot "
        "form co-affiliation edges."
    )

    if not edge_counts:
        st.info(
            "No co-affiliations found to build a network. At least two structured "
            "institution identifiers must occur on the same publication."
        )
        return

    # Collapse IDs that share the same label to avoid duplicate-looking nodes.
    id_to_label: Dict[str, str] = {}
    edge_institution_ids = {
        inst_id for edge in edge_counts for inst_id in edge
    }
    for inst_id in edge_institution_ids:
        country_code = inst_countries.get(inst_id)
        base_label = inst_labels.get(inst_id) or inst_id.split('/')[-1] or inst_id
        if country_code:
            base_label = f"{base_label} ({country_code})"
        id_to_label[inst_id] = base_label

    label_edge_counts: Dict[Tuple[str, str], int] = {}
    for (a, b), w in edge_counts.items():
        la = id_to_label.get(a, a)
        lb = id_to_label.get(b, b)
        if la == lb:
            continue
        key = (la, lb) if la < lb else (lb, la)
        label_edge_counts[key] = label_edge_counts.get(key, 0) + w

    if not label_edge_counts:
        st.info("No co-affiliations found to build a network.")
        return

    # If selected institutions occur in the data, retain links from every origin.
    configured_institution_ids = list(
        dict.fromkeys(
            institution_id
            for institution_id in (
                selected_institution_id,
                *selected_institution_ids,
            )
            if institution_id
        )
    )
    id_alias_to_label: Dict[str, str] = {}
    for institution_id, label in id_to_label.items():
        normalized_id = institution_id.rstrip("/")
        id_alias_to_label[institution_id] = label
        id_alias_to_label[normalized_id] = label
        id_alias_to_label[normalized_id.split("/")[-1]] = label
    selected_labels: Set[str] = set()
    for institution_id in configured_institution_ids:
        selected_label = (
            id_alias_to_label.get(institution_id)
            or id_alias_to_label.get(institution_id.rstrip("/"))
            or id_alias_to_label.get(institution_id.rstrip("/").split("/")[-1])
        )
        if selected_label:
            selected_labels.add(selected_label)
    if selected_labels:
        primary_edges, secondary_edges = _network_edge_groups_for_origins(
            label_edge_counts,
            selected_labels,
            min_second_level_weight,
        )
        if primary_edges:
            show_secondary_edges = False
            if secondary_edges:
                show_secondary_edges = st.toggle(
                    "Show connections between partner institutions",
                    value=False,
                    key="show_secondary_coaffiliations",
                    help=(
                        "Primary edges connect the selected institutions to their "
                        "partners. Enable this to add the strongest links between "
                        "those partner institutions."
                    ),
                )
            visible_secondary_edges = (
                secondary_edges[: max(0, max_secondary_edges)]
                if show_secondary_edges
                else []
            )
            label_edge_counts = {
                **primary_edges,
                **dict(visible_secondary_edges),
            }
            if show_secondary_edges:
                st.caption(
                    "Showing "
                    f"{len(visible_secondary_edges):,} of "
                    f"{len(secondary_edges):,} eligible partner-to-partner "
                    "connections, ranked by co-authored works."
                )

    degree: Dict[str, int] = {}
    for (a, b), w in label_edge_counts.items():
        degree[a] = degree.get(a, 0) + w
        degree[b] = degree.get(b, 0) + w

    node_filter = st.text_input(
        "Filter nodes by label",
        key="network_node_label_filter",
        placeholder="e.g. Warsaw, Berlin or University A, University B",
        help=(
            "Enter one or more comma-separated name fragments. Matching is "
            "case-insensitive and happens before the top-node limit is applied."
        ),
    )
    candidate_nodes = _network_labels_matching_query(list(degree), node_filter)
    if node_filter.strip():
        candidate_nodes.update(selected_labels.intersection(degree))

    # Limit by degree after label filtering, but keep every selected origin.
    top_nodes = set(
        sorted(
            candidate_nodes,
            key=lambda node: degree[node],
            reverse=True,
        )[:max_nodes]
    )
    top_nodes.update(selected_labels.intersection(degree))
    filtered_edges = {
        (a, b): w
        for (a, b), w in label_edge_counts.items()
        if a in top_nodes and b in top_nodes
    }
    filter_status_message: Optional[str] = None
    if not filtered_edges:
        if node_filter.strip():
            filter_status_message = (
                "No connected institution labels match this filter. "
                "Try a different or broader name fragment."
            )
            top_nodes = selected_labels.intersection(degree)
        else:
            st.info("Co-affiliations exist but were filtered out by the top-n limit.")
            return
    if node_filter.strip() and filtered_edges:
        visible_partner_count = len(top_nodes - selected_labels)
        st.caption(
            f"Label filter retained {visible_partner_count:,} matching institution"
            f"{'s' if visible_partner_count != 1 else ''}"
            + (
                f" plus {len(selected_labels):,} selected origin"
                f"{'s' if len(selected_labels) != 1 else ''}."
                if selected_labels
                else "."
            )
        )

    # Use a spring layout to keep connected nodes closer together.
    G = nx.Graph()
    for node in top_nodes:
        G.add_node(node)
    for (a, b), w in filtered_edges.items():
        G.add_edge(a, b, weight=w)

    pos = nx.spring_layout(
        G,
        weight="weight",
        dim=3,
        center=(0, 0, 0),
        seed=42,
    )
    if len(selected_labels) == 1:
        selected_label = next(iter(selected_labels))
        if selected_label in pos:
            pos[selected_label] = [0.0, 0.0, 0.0]

    node_positions = {
        node: tuple((coords.tolist() if hasattr(coords, "tolist") else list(coords))[:3])
        for node, coords in pos.items()
    }

    max_deg = max((degree.get(node, 1) for node in top_nodes), default=1)
    desired_node_radii = {
        node: 0.03 + (degree.get(node, 1) / max_deg) * 0.075
        for node in top_nodes
    }
    minimum_neighbor_distances = {node: math.inf for node in top_nodes}
    for a, b in filtered_edges:
        a_position = node_positions[a]
        b_position = node_positions[b]
        distance = math.sqrt(
            sum(
                (b_position[index] - a_position[index]) ** 2
                for index in range(3)
            )
        )
        minimum_neighbor_distances[a] = min(minimum_neighbor_distances[a], distance)
        minimum_neighbor_distances[b] = min(minimum_neighbor_distances[b], distance)
    node_radii = {
        node: min(desired_node_radii[node], minimum_neighbor_distances[node] * 0.3)
        for node in top_nodes
    }

    edge_traces = []
    for (a, b), w in filtered_edges.items():
        edge_start, edge_end = _edge_endpoints_outside_spheres(
            node_positions[a],
            node_positions[b],
            node_radii[a],
            node_radii[b],
        )
        x0, y0, z0 = edge_start
        x1, y1, z1 = edge_end
        # Keep a single co-authorship prominent, then scale repeated links
        # logarithmically so high-weight edges do not dominate the network.
        weight_scale = math.log2(max(1, w))
        width = min(10.0, 4.0 + 2.0 * weight_scale)
        alpha = min(0.96, 0.82 + 0.05 * weight_scale)
        mid_x = (x0 + x1) / 2
        mid_y = (y0 + y1) / 2
        mid_z = (z0 + z1) / 2
        edge_color = f"rgba(140,140,140,{alpha})"
        edge_traces.append(
            go.Scatter3d(
                x=[x0, mid_x, x1, None],
                y=[y0, mid_y, y1, None],
                z=[z0, mid_z, z1, None],
                mode="lines",
                line=dict(color=edge_color, width=width),
                hoverinfo="text",
                text=["", f"Co-authored works: {w}", "", ""],
                hoverlabel=dict(bgcolor="#f2f2f2", font=dict(color="#000000")),
            )
        )

    figure_data = list(edge_traces)
    if top_nodes:
        sphere_geometry = _build_sphere_mesh(node_positions, node_radii, degree)
        figure_data.append(
            go.Mesh3d(
                x=sphere_geometry.x,
                y=sphere_geometry.y,
                z=sphere_geometry.z,
                i=sphere_geometry.i,
                j=sphere_geometry.j,
                k=sphere_geometry.k,
                intensity=sphere_geometry.intensity,
                intensitymode="vertex",
                colorscale=[[0, "#b4a4e8"], [1, "#4d1fe3"]],
                cmin=0,
                cmax=max_deg,
                showscale=True,
                colorbar=dict(title="Degree"),
                opacity=1.0,
                flatshading=False,
                lighting=dict(
                    ambient=0.55,
                    diffuse=0.9,
                    specular=0.35,
                    roughness=0.55,
                    fresnel=0.15,
                ),
                lightposition=dict(x=100, y=200, z=300),
                hovertext=sphere_geometry.hover_text,
                hoverinfo="text",
                name="Institutions",
            )
        )
    active_theme_type = theme_type or st.context.theme.type or "light"
    label_style = _network_label_style(active_theme_type)
    node_annotations = [
        dict(
            x=node_positions[node][0],
            y=node_positions[node][1],
            z=node_positions[node][2],
            text=node,
            showarrow=False,
            xanchor="center",
            yanchor="bottom",
            yshift=8,
            font=dict(size=14, color=label_style["font_color"], weight=300),
            bgcolor=label_style["background_color"],
            bordercolor=label_style["border_color"],
            borderwidth=1,
            borderpad=3,
        )
        for node in sorted(top_nodes)
    ]
    fig = go.Figure(data=figure_data)
    fig.update_layout(
        showlegend=False,
        uirevision="institution-network",
        margin=dict(l=0, r=0, t=0, b=0),
        scene=dict(
            uirevision="institution-network",
            xaxis=dict(visible=False),
            yaxis=dict(visible=False),
            zaxis=dict(visible=False),
            annotations=node_annotations,
        ),
        height=650,
    )
    if filter_status_message:
        fig.add_annotation(
            x=0.5,
            y=0.06,
            xref="paper",
            yref="paper",
            text=filter_status_message,
            showarrow=False,
            font=dict(size=13, color=label_style["font_color"]),
            bgcolor=label_style["background_color"],
            bordercolor=label_style["border_color"],
            borderwidth=1,
            borderpad=4,
        )
    st.plotly_chart(fig, width="stretch", key="institution_network_chart")

def render_author_oa_chart(
    rows: List[Dict[str, Any]], start_date: str, end_date: str, max_authors: int = 20
) -> None:
    """Show top authors by OA availability across the selected window."""
    if not rows:
        st.info("No publications available to display per-author OA status.")
        return
    df = pd.DataFrame(rows)
    if df.empty:
        st.info("No publications available to display per-author OA status.")
        return

    start_month = pd.to_datetime(start_date, errors="coerce")
    end_month = pd.to_datetime(end_date, errors="coerce")
    if pd.isna(start_month) or pd.isna(end_month):
        st.info("Unable to determine the selected time frame for author distribution.")
        return
    start_month = start_month.to_period("M").to_timestamp()
    end_month = end_month.to_period("M").to_timestamp()
    if start_month > end_month:
        start_month, end_month = end_month, start_month

    if "publication_date" in df.columns:
        df["pub_date"] = pd.to_datetime(df["publication_date"], errors="coerce")
    else:
        df["pub_date"] = pd.NaT
    if df["pub_date"].isna().all() and "publication_year" in df.columns:
        df["pub_date"] = pd.to_datetime(df["publication_year"].astype(str), format="%Y", errors="coerce")
    df = df.dropna(subset=["pub_date"])
    if df.empty:
        st.info("No publications have a valid publication date for this time frame.")
        return
    df["pub_month"] = df["pub_date"].dt.to_period("M").dt.to_timestamp()
    df = df[(df["pub_month"] >= start_month) & (df["pub_month"] <= end_month)]
    if df.empty:
        st.info("No publications fall within the selected publication period.")
        return

    if "authors" not in df.columns:
        st.info("No author information available in these records.")
        return
    df["authors"] = df["authors"].fillna("").astype(str)
    exploded = (
        df.assign(author=df["authors"].str.split(";"))
        .explode("author")
        .assign(author=lambda d: d["author"].str.strip())
    )
    exploded = exploded[exploded["author"] != ""]
    if exploded.empty:
        st.info("No author information is available to build this chart.")
        return
    if "is_oa" not in exploded.columns:
        exploded["oa_access_label"] = "Unknown"
    else:
        def oa_access_label(value: Any) -> str:
            if isinstance(value, bool):
                return "Open access" if value else "Closed"
            if value is None or str(value).strip() == "":
                return "Unknown"
            normalized = str(value).strip().lower()
            if normalized in {"1", "true", "yes", "y", "t"}:
                return "Open access"
            if normalized in {"0", "false", "no", "n", "f"}:
                return "Closed"
            return "Unknown"

        exploded["oa_access_label"] = exploded["is_oa"].apply(oa_access_label)
    grouped = (
        exploded.groupby(["author", "oa_access_label"], dropna=False)
        .size()
        .reset_index(name="count")
    )
    if grouped.empty:
        st.info("No author publication counts available.")
        return
    grouped["oa_status"] = grouped["oa_access_label"]
    totals = grouped.groupby("author")["count"].sum().nlargest(max_authors)
    grouped = grouped[grouped["author"].isin(totals.index)]
    grouped["author"] = pd.Categorical(
        grouped["author"], categories=totals.index.tolist(), ordered=True
    )
    ordered_statuses = ["Open access", "Closed", "Unknown"]
    order_mapping = {status: idx for idx, status in enumerate(ordered_statuses)}
    grouped["status_order"] = grouped["oa_status"].map(order_mapping)
    color_range = ["#0c6b2f", "#6b7280", "#94a3b8"]

    chart = (
        alt.Chart(grouped)
        .mark_bar()
        .encode(
            x=alt.X("count:Q", title="Publications", axis=alt.Axis(format="d")),
            y=alt.Y(
                "author:N",
                title="Author",
                sort=totals.index.tolist()[::-1],
            ),
            color=alt.Color(
                "oa_status:N",
                title="Open-Access status",
                scale=alt.Scale(domain=ordered_statuses, range=color_range),
            ),
            order=alt.Order("status_order:Q", sort="descending"),
            tooltip=[
                alt.Tooltip("author:N", title="Author"),
                alt.Tooltip("oa_status:N", title="OA status"),
                alt.Tooltip("count:Q", title="Publications"),
            ],
        )
        .properties(
            width=1650,
            height=max(300, 40 * len(totals)),
            title=f"OA status distribution for top {len(totals)} authors",
        )
    )
    st.altair_chart(chart, width="stretch")
    st.caption("Authors ranked by number of publications in the selected period.")


def render_oa_status_chart(rows: List[Dict[str, Any]], start_date: str, end_date: str):
    """Plot stacked OA status counts per month for the selected period."""
    if not rows:
        st.info("No publications available in the selected time frame.")
        return
    df = pd.DataFrame(rows)
    if df.empty:
        st.info("No publications available in the selected time frame.")
        return

    start_month = pd.to_datetime(start_date, errors="coerce")
    end_month = pd.to_datetime(end_date, errors="coerce")
    if pd.isna(start_month) or pd.isna(end_month):
        st.info("Unable to determine the selected time frame.")
        return
    start_month = start_month.to_period("M").to_timestamp()
    end_month = end_month.to_period("M").to_timestamp()
    if start_month > end_month:
        start_month, end_month = end_month, start_month

    if "publication_date" in df.columns:
        df["pub_date"] = pd.to_datetime(df["publication_date"], errors="coerce")
    else:
        df["pub_date"] = pd.NaT
    if df["pub_date"].isna().all() and "publication_year" in df.columns:
        df["pub_date"] = pd.to_datetime(df["publication_year"].astype(str), format="%Y", errors="coerce")
    df = df.dropna(subset=["pub_date"])
    if df.empty:
        st.info("No publications have a valid publication date for this time frame.")
        return

    df["pub_month"] = df["pub_date"].dt.to_period("M").dt.to_timestamp()
    df = df[(df["pub_month"] >= start_month) & (df["pub_month"] <= end_month)]
    if df.empty:
        st.info("No publications fall within the selected publication period.")
        return
    if "oa_status" not in df.columns:
        df["oa_status"] = "unknown"
    else:
        df["oa_status"] = df["oa_status"].fillna("unknown")
        df.loc[df["oa_status"].astype(str).str.strip() == "", "oa_status"] = "unknown"

    grouped = (
        df.groupby(["pub_month", "oa_status"], dropna=False)
        .size()
        .reset_index(name="count")
    )
    if grouped.empty:
        st.info("No publications fall within the selected publication period.")
        return

    month_range = pd.date_range(start=start_month, end=end_month, freq="MS")
    status_values = sorted(grouped["oa_status"].unique())
    scaffold = (
        pd.DataFrame({"pub_month": month_range})
        .assign(key=1)
        .merge(pd.DataFrame({"oa_status": status_values, "key": 1}), on="key")
        .drop(columns="key")
    )
    chart_df = scaffold.merge(grouped, on=["pub_month", "oa_status"], how="left").fillna({"count": 0})
    chart_df["count"] = chart_df["count"].astype(int)
    ordered_statuses = [
        *[status for status in OA_STATUS_ORDER if status in status_values],
        *[status for status in status_values if status not in OA_STATUS_ORDER],
    ]
    order_mapping = {status: idx for idx, status in enumerate(ordered_statuses)}
    chart_df["status_order"] = chart_df["oa_status"].map(order_mapping).fillna(len(order_mapping)).astype(int)
    color_range = [OA_STATUS_COLORS.get(status, "#94a3b8") for status in ordered_statuses]

    chart = (
        alt.Chart(chart_df)
        .mark_bar()
        .encode(
            x=alt.X("yearmonth(pub_month):T", title="Publication month"),
            y=alt.Y("count:Q", stack="zero", title="Publications", axis=alt.Axis(format="d")),
            color=alt.Color(
                "oa_status:N",
                title="Open-Access status",
                scale=alt.Scale(domain=ordered_statuses, range=color_range),
            ),
            order=alt.Order("status_order:Q", sort="descending"),
            tooltip=[
                alt.Tooltip("yearmonth(pub_month):T", title="Month"),
                alt.Tooltip("oa_status:N", title="OA status"),
                alt.Tooltip("count:Q", title="Publications"),
            ],
        )
        .properties(
            width=1650,
            height=400,
            title=f"Publications from {start_month:%b %Y} to {end_month:%b %Y}",
        )
    )
    st.altair_chart(chart, width="stretch")


def render_publication_type_chart(rows: List[Dict[str, Any]], start_date: str, end_date: str):
    """Render a pie chart showing publication types in the window."""
    if not rows:
        st.info("No publications available to display publication types.")
        return
    df = pd.DataFrame(rows)
    if df.empty:
        st.info("No publications available to display publication types.")
        return

    start_month = pd.to_datetime(start_date, errors="coerce")
    end_month = pd.to_datetime(end_date, errors="coerce")
    if pd.isna(start_month) or pd.isna(end_month):
        st.info("Unable to determine the selected time frame to calculate publication types.")
        return
    start_month = start_month.to_period("M").to_timestamp()
    end_month = end_month.to_period("M").to_timestamp()
    if start_month > end_month:
        start_month, end_month = end_month, start_month

    if "publication_date" in df.columns:
        df["pub_date"] = pd.to_datetime(df["publication_date"], errors="coerce")
    else:
        df["pub_date"] = pd.NaT
    if df["pub_date"].isna().all() and "publication_year" in df.columns:
        df["pub_date"] = pd.to_datetime(df["publication_year"].astype(str), format="%Y", errors="coerce")
    df = df.dropna(subset=["pub_date"])
    if df.empty:
        st.info("No publications have a valid publication date for this time frame.")
        return

    df["pub_month"] = df["pub_date"].dt.to_period("M").dt.to_timestamp()
    df = df[(df["pub_month"] >= start_month) & (df["pub_month"] <= end_month)]
    if df.empty:
        st.info("No publications fall within the selected publication period.")
        return

    if "type" not in df.columns:
        st.info("No publication type information is available to build this chart.")
        return
    df["type"] = df["type"].fillna("").astype(str).str.strip()
    df.loc[df["type"] == "", "type"] = "unknown"

    grouped = (
        df.groupby("type", dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
    )
    if grouped.empty:
        st.info("No publication type data available for this period.")
        return
    grouped["percentage"] = grouped["count"] / grouped["count"].sum() * 100

    chart = (
        alt.Chart(grouped)
        .mark_arc(innerRadius=70)
        .encode(
            theta=alt.Theta("count:Q", title="Publications"),
            color=alt.Color(
                "type:N",
                title="Publication type",
                legend=alt.Legend(columns=2, labelLimit=240),
            ),
            tooltip=[
                alt.Tooltip("type:N", title="Publication type"),
                alt.Tooltip("count:Q", format="d", title="Publications"),
                alt.Tooltip("percentage:Q", format=".1f", title="Share (%)"),
            ],
        )
        .properties(
            width=1650,
            height=450,
            title="Publication type distribution in the selected time frame",
        )
    )
    st.altair_chart(chart, width="stretch")

def build_output_filename(
    source_ids: Sequence[str],
    institution_id: Optional[str],
    work_types: Optional[Sequence[str]],
    model: str,
    from_date: str,
    to_date: Optional[str],
    limit_rows: Optional[int],
) -> str:
    """Generate a descriptive filename that encodes filters and limits."""
    source_part = "-".join(source_ids) or "publications"
    inst_tail = institution_id.rstrip("/").split("/")[-1] if institution_id else "all"
    type_part = "-".join(work_types or []) or "all"
    model_part = model if model != "skip" else "no-sdg"
    fname = f"{source_part}_{inst_tail}_{type_part}_{model_part}_{from_date}"
    if to_date and to_date != from_date:
        fname += f"_to{to_date}"
    if limit_rows:
        fname += f"_n{limit_rows}"
    return sanitize_filename(f"{fname}.csv")


def rows_to_csv_bytes(rows: List[Dict[str, Any]]) -> bytes:
    """Serialize result rows into UTF-8 encoded CSV bytes."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDNAMES)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in CSV_FIELDNAMES})
    buffer.seek(0)
    return buffer.getvalue().encode("utf-8")


def rows_to_excel_bytes(rows: List[Dict[str, Any]], columns: Optional[List[str]] = None) -> bytes:
    """Serialize rows to a standards-compliant XLSX workbook."""
    selected_columns = CSV_FIELDNAMES if columns is None else columns
    if not selected_columns:
        selected_columns = list(dict.fromkeys(key for row in rows for key in row))
    dataframe = pd.DataFrame(rows, columns=selected_columns)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        dataframe.to_excel(writer, index=False)
        # Publication metadata is external input. Keep formula-looking strings as
        # literal text rather than executable spreadsheet formulas.
        worksheet = writer.book.active
        for row in worksheet.iter_rows(min_row=2):
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    return buffer.getvalue()


def render_source_selector(
    dspace_sources: Sequence[DSpaceSource],
    oai_sources: Sequence[OaiPmhSource],
) -> Tuple[bool, List[DSpaceSource], List[OaiPmhSource]]:
    """Let one automated run query OpenAlex and configured repository sources."""
    st.header("Query setup", divider="rainbow")
    st.subheader("1. Publication sources", divider="violet")
    option_labels = {"openalex": "OpenAlex"}
    option_labels.update({f"dspace:{source.id}": source.label for source in dspace_sources})
    option_labels.update({f"oai:{source.id}": source.label for source in oai_sources})
    selected_keys = st.multiselect(
        "Sources to query",
        options=list(option_labels),
        default=["openalex"],
        format_func=lambda key: option_labels[key],
        help="All selected sources are fetched automatically, normalized, and deduplicated before classification.",
    )
    selected_dspace_ids = {
        key.split(":", 1)[1] for key in selected_keys if key.startswith("dspace:")
    }
    selected_dspace = [source for source in dspace_sources if source.id in selected_dspace_ids]
    selected_oai_ids = {
        key.split(":", 1)[1] for key in selected_keys if key.startswith("oai:")
    }
    selected_oai = [source for source in oai_sources if source.id in selected_oai_ids]
    if not dspace_sources and not oai_sources:
        st.caption(
            "No repository sources are configured; add DSpace or OAI-PMH source entries."
        )
    return "openalex" in selected_keys, selected_dspace, selected_oai


def render_institution_selector(user_agent: str) -> Tuple[Optional[str], bool]:
    """Show the institution search box, lineage toggle, and return (institution_id, include_lineage)."""
    st.subheader("2. OpenAlex institution", divider="violet")

    with st.form("institution_search_form", clear_on_submit=False):
        search_query = st.text_input(
            "Search by institution name first", placeholder="Europa-Universität Viadrina"
        )
        submitted = st.form_submit_button(
            "Search OpenAlex institutions",
            type="primary",
            disabled=not has_contact_user_agent(user_agent),
        )
    search_results: Optional[List[dict]] = st.session_state.get("institution_search_results")
    search_ran = st.session_state.get("institution_search_ran", False)
    if submitted:
        if not search_query.strip():
            st.warning("Please provide a search query.")
        else:
            with st.spinner("Searching institutions…"):
                try:
                    search_results = search_institutions_by_name(search_query.strip(), user_agent=user_agent)
                except requests.HTTPError as exc:
                    st.error(f"Institution search failed: {exc}")
                    search_results = []
                except requests.RequestException as exc:
                    st.error(f"Institution search error: {exc}")
                    search_results = []
            st.session_state["institution_search_results"] = search_results or []
            st.session_state["institution_search_ran"] = True
    search_results = st.session_state.get("institution_search_results")
    search_ran = st.session_state.get("institution_search_ran", False)
    if search_results:
        options: Dict[str, str] = {}
        for item in search_results:
            inst_id = item.get("id") or item.get("ror")
            if not inst_id:
                continue
            country = (item.get("country_code") or "").upper()
            ror_val = item.get("ror")
            tail = ror_val or inst_id
            if not ror_val:
                tail = f"{tail} [no ROR]"
            label = f"{item.get('display_name', '—')} ({country}) — {tail}"
            options[label] = inst_id
        if options:
            choice = st.radio(
                "Matches",
                options=list(options.keys()),
                key="institution_choice",
            )
            selected = options.get(choice)
            if selected:
                st.session_state["selected_institution_id"] = selected
                # store full metadata for lineage use
                st.session_state["selected_institution_meta"] = next(
                    (item for item in search_results if (item.get("id") or item.get("ror")) == selected),
                    {},
                )
    elif search_ran:
        st.info("No matches found.")

    ror_input = st.text_input(
        "…or enter an institution URL directly (OpenAlex institution or ROR)",
        placeholder="https://openalex.org/I123456789 | https://ror.org/02msan859",
        value=st.session_state.get("selected_institution_id", ""),
    )
    if ror_input and (is_ror_url(ror_input) or is_openalex_institution_id(ror_input)):
        st.session_state["selected_institution_id"] = ror_input.strip()
        st.session_state.pop("selected_institution_meta", None)
        # fall through to checkbox/return below

    include_lineage = st.checkbox(
        "Include works from parent/child institutions (OpenAlex lineage)",
        value=st.session_state.get("include_lineage", False),
        help="If enabled, works from related institutions in the OpenAlex lineage list are included (when available).",
        key="include_lineage_checkbox",
    )
    st.session_state["include_lineage"] = include_lineage
    return st.session_state.get("selected_institution_id"), include_lineage


def render_configured_institution_selector(
    repository_sources: Sequence[Union[DSpaceSource, OaiPmhSource]],
) -> Tuple[List[str], bool]:
    """Use institution identifiers attached to the selected repository sources."""
    st.subheader("2. OpenAlex institutions", divider="violet")
    institution_ids: List[str] = []
    missing_labels: List[str] = []
    for source in repository_sources:
        query_id = source.openalex_query_id
        if not query_id:
            missing_labels.append(source.label)
            continue
        if query_id not in institution_ids:
            institution_ids.append(query_id)
        identifiers = [
            value
            for value in (source.openalex_institution_id, source.ror_id)
            if value
        ]
        st.caption(f"{source.label}: {' · '.join(identifiers)}")

    if missing_labels:
        st.warning(
            "No OpenAlex/ROR institution is configured for: "
            + ", ".join(missing_labels)
            + ". OpenAlex will only query the linked institutions."
        )
    include_lineage = st.checkbox(
        "Include works from parent/child institutions (OpenAlex lineage)",
        value=st.session_state.get("include_lineage", False),
        help="If enabled, works from related institutions in each configured OpenAlex lineage are included.",
        key="include_lineage_checkbox",
    )
    st.session_state["include_lineage"] = include_lineage
    return institution_ids, include_lineage


def render_publication_type_selector(
    include_openalex: bool,
    dspace_sources: Sequence[DSpaceSource],
    oai_sources: Sequence[OaiPmhSource] = (),
) -> List[str]:
    """Display all publication types supported by the selected sources."""
    st.subheader("3. Publication types", divider="blue")
    openalex_types = list(OPENALEX_WORK_TYPES)
    entity_type_mappings = {
        "article": ["article"],
        "book": ["book", "book-chapter"],
        "artistic": ["artistic-work"],
    }
    dspace_types: List[str] = []
    for source in dspace_sources:
        for entity_type in source.entity_types:
            normalized = re.sub(r"[^a-z0-9]+", "-", entity_type.lower()).strip("-")
            for work_type in entity_type_mappings.get(normalized, [normalized]):
                if work_type and work_type not in dspace_types:
                    dspace_types.append(work_type)
    oai_types = list(
        dict.fromkeys(
            work_type
            for source in oai_sources
            for work_type in source.publication_types
            if work_type
        )
    )
    types = list(
        dict.fromkeys(
            [
                *(openalex_types if include_openalex else []),
                *dspace_types,
                *oai_types,
            ]
        )
    )
    labels = {
        "article": "Articles",
        "book": "Monographs / books",
        "book-chapter": "Book chapters",
        "artistic-work": "Artistic works",
        "software": "Software",
        "proceedings-article": "Proceedings articles",
        "reference-entry": "Reference entries",
        "preprint": "Preprints",
    }
    return st.multiselect(
        "Publication types to include",
        options=types,
        default=types,
        format_func=lambda value: labels.get(value, value.replace("-", " ").title()),
        help=(
            "Choose one or more types. DSpace and OAI-PMH records are normalized "
            "to the same publication types before filtering."
        ),
    )


def render_model_selector() -> str:
    """Let the user pick which SDG classifier to run."""
    st.subheader("4. SDG classifier", divider="green")
    default_index = next((i for i, (name, _) in enumerate(AURORA_MODELS) if name == "aurora-sdg-multi"), 0)
    selected_index = st.selectbox(
        "Choose a model",
        options=range(len(AURORA_MODELS)),
        index=default_index,
        format_func=lambda index: AURORA_MODELS[index][1],
    )
    return AURORA_MODELS[selected_index][0]


def render_advanced_options(
    semantic_key_from_secret: Optional[str],
    default_from_secret: Optional[str],
) -> Tuple[str, str, Optional[int]]:
    """Render additional filters (date range, record limit, info callouts)."""
    st.subheader("5. Advanced options", divider="yellow")
    today = datetime.today().date().replace(day=1)
    start_str = default_from_secret or "2023-01-01"
    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date().replace(day=1)
    except ValueError:
        start_date = date(2023, 1, 1)
    months: List[date] = []
    year = start_date.year
    month = start_date.month
    while year < today.year or (year == today.year and month <= today.month):
        months.append(date(year, month, 1))
        month += 1
        if month > 12:
            month = 1
            year += 1
    if not months:
        months = [today]
    labels = [dt.strftime("%B %Y") for dt in months]
    label_to_date = dict(zip(labels, months))
    desired_start = date(today.year - 2, 1, 1)
    start_default_date = next((m for m in months if m >= desired_start), months[-1])
    start_default_label = labels[months.index(start_default_date)]
    end_default_label = labels[-1]
    range_selection = st.select_slider(
        "Select publication period",
        options=labels,
        value=(
            (start_default_label, end_default_label)
            if len(labels) > 1
            else (labels[0], labels[0])
        ),
        format_func=lambda label: label,
    )
    start_label, end_label = range_selection
    start_index = labels.index(start_label)
    end_index = labels.index(end_label)
    if start_index > end_index:
        start_label, end_label = end_label, start_label
    from_date = label_to_date[start_label]
    selected_end_month = label_to_date[end_label]
    to_date = end_of_month(selected_end_month)
    st.caption(f"Including works published from {from_date:%B %Y} through {to_date:%B %Y}.")
    limit_value = st.number_input(
        "Limit to first N deduplicated publications (0 = no limit)",
        min_value=0,
        value=0,
        step=50,
        help="For multi-source tests, up to N records are fetched from each source and the final deduplicated result is capped at N.",
    )
    if not semantic_key_from_secret:
        st.info(
            "Add `semantic_scholar_api_key` to .streamlit/secrets.toml to fetch abstracts "
            "from Semantic Scholar when OpenAlex lacks them."
        )
    return from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d"), (limit_value or None)


def build_query_params(selection: QuerySelection) -> Dict[str, Any]:
    """Build the stable, non-secret identity of a query."""
    return {
        "sources": [
            *(["openalex"] if selection.include_openalex else []),
            *[source.id for source in selection.dspace_sources],
            *[source.id for source in selection.oai_sources],
        ],
        "institutions": list(selection.institution_ids),
        "types": list(selection.publication_types),
        "model": selection.model,
        "from": selection.from_date,
        "to": selection.to_date,
        "limit": selection.limit_rows,
    }


def query_configuration_errors(selection: QuerySelection) -> List[str]:
    """Return configuration errors that must block a new fetch."""
    errors = []
    if selection.include_openalex and not has_contact_user_agent(selection.user_agent):
        errors.append(
            "OpenAlex fetches require `http_user_agent` in `.streamlit/secrets.toml` "
            "with a real contact email in `mailto:` form."
        )
    if selection.model != "skip" and not selection.aurora_base_url:
        errors.append(
            "SDG classification requires `aurora_base_url` in "
            "`.streamlit/secrets.toml`."
        )
    return errors


def request_cancel_for_changed_params(
    state: MutableMapping[str, Any],
    current_params: Mapping[str, Any],
) -> bool:
    """Request cancellation when controls no longer match the active fetch."""
    if not state.get("fetch_in_progress"):
        return False
    ongoing_params = state.get("fetch_params")
    if not isinstance(ongoing_params, Mapping):
        return False
    if dict(ongoing_params) == dict(current_params):
        return False
    request_fetch_cancel(state)
    return True


def request_fetch_cancel(state: MutableMapping[str, Any]) -> None:
    """Set the cooperative cancellation flag for the active fetch."""
    state["fetch_cancel_requested"] = True


def invalidate_stale_result(
    state: MutableMapping[str, Any],
    current_params: Mapping[str, Any],
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Remove completed results that do not belong to the active controls."""
    state.setdefault("fetch_cancel_requested", False)
    state.setdefault("fetch_in_progress", False)
    result_payload = state.get(RESULT_SESSION_KEY)
    if (
        isinstance(result_payload, Mapping)
        and not state["fetch_in_progress"]
        and not _result_payload_matches_params(result_payload, current_params)
    ):
        state.pop(RESULT_SESSION_KEY, None)
        state.pop("preview_focus_index", None)
        state["preview_page"] = 1
        return None, True
    return (
        dict(result_payload) if isinstance(result_payload, Mapping) else None,
        False,
    )


def begin_fetch(
    state: MutableMapping[str, Any],
    current_params: Mapping[str, Any],
) -> None:
    """Initialize session state for a new fetch."""
    state["fetch_params"] = dict(current_params)
    state["fetch_cancel_requested"] = False
    state["fetch_in_progress"] = True


def execute_publication_fetch(
    selection: QuerySelection,
    *,
    progress_callback: Callable[[int, Optional[int], str], None],
    cancel_check: Callable[[], bool],
) -> Dict[str, Any]:
    """Resolve lineage, fetch publications, and build a completed result payload."""
    extra_institution_ids = list(selection.institution_ids[1:])
    if selection.include_openalex and selection.include_lineage:
        for index, configured_id in enumerate(selection.institution_ids):
            lineage_ids = (
                list(selection.cached_lineage)
                if index == 0 and selection.cached_lineage
                else fetch_institution_lineage(
                    configured_id,
                    user_agent=selection.user_agent,
                )
            )
            for lineage_id in lineage_ids:
                if (
                    lineage_id not in selection.institution_ids
                    and lineage_id not in extra_institution_ids
                ):
                    extra_institution_ids.append(lineage_id)

    rows, stats = fetch_publications_with_sdg(
        include_openalex=selection.include_openalex,
        dspace_sources=selection.dspace_sources,
        oai_sources=selection.oai_sources,
        institution_id=selection.institution_id,
        from_date=selection.from_date,
        work_type=selection.publication_types,
        model=selection.model,
        to_date=selection.to_date,
        limit_rows=selection.limit_rows,
        user_agent=selection.user_agent,
        semantic_scholar_api_key=selection.semantic_scholar_api_key,
        enable_google_scholar=selection.google_scholar_enabled,
        serpapi_api_key=selection.serpapi_api_key,
        aurora_base_url=selection.aurora_base_url,
        extra_institution_ids=extra_institution_ids or None,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    filename = build_output_filename(
        build_query_params(selection)["sources"],
        selection.institution_id,
        selection.publication_types,
        selection.model,
        selection.from_date,
        selection.to_date,
        selection.limit_rows,
    )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "csv_bytes": rows_to_csv_bytes(rows),
        "rows": rows,
        "stats": stats,
        "filename": filename,
        "params": build_query_params(selection),
    }


def _reset_fetch_state(
    progress_bar: Any,
    progress_text: Any,
    progress_detail: Any,
    cancel_container: Any,
) -> None:
    """Clear progress UI placeholders and reset fetch session flags."""
    progress_bar.empty()
    progress_text.empty()
    progress_detail.empty()
    cancel_container.empty()
    st.session_state["fetch_in_progress"] = False
    st.session_state["fetch_cancel_requested"] = False


def _result_payload_matches_params(
    result_payload: Mapping[str, Any],
    current_params: Mapping[str, Any],
) -> bool:
    """Return whether a completed result belongs to the active query controls."""
    stored_params = result_payload.get("params")
    return (
        result_payload.get("schema_version") == RESULT_SCHEMA_VERSION
        and isinstance(stored_params, Mapping)
        and dict(stored_params) == dict(current_params)
    )


def result_rows_from_payload(result_payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return structured rows, falling back to the stored CSV for old payloads."""
    rows = result_payload.get("rows")
    if isinstance(rows, list):
        return [dict(row) for row in rows if isinstance(row, Mapping)]
    csv_bytes = result_payload.get("csv_bytes") or b""
    try:
        csv_text = bytes(csv_bytes).decode("utf-8")
    except UnicodeDecodeError:
        csv_text = bytes(csv_bytes).decode("utf-8", errors="ignore")
    return list(csv.DictReader(io.StringIO(csv_text)))


def render_fetch_summary(
    stats: FetchStats,
    google_scholar_enabled: bool,
    *,
    semantic_scholar_api_key_configured: bool = False,
) -> None:
    """Render the completed source, deduplication, and enrichment counts."""
    semantic_scholar_auth_status = getattr(
        stats, "semantic_scholar_auth_error_status", None
    )
    gs_note = (
        f"; retrieved from Google Scholar: **{stats.gs_abstract_retrieved:,}**."
        if google_scholar_enabled
        else "."
    )
    st.success(
        f"Fetched **{stats.total_source_records:,}** source records from "
        f"**{', '.join(stats.sources_queried)}** and wrote "
        f"**{stats.total_processed:,}** deduplicated rows "
        f"(**{stats.duplicates_removed:,}** duplicates merged). "
        f"Abstracts available: **{stats.total_abstracts_available:,}**; "
        f"canonical publications missing abstracts before enrichment: "
        f"**{stats.source_abstract_missing:,}**; retrieved from Semantic Scholar: "
        f"**{stats.ss_abstract_retrieved:,}**{gs_note}"
    )
    if semantic_scholar_auth_status in {401, 403}:
        credential_detail = (
            "the configured API key"
            if semantic_scholar_api_key_configured
            else "the unauthenticated request"
        )
        st.warning(
            f"Semantic Scholar rejected {credential_detail} "
            f"(HTTP {semantic_scholar_auth_status}). "
            "Semantic Scholar enrichment was disabled for the remainder of this "
            "fetch; other configured abstract fallbacks continued. Check "
            "`semantic_scholar_api_key` in `.streamlit/secrets.toml`.",
            icon=":material/key_off:",
        )


def render_result_preview(
    all_rows: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Render the paginated preview and return the rows selected for focused charts."""
    total_rows = len(all_rows)
    selected_index = st.session_state.get("preview_focus_index")
    chart_rows = all_rows
    selected_title: Optional[str] = None
    if total_rows <= 0:
        st.session_state["preview_page"] = 1
        st.info("No preview rows available.")
        return chart_rows, selected_title

    st.write("")
    st.subheader("Preview", divider="orange")
    st.markdown(RADIO_CHECKBOX_CSS, unsafe_allow_html=True)
    total_pages = max(1, math.ceil(total_rows / PREVIEW_PAGE_SIZE))
    st.session_state.setdefault("preview_page", 1)
    current_page = min(max(1, st.session_state["preview_page"]), total_pages)
    if total_pages > 1:
        first_col, prev_col, info_col, next_col, last_col = st.columns(
            [1, 1, 2, 1, 1]
        )

        def set_page(target: int) -> None:
            st.session_state["preview_page"] = max(1, min(total_pages, target))
            st.rerun()

        if first_col.button("⏮ First", disabled=current_page == 1):
            set_page(1)
        if prev_col.button("◀ Previous", disabled=current_page == 1):
            set_page(current_page - 1)
        if next_col.button("Next ▶", disabled=current_page == total_pages):
            set_page(current_page + 1)
        if last_col.button("Last ⏭", disabled=current_page == total_pages):
            set_page(total_pages)
        current_page = st.session_state["preview_page"]
        info_col.markdown(f"Page **{current_page} / {total_pages}**")
    else:
        current_page = 1

    start_index = (current_page - 1) * PREVIEW_PAGE_SIZE
    preview_rows = build_preview_rows(
        all_rows,
        PREVIEW_COLUMNS,
        limit=PREVIEW_PAGE_SIZE,
        offset=start_index,
    )
    preview_df = pd.DataFrame(preview_rows)
    if "authors" in preview_df.columns:
        preview_df["authors"] = preview_df["authors"].apply(abbreviate_authors)
    preview_df.insert(
        0,
        "#",
        range(start_index + 1, start_index + 1 + len(preview_df)),
    )
    rows_in_page = len(preview_df)
    table_height = (
        980
        if rows_in_page >= PREVIEW_PAGE_SIZE
        else max(200, rows_in_page * 35 + 120)
    )
    column_configs = {}
    for column in preview_df.columns:
        if column == "#":
            column_configs[column] = st.column_config.NumberColumn(
                "#", help="Row number in this page", width="small"
            )
        elif column == "record_url":
            column_configs[column] = st.column_config.LinkColumn(
                "Source record",
                help="Open the publication in its source repository",
                display_text=r"https?://(?:www\.)?([^/]+)/.*",
            )
        elif column.lower() == "doi":
            column_configs[column] = st.column_config.LinkColumn(
                "DOI",
                help="Open this DOI in a new tab",
                display_text=r"(?:https?://(?:dx\.)?doi\.org/)?(.+)",
            )
        else:
            column_configs[column] = st.column_config.TextColumn(
                column.replace("_", " ").title()
            )
    st.data_editor(
        preview_df,
        hide_index=True,
        disabled=True,
        height=table_height,
        width="stretch",
        column_config=column_configs,
    )
    st.caption(f"Showing page {current_page} of {total_pages}.")

    dropdown_options = ["0 — All publications"]
    for index, row in enumerate(all_rows):
        title_preview = (
            row.get("title") or row.get("display_name") or "(no title)"
        )[:80]
        authors_preview = abbreviate_authors(row.get("authors") or "")
        label = f"{index + 1} — {title_preview}"
        if authors_preview:
            label = f"{index + 1} — {authors_preview}, {title_preview}"
        dropdown_options.append(label)

    dropdown_default = (
        0 if selected_index is None else min(max(0, selected_index + 1), total_rows)
    )
    previous_focus = st.session_state.get("preview_focus_index")
    selected_option = st.selectbox(
        "Focus publication",
        options=list(range(len(dropdown_options))),
        format_func=lambda index: dropdown_options[index],
        index=dropdown_default,
    )
    selected_index = selected_option - 1 if selected_option > 0 else None
    if selected_index != previous_focus:
        st.session_state["preview_focus_index"] = selected_index
        st.rerun()

    if selected_index is not None and 0 <= selected_index < total_rows:
        chart_rows = [all_rows[selected_index]]
        row_info = all_rows[selected_index]
        author_info = abbreviate_authors(row_info.get("authors") or "")
        title_info = (
            row_info.get("title") or row_info.get("display_name") or "(no title)"
        )
        selected_title = (
            f"{author_info}, {title_info}" if author_info else str(title_info)
        )
    st.caption("Select a publication above (0 = All).")
    return chart_rows, selected_title


def render_result_charts(
    all_rows: List[Dict[str, Any]],
    chart_rows: List[Dict[str, Any]],
    selected_title: Optional[str],
    *,
    from_date: str,
    to_date: str,
    institution_id: Optional[str],
    institution_ids: Sequence[str] = (),
) -> None:
    """Render every result visualization in its stable page order."""
    chart_data = aggregate_sdg_counts(chart_rows)
    st.write("")
    st.subheader("SDG distribution", divider="red")
    chart_title = "selected publication" if len(chart_rows) == 1 else "all publications"
    if chart_title == "selected publication" and selected_title:
        chart_title = f"selected publication ({selected_title})"
    render_sdg_pie_chart(chart_data, f"SDGs in {chart_title}")
    st.write("")
    st.subheader("Co-affiliation network", divider="violet")
    render_institution_network(
        all_rows,
        from_date,
        to_date,
        institution_id,
        selected_institution_ids=institution_ids,
    )
    st.write("")
    st.subheader("OA distribution by author", divider="blue")
    render_author_oa_chart(all_rows, from_date, to_date)
    st.write("")
    st.subheader("Open Access - closed access ratio", divider="green")
    render_oa_ring_chart(chart_rows)
    st.write("")
    st.subheader("Publication volume by OA status", divider="yellow")
    render_oa_status_chart(all_rows, from_date, to_date)
    st.write("")
    st.subheader("Publication types in selected period", divider="orange")
    render_publication_type_chart(all_rows, from_date, to_date)


def render_export_downloads(
    export_rows: List[Dict[str, Any]],
    csv_bytes: bytes,
    filename: str,
) -> None:
    """Render XLSX and CSV downloads for one completed result."""
    st.write("")
    st.divider()
    st.header("Download data set", divider="rainbow")
    st.info(
        "Download the full data set as Excel or CSV for further analysis using "
        "[OpenRefine](https://openrefine.org) or other tools.",
        icon=":material/file_download:",
    )
    if export_rows:
        st.download_button(
            "Download Excel",
            data=rows_to_excel_bytes(export_rows, CSV_FIELDNAMES),
            file_name=filename.replace(".csv", ".xlsx"),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    st.download_button(
        "Download CSV",
        data=csv_bytes,
        file_name=filename,
        mime="text/csv",
    )


def render_completed_result(
    result_payload: Mapping[str, Any],
    selection: QuerySelection,
) -> None:
    """Render summary, preview, charts, and downloads for a completed query."""
    stats = result_payload["stats"]
    if not isinstance(stats, FetchStats):
        raise TypeError("Completed result is missing FetchStats")
    csv_bytes = bytes(result_payload["csv_bytes"])
    filename = str(result_payload["filename"])
    all_rows = result_rows_from_payload(result_payload)
    render_fetch_summary(
        stats,
        selection.google_scholar_enabled,
        semantic_scholar_api_key_configured=bool(
            selection.semantic_scholar_api_key
        ),
    )
    chart_rows, selected_title = render_result_preview(all_rows)
    render_result_charts(
        all_rows,
        chart_rows,
        selected_title,
        from_date=selection.from_date,
        to_date=selection.to_date,
        institution_id=selection.institution_id,
        institution_ids=selection.institution_ids,
    )
    render_export_downloads(all_rows, csv_bytes, filename)


def main() -> None:
    """Streamlit entry point that wires all widgets, fetch flow, and previews."""
    st.set_page_config(page_title="Aurora SDG Publication Classifier", layout="wide")
    st.title("Aurora SDG Publication Classifier")
    st.caption(
        "Fetch and deduplicate publications from OpenAlex, DSpace, and OAI-PMH "
        "repositories, relate them to the 17 UN Sustainable Development Goals "
        "(SDGs) using the [Aurora SDG classifier]"
        "(https://aurora-universities.eu/sdg-research/classify/), and export the results."
    )

    user_agent, _ = resolve_user_agent()
    if not has_contact_user_agent(user_agent):
        st.text_input(
            "HTTP User-Agent (set via secrets.toml)",
            value=user_agent,
            disabled=True,
        )
        st.warning(
            "OpenAlex fetches stay disabled until `http_user_agent` in "
            "`.streamlit/secrets.toml` contains a real contact email."
        )

    configured_dspace_sources = resolve_dspace_sources()
    configured_oai_sources = resolve_oai_sources()
    if any(
        not hasattr(source, "openalex_query_id")
        for source in [*configured_dspace_sources, *configured_oai_sources]
    ):
        st.error(
            "The repository source model changed while Streamlit was running. "
            "Stop the current process and restart it so all application modules are reloaded."
        )
        st.code("source .venv/bin/activate\nstreamlit run app.py", language="bash")
        return
    include_openalex, selected_dspace_sources, selected_oai_sources = render_source_selector(
        configured_dspace_sources,
        configured_oai_sources,
    )
    selected_repository_sources: List[Union[DSpaceSource, OaiPmhSource]] = [
        *selected_dspace_sources,
        *selected_oai_sources,
    ]
    openalex_institution_ids: List[str] = []
    using_configured_institutions = False
    institution_id: Optional[str] = None
    include_lineage = False
    if include_openalex:
        configured_ids = [
            source.openalex_query_id
            for source in selected_repository_sources
            if source.openalex_query_id
        ]
        if configured_ids:
            using_configured_institutions = True
            openalex_institution_ids, include_lineage = render_configured_institution_selector(
                selected_repository_sources
            )
            institution_id = openalex_institution_ids[0]
        else:
            institution_id, include_lineage = render_institution_selector(user_agent)
            if institution_id:
                openalex_institution_ids = [institution_id]
    else:
        st.caption("OpenAlex is not selected, so no OpenAlex institution is required.")
    st.write("")
    publication_types = render_publication_type_selector(
        include_openalex,
        selected_dspace_sources,
        selected_oai_sources,
    )
    st.write("")
    model = render_model_selector()
    st.write("")
    semantic_scholar_key = resolve_semantic_scholar_key()
    google_scholar_enabled = resolve_google_scholar_enabled()
    serpapi_api_key = resolve_serpapi_key()
    default_from_date = get_secret_text(SECRET_DEFAULT_START)
    from_date_str, to_date_str, limit_rows = render_advanced_options(
        semantic_scholar_key,
        default_from_date,
    )

    if not include_openalex and not selected_repository_sources:
        st.info("Select at least one publication source to continue.")
        return

    if include_openalex and not openalex_institution_ids:
        st.info("Pick an institution or paste a ROR/OpenAlex institution ID/URL to continue.")
        return

    if include_openalex and any(
        not (is_ror_url(value) or is_openalex_institution_id(value))
        for value in openalex_institution_ids
    ):
        st.error("Institution must be a valid ROR URL or OpenAlex institution URL (e.g., https://openalex.org/I123456789).")
        return

    if not publication_types:
        st.info("Select at least one publication type to continue.")
        return
    
    st.write("")
    st.divider()
    st.header("Run query and preview results", divider="rainbow")
    render_google_scholar_status(google_scholar_enabled, serpapi_api_key)

    cached_meta = (
        {}
        if using_configured_institutions
        else st.session_state.get("selected_institution_meta") or {}
    )
    selection = QuerySelection(
        include_openalex=include_openalex,
        dspace_sources=tuple(selected_dspace_sources),
        oai_sources=tuple(selected_oai_sources),
        institution_id=institution_id,
        institution_ids=tuple(openalex_institution_ids),
        include_lineage=include_lineage,
        cached_lineage=tuple(
            str(value) for value in cached_meta.get("lineage") or [] if value
        ),
        publication_types=tuple(publication_types),
        model=model,
        from_date=from_date_str,
        to_date=to_date_str,
        limit_rows=limit_rows,
        user_agent=user_agent,
        semantic_scholar_api_key=semantic_scholar_key,
        google_scholar_enabled=google_scholar_enabled,
        serpapi_api_key=serpapi_api_key,
        aurora_base_url=resolve_aurora_base_url(),
    )
    current_params = build_query_params(selection)
    fetch_state = cast(MutableMapping[str, Any], st.session_state)
    if request_cancel_for_changed_params(fetch_state, current_params):
        st.toast(
            "Parameters changed, cancelling active fetch...",
            icon=":material/stop_circle:",
        )
    result_payload, result_invalidated = invalidate_stale_result(
        fetch_state,
        current_params,
    )

    configuration_errors = query_configuration_errors(selection)
    for message in configuration_errors:
        st.error(message)

    cancel_button_placeholder = st.empty()

    run_button_clicked = st.button(
        "Fetch works and build CSV",
        type="primary",
        key="main_fetch_button",
        disabled=bool(configuration_errors),
    )
    if run_button_clicked:
        begin_fetch(fetch_state, current_params)
        st.rerun()

    # This block handles rendering the cancel button and the actual fetch logic
    if st.session_state.get("fetch_in_progress"):
        # Render the cancel button inside its dedicated placeholder
        if cancel_button_placeholder.button("Cancel fetch", type="secondary", key="cancel_fetch_button"):
            request_fetch_cancel(fetch_state)
            st.toast("Cancelling fetch…", icon=":material/stop_circle:")

        progress_bar = st.progress(0)
        progress_text = st.empty()
        progress_detail = st.empty()
        current_detail: str = ""

        def progress_callback(done: int, expected: Optional[int], message: str) -> None:
            nonlocal current_detail
            target = limit_rows or expected
            fraction = min(done / target, 1.0) if target else 0.0
            progress_bar.progress(fraction)
            if expected:
                status = f"Processed {done:,} of {expected:,} works"
            elif limit_rows:
                status = f"Processed {done:,} of {limit_rows:,} requested works"
            else:
                status = f"Processed {done:,} works"
            if message:
                current_detail = message
            if current_detail:
                progress_detail.text(f"Currently processing: {current_detail}")
            else:
                progress_detail.empty()
            progress_text.text(status)

        def cancel_check() -> bool:
            return bool(st.session_state.get("fetch_cancel_requested"))

        with st.spinner("Fetching selected sources and contacting Aurora as needed…"):
            try:
                result_payload = execute_publication_fetch(
                    selection,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                )
            except FetchCancelled:
                _reset_fetch_state(
                    progress_bar,
                    progress_text,
                    progress_detail,
                    cancel_button_placeholder,
                )
                st.info("Fetch cancelled.", icon=":material/stop_circle:")
                return
            except requests.HTTPError as exc:
                _reset_fetch_state(
                    progress_bar,
                    progress_text,
                    progress_detail,
                    cancel_button_placeholder,
                )
                st.error(f"Request failed: {exc}")
                return
            except requests.RequestException as exc:
                _reset_fetch_state(
                    progress_bar,
                    progress_text,
                    progress_detail,
                    cancel_button_placeholder,
                )
                st.error(f"Network error: {exc}")
                return
            except ValueError as exc:
                _reset_fetch_state(
                    progress_bar,
                    progress_text,
                    progress_detail,
                    cancel_button_placeholder,
                )
                st.error(f"Source configuration or response error: {exc}")
                return

        _reset_fetch_state(
            progress_bar,
            progress_text,
            progress_detail,
            cancel_button_placeholder,
        )
        st.session_state[RESULT_SESSION_KEY] = result_payload
        st.session_state.pop("preview_focus_index", None)
        st.session_state["preview_page"] = 1
        st.rerun()

    elif not result_payload:
        if result_invalidated:
            st.info("Query settings changed. Fetch again to build results for the current settings.")
        else:
            st.info("Click the button above to fetch publications.")
        return

    render_completed_result(result_payload, selection)


if __name__ == "__main__":
    main()
