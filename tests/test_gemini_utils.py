import unittest

from gemini_utils import DEFAULT_HTTP_TIMEOUT_MS, is_transient_error


class GeminiUtilsTest(unittest.TestCase):
    def test_default_http_timeout_is_positive(self):
        self.assertGreater(DEFAULT_HTTP_TIMEOUT_MS, 0)

    def test_disconnect_and_timeout_are_retryable(self):
        self.assertTrue(is_transient_error("Server disconnected without sending a response."))
        self.assertTrue(is_transient_error("Read timeout while waiting for response"))


if __name__ == "__main__":
    unittest.main()
