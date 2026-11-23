"""
Register audible-cli for an Audible account using the external-browser login
dance (same flow as the OpenAudible container, no password ever typed here).

Two steps, so the login can happen in a normal browser:

  1) python scripts/audible_cli_auth.py start --name skylar
     Prints the Amazon login URL and saves the pending state
     (code_verifier + serial) to output_files/audible_auth_pending.json.
     Open the URL (INCOGNITO for a second account!), log in, land on the
     broken-looking maplanding page, copy the address-bar URL.

  2) python scripts/audible_cli_auth.py complete --name skylar --url "<maplanding url>"
     Registers the device and writes the auth file to
     ~/.audible/<name>.json, then adds it to the audible-cli config.

⚠️ DEVICE PROFILE (2026-11-23). Since about 2026-10-21 Audible REFUSES download
licences for subscription-included (Plus / AYCL) titles to devices registered
with the old iPhone profile — the symptom is "License not granted to customer"
on included titles while bought titles still download (mkb79/audible-cli#317).
The installed `audible` 0.10.0 registers exactly that old profile, so this
script does the registration call ITSELF with the current one (40-character
serial, app 4.60 — the values `audible` 0.13.1 uses). Do NOT switch back to
`audible.register.register` unless the installed library is >= 0.13.1.

`complete` never loses the account's cached key: the existing auth file is
copied to backup-<date>/ first and its `activation_bytes` are carried into the
new one (a plain re-registration dropped Skylar's on 2026-11-16).

After that, per-book downloads are pure CLI:
  audible -P skylar library list
  audible -P skylar download --asin B0XXXXXXX --aax-fallback -o runtime/openaudible/books
"""

import argparse
import json
import secrets
import shutil
import string
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

import audible
from audible.localization import Locale
from audible.login import build_oauth_url, create_code_verifier

# The device profile Audible currently grants included-title licences to.
# Copied from audible 0.13.1 (audible/register.py); see the module docstring.
APP_VERSION = "4.60"
OS_VERSION = "26.2"
SOFTWARE_VERSION = "46000856"
DEVICE_TYPE = "A2CZJZGLK2JJVM"

PENDING = Path(__file__).resolve().parent.parent / "output_files" / "audible_auth_pending.json"
AUDIBLE_DIR = Path.home() / "AppData" / "Local" / "Audible"  # audible-cli app dir on Windows


def cmd_start(name):
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
    print(f'   python scripts/audible_cli_auth.py complete --name {name} --url "<that url>"')


def register_device(authorization_code, code_verifier, domain, serial):
    """Register a device with the CURRENT profile. Mirrors audible 0.13.1's
    `audible.register.register`; only the three version fields and the serial
    format differ from what the installed 0.10.0 would send."""
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
    success = resp.json()["response"]["success"]
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


def cmd_complete(name, response_url):
    state = json.loads(PENDING.read_text())[name]
    verifier = state["verifier"].encode()
    from urllib.parse import urlparse, parse_qs
    authorization_code = parse_qs(urlparse(response_url).query)["openid.oa2.authorization_code"][0]
    registration = register_device(
        authorization_code=authorization_code, code_verifier=verifier,
        domain="com", serial=state["serial"],
    )
    auth = audible.Authenticator()
    auth.locale = Locale("us")
    auth._update_attrs(with_username=False, **registration)
    AUDIBLE_DIR.mkdir(exist_ok=True)
    auth_file = AUDIBLE_DIR / f"{name}.json"
    if auth_file.exists():
        # Keep the old registration's file and carry the account's cached key.
        backup = AUDIBLE_DIR / f"backup-{date.today().isoformat()}"
        backup.mkdir(exist_ok=True)
        shutil.copy2(auth_file, backup / auth_file.name)
        old_key = json.loads(auth_file.read_text()).get("activation_bytes")
        if old_key:
            auth.activation_bytes = old_key
        print(f"previous auth file kept at {backup / auth_file.name}"
              f" (activation_bytes carried over: {bool(old_key)})")
    auth.to_file(str(auth_file), encryption=False)
    print(f"auth saved: {auth_file} — registered as {registration['device_info'].get('device_name')}")

    # add profile to audible-cli config.toml (create if missing)
    cfg = AUDIBLE_DIR / "config.toml"
    entry = (f'\n[profile.{name}]\n'
             f'auth_file = "{name}.json"\ncountry_code = "us"\n')
    if not cfg.exists():
        cfg.write_text(f'title = "Audible Config File"\n\n[APP]\nprimary_profile = "{name}"\n' + entry)
    elif f"[profile.{name}]" not in cfg.read_text():
        cfg.write_text(cfg.read_text() + entry)
    print(f"profile '{name}' registered — try: audible -P {name} library list | head")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start"); s.add_argument("--name", required=True)
    c = sub.add_parser("complete"); c.add_argument("--name", required=True); c.add_argument("--url", required=True)
    args = parser.parse_args()
    if args.cmd == "start":
        cmd_start(args.name)
    else:
        cmd_complete(args.name, args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
