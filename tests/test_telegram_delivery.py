import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests

from telegram_notify import (
    _send_message,
    _target_fingerprint,
    build_structured_summary,
    send_photo,
)
from test_portfolio_schema import state_payload


class FakeResponse:
    def __init__(self, status_code, response_json=None, json_error=None):
        self.status_code = status_code
        self._response_json = response_json
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._response_json


class TelegramDeliveryTest(unittest.TestCase):
    token = "123456:token-that-must-not-appear-in-logs"
    chat_id = "-1001234567890"

    def test_message_success_requires_json_ok_result_and_message_id(self):
        output = io.StringIO()
        with patch(
            "telegram_notify.requests.post",
            return_value=FakeResponse(200, {"ok": True, "result": {}}),
        ) as post, redirect_stdout(output):
            delivered = _send_message(self.token, self.chat_id, "hello")

        self.assertFalse(delivered)
        self.assertEqual(post.call_count, 1)
        self.assertIn("delivery rejected", output.getvalue())
        self.assertIn(f"target={_target_fingerprint(self.chat_id)}", output.getvalue())
        self.assertNotIn(self.chat_id, output.getvalue())
        self.assertNotIn(self.token, output.getvalue())

    def test_message_success_logs_safe_target_and_message_receipt(self):
        output = io.StringIO()
        with patch(
            "telegram_notify.requests.post",
            return_value=FakeResponse(200, {"ok": True, "result": {"message_id": 777}}),
        ), redirect_stdout(output):
            delivered = _send_message(self.token, self.chat_id, "hello")

        self.assertTrue(delivered)
        self.assertIn(f"target={_target_fingerprint(self.chat_id)}", output.getvalue())
        self.assertIn("message_id=777", output.getvalue())
        self.assertNotIn(self.chat_id, output.getvalue())
        self.assertNotIn(self.token, output.getvalue())

    def test_markdown_parse_error_retries_once_without_parse_mode(self):
        responses = [
            FakeResponse(
                400,
                {
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: can't parse entities",
                },
            ),
            FakeResponse(200, {"ok": True, "result": {"message_id": 778}}),
        ]
        output = io.StringIO()
        with patch("telegram_notify.requests.post", side_effect=responses) as post, redirect_stdout(output):
            delivered = _send_message(self.token, self.chat_id, "*broken markdown")

        self.assertTrue(delivered)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["parse_mode"], "Markdown")
        self.assertNotIn("parse_mode", post.call_args_list[1].kwargs["json"])
        self.assertIn("Markdown fallback", output.getvalue())
        self.assertIn("message_id=778", output.getvalue())

    def test_non_markup_400_is_not_retried(self):
        output = io.StringIO()
        response = FakeResponse(
            400,
            {
                "ok": False,
                "error_code": 400,
                "description": f"Bad Request: chat {self.chat_id} not found",
            },
        )
        with patch("telegram_notify.requests.post", return_value=response) as post, redirect_stdout(output):
            delivered = _send_message(self.token, self.chat_id, "hello")

        self.assertFalse(delivered)
        self.assertEqual(post.call_count, 1)
        self.assertIn("chat<redacted>", output.getvalue())
        self.assertNotIn(self.chat_id, output.getvalue())

    def test_request_exception_does_not_echo_token_or_request_url(self):
        output = io.StringIO()
        error = requests.exceptions.ReadTimeout(
            "https://api.telegram.org/bot123456:token-that-must-not-appear-in-logs/sendMessage"
        )
        with patch("telegram_notify.requests.post", side_effect=error), redirect_stdout(output):
            delivered = _send_message(self.token, self.chat_id, "hello")

        self.assertFalse(delivered)
        self.assertIn("exception=ReadTimeout", output.getvalue())
        self.assertNotIn("token-that-must-not-appear-in-logs", output.getvalue())
        self.assertNotIn("https://api.telegram.org", output.getvalue())

    def test_photo_rate_limit_retry_rewinds_stream_before_second_upload(self):
        responses = iter([
            FakeResponse(
                429,
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 0},
                },
            ),
            FakeResponse(200, {"ok": True, "result": {"message_id": 779}}),
        ])
        uploaded_payloads = []

        def post(*_args, **kwargs):
            uploaded_payloads.append(kwargs["files"]["photo"].read())
            return next(responses)

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "chart.png"
            image_path.write_bytes(b"chart-bytes")
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": self.token,
                    "TELEGRAM_CHAT_ID": self.chat_id,
                },
                clear=False,
            ), patch("telegram_notify.requests.post", side_effect=post) as mocked_post, patch(
                "telegram_notify.time.sleep"
            ) as sleep:
                delivered = send_photo(str(image_path), "chart")

        self.assertTrue(delivered)
        self.assertEqual(mocked_post.call_count, 2)
        self.assertEqual(uploaded_payloads, [b"chart-bytes", b"chart-bytes"])
        sleep.assert_called_once_with(1)

    def test_structured_summary_labels_action_execution_context(self):
        summary = build_structured_summary(
            state_payload(),
            "2026년 07월 13일",
            run_label="rebalance",
        )

        self.assertIn("메르AI 투자 브리핑", summary)


if __name__ == "__main__":
    unittest.main()
