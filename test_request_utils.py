"""Tests for the shared HTTP retry policy."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch

import requests

import openalex_sdg
from request_utils import _backoff, request_with_backoff


def response(status_code: int, *, retry_after: str | None = None) -> requests.Response:
    result = requests.Response()
    result.status_code = status_code
    result.url = "https://example.test/resource"
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


class RequestWithBackoffTests(unittest.TestCase):
    def test_backoff_is_exponential_with_full_jitter_and_cap(self) -> None:
        with patch("request_utils.random.uniform", side_effect=lambda low, high: high):
            self.assertEqual(_backoff(1, 0.5, 15.0, None), 0.5)
            self.assertEqual(_backoff(2, 0.5, 15.0, None), 1.0)
            self.assertEqual(_backoff(7, 0.5, 15.0, None), 15.0)

    def test_retry_after_takes_precedence_and_is_capped(self) -> None:
        with patch("request_utils.random.uniform") as jitter:
            self.assertEqual(_backoff(1, 0.5, 15.0, "7"), 7.0)
            self.assertEqual(_backoff(1, 0.5, 15.0, "30"), 15.0)
        jitter.assert_not_called()

    def test_retryable_status_uses_retry_after_then_returns_success(self) -> None:
        session = Mock()
        session.get.side_effect = [
            response(503, retry_after="4"),
            response(200),
        ]

        with patch("request_utils.time.sleep") as sleep:
            result = request_with_backoff(
                session,
                "get",
                "https://example.test/resource",
                retries=2,
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(4.0)

    def test_transport_errors_are_retried_with_shared_backoff(self) -> None:
        session = Mock()
        session.get.side_effect = [requests.ConnectionError("offline"), response(200)]

        with (
            patch("request_utils._backoff", return_value=0.75) as backoff,
            patch("request_utils.time.sleep") as sleep,
        ):
            result = request_with_backoff(
                session,
                "get",
                "https://example.test/resource",
                retries=2,
                base=0.25,
                cap=3.0,
            )

        self.assertEqual(result.status_code, 200)
        backoff.assert_called_once_with(1, 0.25, 3.0, None)
        sleep.assert_called_once_with(0.75)

    def test_per_attempt_hook_runs_for_every_retry(self) -> None:
        session = Mock()
        session.post.side_effect = [response(500), response(200)]
        before_request = Mock()

        with patch("request_utils.time.sleep"):
            request_with_backoff(
                session,
                "post",
                "https://example.test/resource",
                retries=2,
                base=0,
                _before_request=before_request,
            )

        self.assertEqual(before_request.call_args_list, [call(), call()])

    def test_aurora_final_429_reports_status(self) -> None:
        session = Mock()
        session.post.return_value = response(429)

        with patch("request_utils.time.sleep"):
            prediction, note = openalex_sdg.classify_text_aurora(
                "aurora-sdg-multi",
                "Classification input",
                session=session,
                aurora_base_url="https://aurora.example/classify",
                retries=2,
                pause=0,
            )

        self.assertIsNone(prediction)
        self.assertEqual(note, "http_error:429")
        self.assertEqual(session.post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
