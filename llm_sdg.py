"""Independent, evidence-based SDG decisions from a hosted open-weights LLM."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import requests

from request_utils import request_with_backoff

MODEL_ID = "Qwen/Qwen3.8-27B"
PROMPT_VERSION = "independent-sdg-v3"
SDG_NAMES = (
    "No Poverty", "Zero Hunger", "Good Health and Well-being", "Quality Education",
    "Gender Equality", "Clean Water and Sanitation", "Affordable and Clean Energy",
    "Decent Work and Economic Growth", "Industry, Innovation and Infrastructure",
    "Reduced Inequalities", "Sustainable Cities and Communities",
    "Responsible Consumption and Production", "Climate Action", "Life Below Water",
    "Life on Land", "Peace, Justice and Strong Institutions",
    "Partnerships for the Goals",
)
SYSTEM_PROMPT = (
    "Classify the publication against all 17 UN Sustainable Development Goals using its title and abstract. "
    "Focus on the main research question and measured outcomes, even when SDG terms are absent. "
    "Assign every goal that is a substantive topic, analytical dimension, or policy outcome. "
    "For example, research on refugee integration and unequal opportunity can support SDG 10, "
    "and measured employment outcomes can support SDG 8. Research on democratic participation "
    "can support SDG 16. Renewable energy adoption can support SDG 7. These are examples, not "
    "automatic keyword rules. A generic software release, incidental location, or passing "
    "mention has no SDG. Return no_sdg only if no substantive goal is supported. For every "
    "assigned goal, give a short exact contiguous quote from the supplied title or abstract "
    "as evidence. Never invent text. Return manual_review if the evidence is genuinely "
    "ambiguous. Goals:\n"
) + "\n".join(f"{n}. {name}" for n, name in enumerate(SDG_NAMES, 1))

RESPONSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "sdgs"],
    "properties": {
        "status": {"type": "string", "enum": ["classified", "no_sdg", "manual_review"]},
        "sdgs": {"type": "array", "maxItems": 17, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["code", "evidence"],
            "properties": {"code": {"type": "integer", "minimum": 1, "maximum": 17},
                           "evidence": {"type": "string"}},
        }},
    },
}


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str = MODEL_ID
    enable_thinking: Optional[bool] = None

    @property
    def thinking_mode(self) -> Optional[bool]:
        """Use direct answers for Qwen3.8 unless the user overrides the mode."""
        if self.enable_thinking is not None:
            return self.enable_thinking
        return False if "qwen3.8" in self.model.casefold() else None

    @property
    def identity(self) -> str:
        # The key is deliberately excluded; it can rotate without changing decisions.
        value = (
            f"{self.base_url.rstrip('/')}|{self.model}|{PROMPT_VERSION}|"
            f"{self.thinking_mode}|{SYSTEM_PROMPT}"
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @property
    def cache_model(self) -> str:
        return f"llm:{self.identity}"


def input_hash(title: str, abstract: str, config: LlmConfig) -> str:
    payload = json.dumps([title, abstract, config.identity], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_decision(value: Any, title: str = "", abstract: str = "") -> dict[str, Any]:
    """Reject malformed or contradictory decisions; an empty no_sdg is valid."""
    if not isinstance(value, dict) or set(value) != {"status", "sdgs"}:
        raise ValueError("invalid decision fields")
    status, sdgs = value["status"], value["sdgs"]
    if status not in {"classified", "no_sdg", "manual_review"} or not isinstance(sdgs, list):
        raise ValueError("invalid decision status")
    if (status == "classified") != bool(sdgs) or (status != "classified" and sdgs):
        raise ValueError("contradictory decision")
    seen = set()
    for item in sdgs:
        if not isinstance(item, dict) or set(item) != {"code", "evidence"}:
            raise ValueError("invalid SDG fields")
        code, evidence = item["code"], item["evidence"]
        if type(code) is not int or not 1 <= code <= 17 or code in seen:
            raise ValueError("invalid or duplicate SDG code")
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500:
            raise ValueError("invalid SDG evidence")
        if title or abstract:
            source = re.sub(r"\s+", " ", f"{title} {abstract}").casefold()
            quote = re.sub(r"\s+", " ", evidence).casefold().strip().strip('"“”‘’')
            if quote not in source:
                raise ValueError("evidence not found in source")
        seen.add(code)
    return {"status": status, "sdgs": sorted(sdgs, key=lambda item: item["code"])}


def classify_publication(
    title: str, abstract: str, session: requests.Session, config: LlmConfig,
    *, request_limiter: Optional[Any] = None,
) -> tuple[Optional[dict[str, Any]], str]:
    """Call an OpenAI-compatible chat endpoint; return fixed failure categories."""
    if not title.strip() and not abstract.strip():
        return None, "llm_no_text"
    parsed = urlparse(config.base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None, "llm_configuration_error"
    url = f"{config.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"title": title, "abstract": abstract}, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "sdg_decision", "strict": True, "schema": RESPONSE_SCHEMA,
        }},
    }
    if config.thinking_mode is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": config.thinking_mode}
    try:
        response = request_with_backoff(
            session, "post", url,
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
            json=payload, timeout=120, retries=3, base=1.0, cap=90.0,
            _before_request=request_limiter.wait if request_limiter else None,
            _after_response=request_limiter.observe_minute_limit if request_limiter else None,
        )
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return None, f"llm_http_{status}" if isinstance(status, int) else "llm_transport_error"
    try:
        data = response.json()
        message = data["choices"][0]["message"]
        content = message["content"]
        if isinstance(content, str):
            decision = json.loads(content)
        else:
            raise ValueError("missing content")
        return validate_decision(decision, title, abstract), ""
    except (ValueError, KeyError, IndexError, TypeError):
        return None, "llm_invalid_response"


def format_decision(value: Mapping[str, Any]) -> str:
    return "\n".join(
        f"SDG {item['code']} ({SDG_NAMES[item['code'] - 1]})"
        for item in value["sdgs"]
    )


def evidence_text(value: Mapping[str, Any]) -> str:
    return "\n".join(f"SDG {item['code']}: {item['evidence']}" for item in value["sdgs"])
