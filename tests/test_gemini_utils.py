import unittest
from unittest.mock import Mock, patch

from google.genai import types

from gemini_utils import (
    DEFAULT_HTTP_TIMEOUT_MS,
    PERMANENT_ERROR,
    RATE_LIMIT_ERROR,
    TRANSIENT_ERROR,
    classify_gemini_error,
    generate_content_with_retry,
    is_transient_error,
)


class GeminiUtilsTest(unittest.TestCase):
    def test_default_http_timeout_is_positive(self):
        self.assertGreater(DEFAULT_HTTP_TIMEOUT_MS, 0)

    def test_disconnect_and_timeout_are_retryable(self):
        self.assertTrue(is_transient_error("Server disconnected without sending a response."))
        self.assertTrue(is_transient_error("Read timeout while waiting for response"))
        self.assertTrue(is_transient_error("502 Bad Gateway"))
        self.assertTrue(is_transient_error("ConnectError: DNS name resolution failed"))

    def test_error_classification_distinguishes_lifecycle_quota_and_outage(self):
        self.assertEqual(classify_gemini_error("404 NOT_FOUND model"), PERMANENT_ERROR)
        self.assertEqual(classify_gemini_error("429 RESOURCE_EXHAUSTED"), RATE_LIMIT_ERROR)
        self.assertEqual(classify_gemini_error("503 UNAVAILABLE"), TRANSIENT_ERROR)

    def test_permanent_model_error_is_not_retried(self):
        client = Mock()
        client.models.generate_content.side_effect = RuntimeError("404 NOT_FOUND model")

        with patch("gemini_utils.wait_for_model_slot"):
            with self.assertRaisesRegex(RuntimeError, PERMANENT_ERROR):
                generate_content_with_retry(client, "missing-model", "x", Mock(), 3)

        client.models.generate_content.assert_called_once()

    def test_transient_error_uses_bounded_application_retry(self):
        client = Mock()
        response = Mock(model_version="gemini-3.5-flash", response_id="response-1")
        response.usage_metadata = None
        client.models.generate_content.side_effect = [
            RuntimeError("503 UNAVAILABLE"),
            response,
        ]

        with patch("gemini_utils.wait_for_model_slot"), \
             patch("gemini_utils.time.sleep"), \
             patch("gemini_utils.random.uniform", return_value=0):
            result = generate_content_with_retry(
                client,
                "gemini-3.5-flash",
                "x",
                Mock(),
                3,
            )

        self.assertIs(result, response)
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_request_timeout_is_capped_by_remaining_wall_clock_budget(self):
        client = Mock()
        response = Mock(model_version="gemini-3.5-flash", response_id="response-1")
        response.usage_metadata = None
        client.models.generate_content.return_value = response
        config = types.GenerateContentConfig(max_output_tokens=10)

        with patch("gemini_utils._model_slot_wait_seconds", return_value=0), \
             patch("gemini_utils.wait_for_model_slot"):
            generate_content_with_retry(
                client,
                "gemini-3.5-flash",
                "x",
                config,
                max_retries=1,
                http_timeout_ms=120_000,
                retry_budget_seconds=10,
            )

        sent_config = client.models.generate_content.call_args.kwargs["config"]
        self.assertIsNone(config.http_options)
        self.assertGreater(sent_config.http_options.timeout, 0)
        self.assertLessEqual(sent_config.http_options.timeout, 10_000)
        self.assertEqual(sent_config.http_options.retry_options.attempts, 1)

    def test_model_slot_wait_cannot_overrun_wall_clock_budget(self):
        client = Mock()

        with patch("gemini_utils._model_slot_wait_seconds", return_value=8):
            with self.assertRaisesRegex(RuntimeError, "wall-clock budget exhausted"):
                generate_content_with_retry(
                    client,
                    "gemini-3.5-flash",
                    "x",
                    types.GenerateContentConfig(),
                    max_retries=2,
                    http_timeout_ms=120_000,
                    retry_budget_seconds=5,
                )

        client.models.generate_content.assert_not_called()


if __name__ == "__main__":
    unittest.main()
