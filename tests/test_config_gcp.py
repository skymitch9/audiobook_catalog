"""
Tests for GCP Secret Manager fallback logic in app/config.py.

All tests mock GCP so no real credentials or network calls are made.
"""
import os
import unittest
from unittest.mock import MagicMock, patch


class TestEnvOrSecret(unittest.TestCase):
    """_env_or_secret: env var takes priority; GCP is the silent fallback."""

    def _call(self, name, *aliases, env=None, gcp_return=None):
        from app.config import _env_or_secret
        env = env or {}
        with patch.dict(os.environ, env, clear=False):
            for k in (name, *aliases):
                os.environ.pop(k, None)
            os.environ.update(env)
            with patch("app.config._gcp_secret", return_value=gcp_return) as mock_gcp:
                result = _env_or_secret(name, *aliases)
                return result, mock_gcp

    def test_returns_env_var_when_set(self):
        result, mock_gcp = self._call("__TC_KEY__", env={"__TC_KEY__": "from_env"})
        self.assertEqual(result, "from_env")
        mock_gcp.assert_not_called()

    def test_alias_env_var_is_checked(self):
        result, mock_gcp = self._call("__TC_KEY__", "__TC_ALIAS__", env={"__TC_ALIAS__": "alias_val"})
        self.assertEqual(result, "alias_val")
        mock_gcp.assert_not_called()

    def test_falls_back_to_gcp_when_env_missing(self):
        result, mock_gcp = self._call("__TC_MISSING__", gcp_return="from_gcp")
        self.assertEqual(result, "from_gcp")
        mock_gcp.assert_called_once_with("__TC_MISSING__")

    def test_returns_none_when_both_env_and_gcp_missing(self):
        result, _ = self._call("__TC_MISSING__", gcp_return=None)
        self.assertIsNone(result)

    def test_empty_env_var_falls_back_to_gcp(self):
        result, mock_gcp = self._call("__TC_KEY__", env={"__TC_KEY__": ""}, gcp_return="from_gcp")
        self.assertEqual(result, "from_gcp")
        mock_gcp.assert_called_once()


class TestGcpSecret(unittest.TestCase):
    """_gcp_secret: returns value on success, None on any exception."""

    def _call(self, secret_name, *, side_effect=None, return_value=None):
        from app.config import _gcp_secret
        mock_response = MagicMock()
        mock_response.payload.data.decode.return_value = return_value or ""
        mock_client = MagicMock()
        if side_effect:
            mock_client.access_secret_version.side_effect = side_effect
        else:
            mock_client.access_secret_version.return_value = mock_response

        with patch("google.cloud.secretmanager.SecretManagerServiceClient", return_value=mock_client):
            return _gcp_secret(secret_name)

    def test_returns_secret_value_on_success(self):
        result = self._call("MY_SECRET", return_value="super-secret")
        self.assertEqual(result, "super-secret")

    def test_returns_none_when_secret_not_found(self):
        from google.api_core.exceptions import NotFound
        result = self._call("MISSING_SECRET", side_effect=NotFound("not found"))
        self.assertIsNone(result)

    def test_returns_none_when_not_authenticated(self):
        result = self._call("ANY_SECRET", side_effect=Exception("no credentials"))
        self.assertIsNone(result)

    def test_returns_none_when_import_fails(self):
        from app.config import _gcp_secret
        with patch.dict("sys.modules", {"google.cloud.secretmanager": None}):
            result = _gcp_secret("ANY_SECRET")
            self.assertIsNone(result)

    def test_returns_none_for_empty_secret_value(self):
        result = self._call("EMPTY_SECRET", return_value="")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
