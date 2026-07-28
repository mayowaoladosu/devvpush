import unittest
from datetime import timedelta
from types import SimpleNamespace

from config import Settings
from dependencies import _refresh_auth_token, decode_jwt_claims
from models import utc_now


class AuthCookieRefreshTests(unittest.TestCase):
    def test_refreshed_cookie_is_a_valid_string_token(self):
        request = SimpleNamespace(state=SimpleNamespace())
        settings = Settings(
            secret_key="test-secret-key",
            auth_token_ttl_days=30,
            auth_token_refresh_threshold_days=1,
        )

        _refresh_auth_token(
            request,
            settings,
            user_id=42,
            expires_at=utc_now() + timedelta(minutes=5),
        )

        token = request.state.auth_cookie_refresh["value"]
        self.assertIsInstance(token, str)
        self.assertFalse(token.startswith("b'"))
        claims = decode_jwt_claims(token, settings, required_type="auth_token")
        self.assertEqual(42, claims["sub"])


if __name__ == "__main__":
    unittest.main()