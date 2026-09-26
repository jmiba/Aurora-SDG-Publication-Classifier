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
    def test_rate_limiter_sleeps_after_releasing_reservation_lock(self) -> None:
        limiter = openalex_sdg._RateLimiter(0.5)
        limiter._next_start = 10.25

        def assert_lock_is_free(delay: float) -> None:
            self.assertEqual(delay, 0.25)
            self.assertTrue(limiter._lock.acquire(blocking=False))
            limiter._lock.release()

        with (
            patch.object(openalex_sdg.time, "monotonic", return_value=10.0),
            patch.object(openalex_sdg.time, "sleep", side_effect=assert_lock_is_free),
        ):
            limiter.wait()

        self.assertEqual(limiter._next_start, 10.75)

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

    def test_rate_limit_reset_header_is_used_for_429(self) -> None:
        session = Mock()
        limited = response(429)
        limited.headers["ratelimit-reset"] = "54"
        session.post.side_effect = [limited, response(200)]
        observe = Mock()

        with patch("request_utils.time.sleep") as sleep:
            result = request_with_backoff(
                session, "post", "https://example.test/resource",
                retries=2, cap=90, _after_response=observe,
            )

        self.assertEqual(result.status_code, 200)
        sleep.assert_called_once_with(54.0)
        self.assertEqual(observe.call_count, 2)

    def test_advertised_minute_limit_paces_later_llm_requests(self) -> None:
        limiter = openalex_sdg._RateLimiter(0.5)
        limited = response(200)
        limited.headers["x-ratelimit-limit-minute"] = "10"
        with patch.object(openalex_sdg.time, "monotonic", return_value=10.0):
            limiter.observe_minute_limit(limited)
        self.assertEqual(limiter._min_interval, 6.0)
        self.assertEqual(limiter._next_start, 16.0)

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

    def test_aurora_non_json_success_reports_invalid_json(self) -> None:
        session = Mock()
        invalid_response = response(200)
        invalid_response._content = b"not json"
        invalid_response.encoding = "utf-8"
        session.post.return_value = invalid_response

        prediction, note = openalex_sdg.classify_text_aurora(
            "aurora-sdg-multi",
            "Classification input",
            session=session,
            aurora_base_url="https://aurora.example/classify",
            retries=1,
        )

        self.assertIsNone(prediction)
        self.assertEqual(note, "invalid json")

    def test_semantic_scholar_quotes_doi_as_one_url_segment(self) -> None:
        session = Mock()
        success = response(200)
        success._content = b'{"abstract": "Found"}'
        success.encoding = "utf-8"
        session.get.return_value = success

        abstract = openalex_sdg.get_abstract_from_semantic_scholar(
            "10.1234/example(2024)/part?query",
            session=session,
            retries=1,
        )

        self.assertEqual(abstract, "Found")
        requested_url = session.get.call_args.args[0]
        self.assertIn("10.1234%2Fexample%282024%29%2Fpart%3Fquery", requested_url)

    def test_semantic_scholar_reports_rejected_credentials(self) -> None:
        session = Mock()
        session.get.return_value = response(401)
        on_auth_error = Mock()

        abstract = openalex_sdg.get_abstract_from_semantic_scholar(
            "10.1234/rejected-key",
            session=session,
            api_key="stale-key",
            on_auth_error=on_auth_error,
        )

        self.assertIsNone(abstract)
        on_auth_error.assert_called_once_with(401)
        self.assertEqual(session.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
