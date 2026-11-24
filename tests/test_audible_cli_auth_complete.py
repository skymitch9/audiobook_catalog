"""`audible_cli_auth.py complete` never wastes a sign-in (review fix 2026-11-24).

* Every precondition that can fail is checked BEFORE the registration POST:
  when one fails, the POST is never attempted (the code stays unspent).
* A write failure AFTER the POST leaves a recovery file, and `recover` finishes
  from it with no Amazon call.
* Backups get unique names: two `complete` runs the same day keep both.

Isolation: an autouse fixture makes the real HTTP call and the real audible-cli
dir fail the test if anything reaches them. Fake tokens only; no real auth file
is ever read.
"""

from __future__ import annotations

import json
import os

import pytest

from scripts import audible_cli_auth as cli

# The real functions, captured before the autouse fixture replaces them.
REAL_RESOLVE = cli.resolve_audible_dir
REAL_REGISTER = cli.register_device

GOOD_URL = "https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=ANcode123&x=1"


class _FakeAuth:
    """Stands in for audible.Authenticator (not installed on CI)."""

    def __init__(self):
        self.data = {}
        self.activation_bytes = None

    def _update_attrs(self, with_username=False, **kw):
        self.data.update(kw)

    def to_dict(self):
        return {**self.data, "activation_bytes": self.activation_bytes}


def _registration():
    return {"adp_token": "fake-adp", "access_token": "fake-access",
            "device_info": {"device_name": "Test's Audible for iPhone"}}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Nothing in this module may reach Amazon or the live audible-cli dir."""
    monkeypatch.setattr(cli, "register_device",
                        lambda **k: pytest.fail("registration attempted without a test fake"))
    monkeypatch.setattr(cli, "resolve_audible_dir",
                        lambda: pytest.fail("live audible-cli dir resolved"))
    monkeypatch.setattr(cli, "PENDING", tmp_path / "output_files" / "audible_auth_pending.json")
    monkeypatch.setattr(cli, "_new_authenticator", _FakeAuth)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A ready-to-complete state: pending sign-in, a tmp audible dir, and a
    registration fake that counts calls."""
    audible_dir = tmp_path / "Audible"
    audible_dir.mkdir()
    cli.PENDING.parent.mkdir(parents=True)
    cli.PENDING.write_text(json.dumps({"tester": {"verifier": "v" * 43, "serial": "s" * 40}}))
    monkeypatch.setattr(cli, "resolve_audible_dir", lambda: audible_dir)

    class _Env:
        dir = audible_dir
        posts = 0

    e = _Env()

    def register(**kw):
        e.posts += 1
        return _registration()

    monkeypatch.setattr(cli, "register_device", register)
    return e


def test_happy_path_writes_profile_and_carries_activation_bytes(env):
    (env.dir / "tester.json").write_text(json.dumps({"activation_bytes": "cafe1234"}))
    cli.cmd_complete("tester", GOOD_URL)
    assert env.posts == 1
    saved = json.loads((env.dir / "tester.json").read_text())
    assert saved["activation_bytes"] == "cafe1234"
    assert saved["adp_token"] == "fake-adp"
    assert "[profile.tester]" in (env.dir / "config.toml").read_text()
    assert not list(env.dir.glob("*.recovery-*"))
    assert not list(env.dir.glob(".*.tmp"))


# ---- preconditions: the POST is never attempted -----------------------------


def test_unparseable_existing_auth_file_refuses_before_registration(env):
    (env.dir / "tester.json").write_text("{not json")
    with pytest.raises(cli.Refused, match="nothing was sent to Amazon"):
        cli.cmd_complete("tester", GOOD_URL)
    assert env.posts == 0
    assert (env.dir / "tester.json").read_text() == "{not json"  # untouched


def test_non_object_existing_auth_file_refuses(env):
    (env.dir / "tester.json").write_text("[1, 2]")
    with pytest.raises(cli.Refused):
        cli.cmd_complete("tester", GOOD_URL)
    assert env.posts == 0


def test_url_without_code_refuses(env):
    with pytest.raises(cli.Refused, match="authorization_code"):
        cli.cmd_complete("tester", "https://www.amazon.com/ap/maplanding?foo=bar")
    assert env.posts == 0


def test_missing_pending_state_refuses(env):
    with pytest.raises(cli.Refused, match="start --name other"):
        cli.cmd_complete("other", GOOD_URL)
    assert env.posts == 0


