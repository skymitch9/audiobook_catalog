"""
Register audible-cli for an Audible account using the external-browser login
dance (same flow as the OpenAudible container, no password ever typed here).

Two steps, so the login can happen in a normal browser:

  1) py -3.12 scripts/audible_cli_auth.py start --name skylar
     Prints the Amazon login URL and saves the pending state
     (code_verifier + serial) to output_files/audible_auth_pending.json.
     Open the URL (INCOGNITO for a second account!), log in, land on the
     broken-looking maplanding page, copy the address-bar URL.

  2) py -3.12 scripts/audible_cli_auth.py complete --name skylar --url "<maplanding url>"
     Registers the device and writes the auth file to <audible-cli app
     dir>/<name>.json, then adds it to the audible-cli config.toml there.

⚠️ WHICH DIRECTORY, AND WHICH PYTHON (2026-11-24). The target is whatever
audible-cli ITSELF resolves (`audible_cli.config.get_app_dir()`, which honours
AUDIBLE_CONFIG_DIR). On this box that is the Microsoft Store Python 3.12
package dir, %LOCALAPPDATA%\\Packages\\PythonSoftwareFoundation.Python.3.12_
qbz5n2kfra8p0\\LocalCache\\Local\\Audible — NOT ~/.audible/ and not plain
~/AppData/Local/Audible (that only reached the right place through the Store
package's filesystem redirection; under any other interpreter it was a
directory audible-cli never reads). Run this with the interpreter that has
audible-cli (`py -3.12`); under one without it, `complete` refuses before
contacting Amazon.

⚠️ ONE-SHOT. The authorization code in the maplanding URL is spent by the
registration POST and a device then exists at Amazon. So `complete` does every
check that can fail FIRST (pending state, URL, the existing auth file reads and
parses, a verified backup is written, the target dir is writable) and only then
registers. After the POST the auth file is written atomically; if that still
fails, the raw registration is saved to a `<name>.recovery-<timestamp>.json`
next to it and the run prints the `recover` command that finishes the job:

  3) py -3.12 scripts/audible_cli_auth.py recover --name skylar --file "<recovery file>"

⚠️ DEVICE PROFILE (2026-11-23). Since about 2026-10-21 Audible REFUSES download
licences for subscription-included (Plus / AYCL) titles to devices registered
with the old iPhone profile — the symptom is "License not granted to customer"
on included titles while bought titles still download (mkb79/audible-cli#317).
The installed `audible` 0.10.0 registers exactly that old profile, so this
script does the registration call ITSELF with the current one (40-character
serial, app 4.60 — the values `audible` 0.13.1 uses). Do NOT switch back to
`audible.register.register` unless the installed library is >= 0.13.1.

`complete` never loses the account's cached key: the existing auth file is
copied to backup-<date>/<name>-<HHMMSS-micro>.json first (a unique name, never
overwritten — a second `complete` the same day keeps both) and its
`activation_bytes` are carried into the new one (a plain re-registration
dropped Skylar's on 2026-11-16).

After that, per-book downloads are pure CLI:
  audible -P skylar library list
  audible -P skylar download --asin B0XXXXXXX --aax-fallback -o runtime/openaudible/books
"""

import argparse
import json
import os
import secrets
import string
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# httpx / audible / audible_cli are imported where used: CI does not install
# them (requirements.txt), and the tests exercise this module without them.

# The device profile Audible currently grants included-title licences to.
# Copied from audible 0.13.1 (audible/register.py); see the module docstring.
APP_VERSION = "4.60"
OS_VERSION = "26.2"
SOFTWARE_VERSION = "46000856"
DEVICE_TYPE = "A2CZJZGLK2JJVM"

PENDING = Path(__file__).resolve().parent.parent / "output_files" / "audible_auth_pending.json"


class Refused(SystemExit):
    """A precondition failed. Raised only BEFORE the registration POST, so the
    authorization code is unspent and the user can fix it and re-run."""

    def __init__(self, why):
        super().__init__(f"refused, nothing was sent to Amazon: {why}")


def resolve_audible_dir():
    """The directory audible-cli itself reads, asked of audible-cli in THIS
    interpreter (`get_app_dir()` honours AUDIBLE_CONFIG_DIR). Refuses when this
    interpreter has no audible-cli: audible_download runs `sys.executable -m
    audible_cli`, so a profile written from an interpreter without it lands
    somewhere the downloader may never look."""
    try:
        from audible_cli.config import get_app_dir
    except ImportError:
        raise Refused(
            f"audible-cli is not importable in this interpreter ({sys.executable}), so "
            "its config dir cannot be confirmed. Run this script with the Python that "
            "has audible-cli installed: py -3.12 scripts/audible_cli_auth.py ..."
        )
    return Path(get_app_dir())


