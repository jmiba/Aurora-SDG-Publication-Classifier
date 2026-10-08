"""Optional, pinned OpenAlex embedding/head comparison without hosted inference.

Head arithmetic adapted from ourresearch/openalex-sdgs classifier/head.py.
Copyright (c) 2026 Impactstory, Inc. (OpenAlex), MIT; see models/openalex-sdgs/LICENSE.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import threading
from pathlib import Path
from typing import Any, Callable

import streamlit as st

from llm_sdg import SDG_NAMES

MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
MODEL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
HEAD_SHA256 = "0b1357dcb19215205faf7381c224159cdac6a733fd1d08725f2935d6b938f52a"
HEAD_PATH = Path(__file__).parent / "models/openalex-sdgs/sdg_jev_head_v2.json"
INPUT_POLICY = "title-abstract:first-2000-chars:venue-omitted:v1"
IDENTITY = {
    "embedding_model": MODEL_ID, "embedding_revision": MODEL_REVISION,
    "head_sha256": HEAD_SHA256, "input_policy": INPUT_POLICY,
    "precision": "float32", "device": "cpu", "normalization": "unit-l2",
    "scoring": "released-local:float64-isotonic:threshold-before-rounding",
}
CACHE_MODEL = "openalex-local:" + hashlib.sha256(
    json.dumps(IDENTITY, sort_keys=True).encode()
).hexdigest()
_INFERENCE_LOCK = threading.Lock()


def dependencies_available() -> bool:
    """Check availability without importing the ML stack or loading weights."""
    return all(importlib.util.find_spec(name) is not None
               for name in ("torch", "sentence_transformers", "numpy"))


def work_text(title: str, abstract: str) -> str:
    """Match the released input formatter, with venue deliberately omitted."""
    return ("Title: " + title + ("\n\nAbstract: " + abstract if abstract else ""))[:2000]


def input_hash(title: str, abstract: str) -> str:
    """Include full source text and the pinned inference contract in cache reuse."""
    return hashlib.sha256(json.dumps(
        [title, abstract, CACHE_MODEL], ensure_ascii=False
    ).encode()).hexdigest()


def validate_result(value: Any) -> dict[str, Any]:
    """Validate all scores and recompute membership rather than trusting cached tags."""
    if not isinstance(value, dict) or value.get("identity") != IDENTITY:
        raise ValueError("invalid classifier identity")
    scores = value.get("scores")
    if not isinstance(scores, list) or len(scores) != 17:
        raise ValueError("expected 17 scores")
    if any(type(score) not in (int, float) or not math.isfinite(score)
           or not 0 <= score <= 1 for score in scores):
        raise ValueError("invalid score")
    # Match the released local head's float32 representation of its 0.4 cutoff.
    keep = [i for i, score in enumerate(scores) if score >= 0.4000000059604645]
    keep.sort(key=lambda i: (-scores[i], i))
    return {"identity": dict(IDENTITY), "scores": scores,
            "sdgs": [{"code": i + 1, "score": scores[i]} for i in keep],
            "status": "classified" if keep else "below_threshold"}


def format_result(result: dict[str, Any] | None) -> str:
    if result is None:
        return ""
    return "; ".join(f"SDG {goal['code']} ({goal['score']:.4f})" for goal in result["sdgs"])


def format_primary_result(result: dict[str, Any] | None) -> str:
    """Present thresholded local scores in the shared chart/preview percentage contract."""
    if result is None:
        return ""
    return "\n".join(
        f"{goal['score'] * 100:.4f}% SDG {goal['code']} ({SDG_NAMES[goal['code'] - 1]})"
        for goal in result["sdgs"]
    )


class Head:
    """Released float32 logistic head with float64 isotonic interpolation."""

    def __init__(self) -> None:
        import numpy as np

        raw = HEAD_PATH.read_bytes()
        if hashlib.sha256(raw).hexdigest() != HEAD_SHA256:
            raise ValueError("head checksum mismatch")
        artifact = json.loads(raw)
        self.weights = np.asarray(artifact["W"], np.float32)
        self.bias = np.asarray(artifact["b"], np.float32)
        self.iso = [(np.asarray(goal["iso_x"], np.float64),
                     np.asarray(goal["iso_y"], np.float64)) for goal in artifact["goals"]]

    def score(self, vectors: Any) -> Any:
        import numpy as np

        vectors = np.asarray(vectors, np.float32)
        if vectors.ndim != 2 or vectors.shape[1] != 1024 or not np.isfinite(vectors).all():
            raise ValueError("invalid embedding")
        if not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4, rtol=0):
            raise ValueError("embedding is not normalized")
        raw = 1 / (1 + np.exp(-(vectors @ self.weights.T + self.bias)))
        return np.stack([np.clip(np.interp(raw[:, i], *self.iso[i]), 0, 1)
                         for i in range(17)], axis=1)


@st.cache_resource(max_entries=1, show_spinner=False)
def load_resources() -> tuple[Any, Head]:
    """Load once per process; the caller serializes use of the shared CPU model."""
    import torch
    from sentence_transformers import SentenceTransformer

    encoder = SentenceTransformer(
        MODEL_ID, revision=MODEL_REVISION, device="cpu",
        model_kwargs={"dtype": torch.float32},
    )
    return encoder, Head()


class LocalClassifier:
    """One fetch's adapter; a load failure is retried on the next fetch, not every row."""

    def __init__(self) -> None:
        self.load_failure = ""

    # @lat: [[openalex-local#Local OpenAlex comparison#Inference and isolation]]
    def classify(self, title: str, abstract: str,
                 ensure_active: Callable[[], None]) -> tuple[dict[str, Any] | None, str]:
        if not title.strip():
            return None, "local_no_title"
        while not _INFERENCE_LOCK.acquire(timeout=0.1):
            ensure_active()
        try:
            ensure_active()
            if self.load_failure:
                return None, self.load_failure
            try:
                encoder, head = load_resources()
            except ImportError:
                self.load_failure = "local_dependencies_missing"
                return None, self.load_failure
            except Exception:
                self.load_failure = "local_model_unavailable"
                return None, self.load_failure
            ensure_active()
            try:
                vectors = encoder.encode([work_text(title, abstract)], batch_size=1,
                                         normalize_embeddings=True, show_progress_bar=False)
                scores = head.score(vectors)[0].tolist()
                result = validate_result({"identity": IDENTITY, "scores": scores})
            except Exception:
                return None, "local_inference_error"
            ensure_active()
            note = "venue_omitted"
            if not abstract:
                note += "; title_only_no_abstract"
            if len("Title: " + title + ("\n\nAbstract: " + abstract if abstract else "")) > 2000:
                note += "; input_truncated"
            return result, note
        finally:
            _INFERENCE_LOCK.release()