def test_unwritable_target_refuses(env, monkeypatch):
    def no_temp(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(cli.tempfile, "mkstemp", no_temp)
    with pytest.raises(cli.Refused, match="cannot write"):
        cli.cmd_complete("tester", GOOD_URL)
    assert env.posts == 0


def test_backup_failure_refuses(env, monkeypatch):
    (env.dir / "tester.json").write_text(json.dumps({"activation_bytes": "cafe"}))

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(cli, "_exclusive_write_bytes", boom)
    with pytest.raises(cli.Refused, match="backup"):
        cli.cmd_complete("tester", GOOD_URL)
    assert env.posts == 0


def test_interpreter_without_audible_cli_refuses(monkeypatch):
    """The real resolver, with audible_cli made unimportable."""
    import sys
    monkeypatch.setitem(sys.modules, "audible_cli", None)
    monkeypatch.setitem(sys.modules, "audible_cli.config", None)
    with pytest.raises(cli.Refused, match="py -3.12"):
        REAL_RESOLVE()


def test_real_resolver_asks_audible_cli(monkeypatch, tmp_path):
    """When audible-cli is importable, the dir is ITS answer (here driven by
    AUDIBLE_CONFIG_DIR, which audible-cli honours) — not a hard-coded path."""
    pytest.importorskip("audible_cli")
    monkeypatch.setenv("AUDIBLE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert REAL_RESOLVE() == (tmp_path / "cfg").resolve()


# ---- after the POST: recovery, never silent loss ----------------------------


def test_write_failure_after_registration_leaves_a_recovery_file(env, monkeypatch, capsys):
    (env.dir / "tester.json").write_text(json.dumps({"activation_bytes": "cafe1234"}))
    real_replace = os.replace

    def failing_replace(src, dst):
        if str(dst).endswith("tester.json"):
            raise PermissionError("locked by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(cli.os, "replace", failing_replace)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_complete("tester", GOOD_URL)
    assert env.posts == 1
    msg = str(exc.value)
    recoveries = list(env.dir.glob("tester.recovery-*.json"))
    assert len(recoveries) == 1
    assert str(recoveries[0]) in msg and "recover --name tester" in msg
    assert "fake-adp" not in msg and "fake-adp" not in capsys.readouterr().out  # secrets not printed
    saved = json.loads(recoveries[0].read_text())
    assert saved["registration"]["adp_token"] == "fake-adp"
    assert saved["activation_bytes"] == "cafe1234"
    # the old auth file is intact (atomic write never truncated it)
    assert json.loads((env.dir / "tester.json").read_text()) == {"activation_bytes": "cafe1234"}

    # ... and `recover` finishes it with no second registration
    monkeypatch.setattr(cli.os, "replace", real_replace)
    cli.cmd_recover("tester", str(recoveries[0]))
    assert env.posts == 1
    final = json.loads((env.dir / "tester.json").read_text())
    assert final["adp_token"] == "fake-adp" and final["activation_bytes"] == "cafe1234"


def test_token_validation_failure_after_registration_leaves_recovery(env, monkeypatch):
    class _Strict(_FakeAuth):
        def _update_attrs(self, with_username=False, **kw):
            raise ValueError("adp_token: Invalid token.")
    monkeypatch.setattr(cli, "_new_authenticator", _Strict)
    with pytest.raises(SystemExit, match="REGISTERED at Amazon"):
        cli.cmd_complete("tester", GOOD_URL)
    assert len(list(env.dir.glob("tester.recovery-*.json"))) == 1


def test_real_audible_authenticator_failure_is_recovered(env, monkeypatch):
    """With the real `audible` library (local only): its token validation
    rejecting the fake registration happens AFTER the POST and is recovered."""
    pytest.importorskip("audible")
    import audible
    from audible.localization import Locale

    def real_auth():
        a = audible.Authenticator()
        a.locale = Locale("us")
        return a
    monkeypatch.setattr(cli, "_new_authenticator", real_auth)
    with pytest.raises(SystemExit, match="REGISTERED at Amazon"):
        cli.cmd_complete("tester", GOOD_URL)
    assert len(list(env.dir.glob("tester.recovery-*.json"))) == 1


def test_unparsed_200_response_is_saved(env, monkeypatch):
    def register(**kw):
        env.posts += 1
        raise cli.UnparsedRegistration({"response": {"weird": True}}, KeyError("success"))
    monkeypatch.setattr(cli, "register_device", register)
    with pytest.raises(SystemExit, match="unexpected shape"):
        cli.cmd_complete("tester", GOOD_URL)
    (rec,) = env.dir.glob("tester.recovery-*.json")
    assert json.loads(rec.read_text())["registration"] == {"unparsed_response": {"response": {"weird": True}}}
    with pytest.raises(SystemExit, match="unparsed"):
        cli.cmd_recover("tester", str(rec))


def test_register_device_wraps_a_bad_200_body(monkeypatch):
    """The real register_device, with httpx.post faked."""
    import sys
    import types

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"response": {}}

    fake_httpx = types.SimpleNamespace(post=lambda *a, **k: _Resp())
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    with pytest.raises(cli.UnparsedRegistration):
        REAL_REGISTER(authorization_code="c", code_verifier=b"v", domain="com", serial="s" * 40)


# ---- backups -----------------------------------------------------------------


def test_two_completes_the_same_day_keep_both_backups(env):
    (env.dir / "tester.json").write_text(json.dumps({"activation_bytes": "first", "gen": 1}))
    cli.cmd_complete("tester", GOOD_URL)
    second_original = (env.dir / "tester.json").read_bytes()
    cli.cmd_complete("tester", GOOD_URL)
    backups = sorted((p for p in env.dir.glob("backup-*/*.json")), key=lambda p: p.name)
    assert len(backups) == 2
    contents = {p.read_bytes() for p in backups}
    assert json.dumps({"activation_bytes": "first", "gen": 1}).encode() in contents
    assert second_original in contents


def test_exclusive_write_never_overwrites(tmp_path, monkeypatch):
    class _FrozenDT:
        @staticmethod
        def now():
            import datetime as _d
            return _d.datetime(2026, 11, 24, 12, 0, 0, 0)
    monkeypatch.setattr(cli, "datetime", _FrozenDT)
    a = cli._exclusive_write_bytes(tmp_path, "x", ".json", b"one")
    b = cli._exclusive_write_bytes(tmp_path, "x", ".json", b"two")
    assert a != b and a.read_bytes() == b"one" and b.read_bytes() == b"two"
