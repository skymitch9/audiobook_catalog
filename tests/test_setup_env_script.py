"""
Tests for scripts/setup_env_from_gcp.py utility functions.

No GCP calls are made — only the pure file I/O helpers are tested.
"""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

# Make the scripts directory importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from setup_env_from_gcp import load_env, write_env


class TestLoadEnv(unittest.TestCase):

    def _write(self, tmp, content):
        p = Path(tmp) / ".env"
        p.write_text(content, encoding="utf-8")
        return p

    def test_parses_simple_key_value(self):
        with TemporaryDirectory() as tmp:
            p = self._write(tmp, "FOO=bar\nBAZ=qux\n")
            result = load_env(p)
            self.assertEqual(result["FOO"], "bar")
            self.assertEqual(result["BAZ"], "qux")

    def test_ignores_comment_lines(self):
        with TemporaryDirectory() as tmp:
            p = self._write(tmp, "# comment\nFOO=bar\n")
            result = load_env(p)
            self.assertNotIn("# comment", result)
            self.assertEqual(result["FOO"], "bar")

    def test_handles_value_with_equals_sign(self):
        with TemporaryDirectory() as tmp:
            p = self._write(tmp, "URL=https://example.com/path?a=1&b=2\n")
            result = load_env(p)
            self.assertEqual(result["URL"], "https://example.com/path?a=1&b=2")

    def test_handles_missing_file(self):
        result = load_env(Path("/nonexistent/.env"))
        self.assertEqual(result, {})

    def test_handles_empty_file(self):
        with TemporaryDirectory() as tmp:
            p = self._write(tmp, "")
            result = load_env(p)
            self.assertEqual(result, {})

    def test_preserves_keys_with_hyphens(self):
        with TemporaryDirectory() as tmp:
            p = self._write(tmp, "Claude-llm=sk-ant-abc123\n")
            result = load_env(p)
            self.assertEqual(result["Claude-llm"], "sk-ant-abc123")


class TestWriteEnv(unittest.TestCase):

    def test_writes_all_keys(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            data = {"FOO": "bar", "BAZ": "qux"}
            write_env(p, data)
            text = p.read_text(encoding="utf-8")
            self.assertIn("FOO=bar", text)
            self.assertIn("BAZ=qux", text)

    def test_roundtrip_preserves_values(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            original = {
                "ROOT_DIR": r"C:\Users\test\books",
                "HARDCOVER_TOKEN": "eyJhbGci.payload.sig",
                "Claude-llm": "sk-ant-key123",
                "HARDCOVER_ENABLED": "true",
            }
            write_env(p, original)
            result = load_env(p)
            self.assertEqual(result, original)

    def test_update_overwrites_existing_key(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            write_env(p, {"FOO": "old"})
            data = load_env(p)
            data["FOO"] = "new"
            write_env(p, data)
            result = load_env(p)
            self.assertEqual(result["FOO"], "new")

    def test_secrets_merged_without_losing_machine_config(self):
        """Simulates fetching secrets from GCP and merging with existing machine-specific .env."""
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            write_env(p, {"ROOT_DIR": r"C:\Users\me\books", "INSPECT_DIR": ""})
            env = load_env(p)
            env["HARDCOVER_TOKEN"] = "new-token-from-gcp"
            env["GITHUB_TOKEN"] = "ghp_abc123"
            write_env(p, env)
            result = load_env(p)
            self.assertEqual(result["ROOT_DIR"], r"C:\Users\me\books")
            self.assertEqual(result["HARDCOVER_TOKEN"], "new-token-from-gcp")
            self.assertEqual(result["GITHUB_TOKEN"], "ghp_abc123")


if __name__ == "__main__":
    unittest.main()
