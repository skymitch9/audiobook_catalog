"""app/tools/audible_version_check.py — weekly "is a newer audible/audible-cli
on PyPI?" (report only). PyPI is never contacted here: ``fetch`` is injected.

Pinned: the comparison is numeric (0.10 > 0.9), a missing install never reads
as "newer available", and one package's network failure does not hide the
other's answer.
"""

from __future__ import annotations

import urllib.error

from app.tools import audible_version_check as avc


def test_is_newer_compares_numerically_not_as_text():
    assert avc.is_newer("0.10.1", "0.10.0") is True
    assert avc.is_newer("0.10.0", "0.9.9") is True
    assert avc.is_newer("0.3.3", "0.3.3") is False
    assert avc.is_newer("0.3.2", "0.3.3") is False


def test_not_installed_or_unknown_latest_is_never_newer():
    assert avc.is_newer("1.0", None) is False
    assert avc.is_newer(None, "1.0") is False


def test_check_reports_each_package_and_survives_one_failure(monkeypatch):
    monkeypatch.setattr(avc, "installed_version", lambda d: {"audible": "0.10.0", "audible-cli": "0.3.3"}[d])

    def _fetch(dist):
        if dist == "audible":
            raise urllib.error.URLError("getaddrinfo failed")
        return "0.3.4"

    got = avc.check(fetch=_fetch)
    assert got["audible"]["error"] and got["audible"]["newer"] is False
    assert got["audible-cli"] == {"installed": "0.3.3", "latest": "0.3.4", "newer": True, "error": None}
    line = avc.describe(got)
    assert "audible-cli 0.3.3 -> 0.3.4 AVAILABLE" in line and "PyPI unreachable" in line


def test_the_request_carries_only_a_generic_user_agent(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"info": {"version": "9.9.9"}}'

    def _urlopen(req, timeout):
        seen["url"], seen["ua"], seen["timeout"] = req.full_url, req.get_header("User-agent"), timeout
        return _Resp()

    monkeypatch.setattr(avc.urllib.request, "urlopen", _urlopen)
    assert avc.latest_on_pypi("audible-cli") == "9.9.9"
    assert seen["url"] == "https://pypi.org/pypi/audible-cli/json"
    assert seen["timeout"] <= 20
    assert seen["ua"] == avc.USER_AGENT
