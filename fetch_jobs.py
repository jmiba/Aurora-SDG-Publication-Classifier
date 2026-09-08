"""Process-local registry for background publication-fetch jobs.

Streamlit re-executes the app script inside a fresh module namespace on every
rerun, so module-level state in ``app.py`` (including any job registry defined
there) is reset to its initial value after each rerun. The registry must
therefore live in a normally imported module, which Python caches in
``sys.modules`` and only executes once per server process.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class FetchJobSnapshot:
    """Immutable view of background fetch state for one UI poll."""

    done: bool
    progress_done: int
    progress_expected: Optional[int]
    progress_message: str
    result_payload: Optional[Dict[str, Any]]
    error: Optional[Exception]


@dataclass
class FetchJob:
    """Thread-safe state shared by a fetch worker and its Streamlit session."""

    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: Any = field(default_factory=threading.Lock)
    done: bool = False
    progress_done: int = 0
    progress_expected: Optional[int] = None
    progress_message: str = "Starting fetch"
    result_payload: Optional[Dict[str, Any]] = None
    error: Optional[Exception] = None
    thread: Optional[threading.Thread] = None

    def publish_progress(
        self, done: int, expected: Optional[int], message: str
    ) -> None:
        with self.lock:
            self.progress_done = done
            self.progress_expected = expected
            if message:
                self.progress_message = message

    def complete(
        self,
        *,
        result_payload: Optional[Dict[str, Any]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        with self.lock:
            self.result_payload = result_payload
            self.error = error
            self.done = True

    def snapshot(self) -> FetchJobSnapshot:
        with self.lock:
            return FetchJobSnapshot(
                done=self.done,
                progress_done=self.progress_done,
                progress_expected=self.progress_expected,
                progress_message=self.progress_message,
                result_payload=self.result_payload,
                error=self.error,
            )


_FETCH_JOBS: Dict[str, FetchJob] = {}
_FETCH_JOBS_LOCK = threading.Lock()


def get_fetch_job(job_id: str) -> Optional[FetchJob]:
    """Return one process-local background job without exposing the registry."""
    with _FETCH_JOBS_LOCK:
        return _FETCH_JOBS.get(job_id)


def discard_fetch_job(job_id: str) -> None:
    """Remove a completed job from the process-local registry."""
    with _FETCH_JOBS_LOCK:
        _FETCH_JOBS.pop(job_id, None)


def start_fetch_job(
    runner: Callable[[FetchJob], Optional[Dict[str, Any]]]
) -> str:
    """Start a background fetch and return its process-local job identifier.

    The runner receives the live job so it can wire progress publication and
    cancellation checks directly onto the job's thread-safe state.
    """
    job_id = uuid.uuid4().hex
    job = FetchJob()

    def run() -> None:
        try:
            payload = runner(job)
        except Exception as exc:
            job.complete(error=exc)
        else:
            job.complete(result_payload=payload)

    thread = threading.Thread(
        target=run,
        name=f"publication-fetch-{job_id[:8]}",
        daemon=True,
    )
    job.thread = thread
    with _FETCH_JOBS_LOCK:
        _FETCH_JOBS[job_id] = job
    thread.start()
    return job_id