def _atomic_write_text(path, text):
    """temp file in the same dir + os.replace: the target is either the old
    file or the complete new one, never a truncated one."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _exclusive_write_bytes(dir_path, stem, suffix, data):
    """Write ``data`` to ``dir_path/<stem>-<HHMMSS-micro>[-n]<suffix>``,
    creating it exclusively — an existing file is never overwritten, however
    fast two runs follow each other. Returns the path."""
    stamp = datetime.now().strftime("%H%M%S-%f")
    for n in range(1000):
        candidate = dir_path / f"{stem}-{stamp}{'' if n == 0 else f'-{n}'}{suffix}"
        try:
            with open(candidate, "xb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"no free name for {stem}{suffix} in {dir_path}")


def _check_writable(dir_path):
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(dir=str(dir_path), prefix=".write-probe-")
        os.close(fd)
        os.unlink(probe)
    except OSError as e:
        raise Refused(f"cannot write to {dir_path}: {e}")


def _backup_existing(auth_file):
    """Copy ``auth_file`` into backup-<date>/ under a never-reused name and
    verify the copy byte-for-byte. Returns (backup_path, activation_bytes)."""
    try:
        raw = auth_file.read_bytes()
        old = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as e:
        raise Refused(f"the existing {auth_file.name} cannot be read as JSON ({type(e).__name__}); "
                      "move it aside or repair it first — its activation_bytes would be lost")
    if not isinstance(old, dict):
        raise Refused(f"the existing {auth_file.name} is not a JSON object")
    backup_dir = auth_file.parent / f"backup-{date.today().isoformat()}"
    try:
        backup_dir.mkdir(exist_ok=True)
        backup = _exclusive_write_bytes(backup_dir, auth_file.stem, auth_file.suffix, raw)
        ok = backup.read_bytes() == raw
    except OSError as e:
        raise Refused(f"could not write a backup of {auth_file.name} in {backup_dir}: {e}")
    if not ok:
        raise Refused(f"the backup {backup} does not match {auth_file.name}")
    return backup, old.get("activation_bytes")


def _new_authenticator():
    """Imported and constructed before the POST, so a broken `audible`
    install refuses instead of failing after the code is spent."""
    try:
        import audible
        from audible.localization import Locale
    except ImportError as e:
        raise Refused(f"the `audible` library is not importable in {sys.executable}: {e}")
    auth = audible.Authenticator()
    auth.locale = Locale("us")
    return auth


def _auth_json(auth, registration, activation_bytes):
    """Fill the Authenticator from a registration and serialise it exactly as
    `Authenticator.to_file(encryption=False)` does (indent 4). `_update_attrs`
    VALIDATES the tokens, so this can raise — it runs after the POST and is
    covered by the recovery file."""
    auth._update_attrs(with_username=False, **registration)
    if activation_bytes:
        auth.activation_bytes = activation_bytes
    return json.dumps(auth.to_dict(), indent=4)


def _write_recovery(audible_dir, name, registration, activation_bytes):
    """Save the raw registration so the spent sign-in is not lost. Contains
    live tokens: its CONTENTS are never printed, only its path."""
    payload = json.dumps({"name": name, "registration": registration,
                          "activation_bytes": activation_bytes}).encode("utf-8")
    for d in (audible_dir, PENDING.parent):
        try:
            d.mkdir(parents=True, exist_ok=True)
            return _exclusive_write_bytes(d, f"{name}.recovery", ".json", payload)
        except OSError:
            continue
    return None


def _install_profile(audible_dir, name, auth, registration, activation_bytes):
    """Everything after the POST. Raises on failure; the caller recovers."""
    auth_file = audible_dir / f"{name}.json"
    _atomic_write_text(auth_file, _auth_json(auth, registration, activation_bytes))
    device = (registration.get("device_info") or {}).get("device_name")
    print(f"auth saved: {auth_file} — registered as {device}")

    # add profile to audible-cli config.toml (create if missing)
    cfg = audible_dir / "config.toml"
    entry = (f'\n[profile.{name}]\n'
             f'auth_file = "{name}.json"\ncountry_code = "us"\n')
    if not cfg.exists():
        _atomic_write_text(cfg, f'title = "Audible Config File"\n\n[APP]\nprimary_profile = "{name}"\n' + entry)
    elif f"[profile.{name}]" not in cfg.read_text(encoding="utf-8"):
        _atomic_write_text(cfg, cfg.read_text(encoding="utf-8") + entry)
    print(f"profile '{name}' registered — try: audible -P {name} library list | head")


def cmd_start(name):
    from audible.login import build_oauth_url, create_code_verifier

    verifier = create_code_verifier()
    serial = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(40))
    url, _ = build_oauth_url(
        country_code="us", domain="com", market_place_id="AF2M0KC94RCEA",
        code_verifier=verifier, serial=serial, with_username=False,
    )
    PENDING.parent.mkdir(exist_ok=True)
    state = {}
    if PENDING.exists():
        state = json.loads(PENDING.read_text())
    state[name] = {"verifier": verifier.decode() if isinstance(verifier, bytes) else verifier,
                   "serial": serial}
    PENDING.write_text(json.dumps(state))
    print("1) Open this URL (INCOGNITO if it's not the browser's signed-in account):\n")
    print(url)
    print("\n2) Log in, land on the maplanding page, copy the address-bar URL, then run:")
    print(f'   py -3.12 scripts/audible_cli_auth.py complete --name {name} --url "<that url>"')


def register_device(authorization_code, code_verifier, domain, serial):
    """Register a device with the CURRENT profile. Mirrors audible 0.13.1's
    `audible.register.register`; only the three version fields and the serial
    format differ from what the installed 0.10.0 would send."""
    import httpx

    body = {
        "requested_token_type": ["bearer", "mac_dms", "website_cookies",
                                 "store_authentication_cookie"],
        "cookies": {"website_cookies": [], "domain": f".amazon.{domain}"},
        "registration_data": {
            "domain": "Device",
            "app_version": APP_VERSION,
            "device_serial": serial,
            "device_type": DEVICE_TYPE,
            "device_name": ("%FIRST_NAME%%FIRST_NAME_POSSESSIVE_STRING%%DUPE_"
                            "STRATEGY_1ST%Audible for iPhone"),
            "os_version": OS_VERSION,
            "software_version": SOFTWARE_VERSION,
            "device_model": "iPhone",
            "app_name": "Audible",
        },
        "auth_data": {
            "client_id": (serial.encode() + b"#" + DEVICE_TYPE.encode()).hex(),
            "authorization_code": authorization_code,
            "code_verifier": code_verifier.decode(),
            "code_algorithm": "SHA-256",
            "client_domain": "DeviceLegacy",
        },
        "requested_extensions": ["device_info", "customer_info"],
    }
    resp = httpx.post(f"https://api.amazon.{domain}/auth/register", json=body)
    if resp.status_code != 200:
        # Amazon's error body names the cause and carries no credential.
        raise SystemExit(f"registration refused ({resp.status_code}): {resp.text[:400]}")
    try:
        raw = resp.json()
    except ValueError:
        raw = resp.text
    try:
        return _parse_registration(raw)
    except Exception as e:
        # 200 = the device exists and the code is spent; keep what Amazon sent.
        raise UnparsedRegistration(raw, e)


class UnparsedRegistration(Exception):
    """A 200 from /auth/register whose body did not have the expected shape."""

    def __init__(self, raw, cause):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.raw = raw


def _parse_registration(raw):
    success = raw["response"]["success"]
    tokens = success["tokens"]
    expires = (datetime.now(timezone.utc)
               + timedelta(seconds=int(tokens["bearer"]["expires_in"]))).timestamp()
    cookies = {c["Name"]: c["Value"].replace('"', "")
               for c in tokens.get("website_cookies", [])} or None
    return {
        "adp_token": tokens["mac_dms"]["adp_token"],
        "device_private_key": tokens["mac_dms"]["device_private_key"],
        "access_token": tokens["bearer"]["access_token"],
        "refresh_token": tokens["bearer"]["refresh_token"],
        "expires": expires,
        "website_cookies": cookies,
        "store_authentication_cookie": tokens.get("store_authentication_cookie"),
        "device_info": success["extensions"]["device_info"],
        "customer_info": success["extensions"]["customer_info"],
    }


def _preflight_target(name):
    """Every check on the destination: dir is audible-cli's, writable, and an
    existing auth file is parsed and backed up. Returns
    (audible_dir, activation_bytes, authenticator)."""
    audible_dir = resolve_audible_dir()
    _check_writable(audible_dir)
    cfg = audible_dir / "config.toml"
    if cfg.exists():
        try:
            cfg.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise Refused(f"{cfg} cannot be read ({type(e).__name__}); the profile could not be added to it")
    auth_file = audible_dir / f"{name}.json"
    old_key = None
    if auth_file.exists():
        backup, old_key = _backup_existing(auth_file)
        print(f"previous auth file kept at {backup}"
              f" (activation_bytes carried over: {bool(old_key)})")
    return audible_dir, old_key, _new_authenticator()


def cmd_complete(name, response_url):
    from urllib.parse import parse_qs, urlparse

    # ---- preflight: NOTHING here contacts Amazon; any failure is a Refused
    # and the one-time authorization code stays unspent.
    try:
        state = json.loads(PENDING.read_text(encoding="utf-8"))[name]
        verifier = state["verifier"].encode()
        serial = state["serial"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise Refused(f"no usable pending sign-in for '{name}' in {PENDING} ({type(e).__name__}); "
                      f"run: py -3.12 scripts/audible_cli_auth.py start --name {name}")
    codes = parse_qs(urlparse(response_url).query).get("openid.oa2.authorization_code")
    if not codes or not codes[0]:
        raise Refused("the --url has no openid.oa2.authorization_code — copy the whole "
                      "maplanding address-bar URL")
    audible_dir, old_key, auth = _preflight_target(name)

    # ---- point of no return: the code is spent and a device exists at Amazon.
    try:
        registration = register_device(
            authorization_code=codes[0], code_verifier=verifier,
            domain="com", serial=serial,
        )
    except UnparsedRegistration as e:
        recovery = _write_recovery(audible_dir, name, {"unparsed_response": e.raw}, old_key)
        raise SystemExit(
            f"REGISTERED at Amazon, but the response had an unexpected shape ({e}). "
            f"The raw response is saved at {recovery} (live tokens — do not share or print it); "
            "`recover` cannot read this shape — it needs a code change to _parse_registration."
        )
    try:
        _install_profile(audible_dir, name, auth, registration, old_key)
    except Exception as e:
        recovery = _write_recovery(audible_dir, name, registration, old_key)
        if recovery is None:
            raise SystemExit(
                f"REGISTERED at Amazon but could not save the result anywhere ({type(e).__name__}: {e}). "
                "The sign-in is lost: remove the new 'Audible for iPhone' device under Amazon > "
                "Manage Your Content and Devices > Devices, then start over with `start`."
            )
        raise SystemExit(
            f"REGISTERED at Amazon, but writing the profile failed ({type(e).__name__}: {e}).\n"
            f"The registration is saved (it holds live tokens — do not share or print it) at:\n"
            f"  {recovery}\n"
            "Fix the cause, then finish WITHOUT signing in again:\n"
            f'  py -3.12 scripts/audible_cli_auth.py recover --name {name} --file "{recovery}"\n'
            "Do NOT run `start`/`complete` again for this — that registers a second device."
        )


def cmd_recover(name, recovery_file):
    """Finish a `complete` whose post-registration write failed, from the
    recovery file it saved. No Amazon call."""
    try:
        saved = json.loads(Path(recovery_file).read_text(encoding="utf-8"))
        registration = saved["registration"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"cannot read the recovery file ({type(e).__name__}); nothing changed")
    if not isinstance(registration, dict) or "unparsed_response" in registration:
        raise SystemExit("that recovery file holds an unparsed Amazon response; `recover` cannot "
                         "use it until _parse_registration handles that shape; nothing changed")
    if saved.get("name") != name:
        raise SystemExit(f"that recovery file is for '{saved.get('name')}', not '{name}'; nothing changed")
    audible_dir, file_key, auth = _preflight_target(name)
    _install_profile(audible_dir, name, auth, registration, saved.get("activation_bytes") or file_key)
    print(f"recovered. Once `audible -P {name} library list` works, delete {recovery_file}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start"); s.add_argument("--name", required=True)
    c = sub.add_parser("complete"); c.add_argument("--name", required=True); c.add_argument("--url", required=True)
    r = sub.add_parser("recover"); r.add_argument("--name", required=True); r.add_argument("--file", required=True)
    args = parser.parse_args()
    if args.cmd == "start":
        cmd_start(args.name)
    elif args.cmd == "recover":
        cmd_recover(args.name, args.file)
    else:
        cmd_complete(args.name, args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
