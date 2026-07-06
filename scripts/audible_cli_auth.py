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

After that, per-book downloads are pure CLI:
  audible -P skylar library list
  audible -P skylar download --asin B0XXXXXXX --aax-fallback -o runtime/openaudible/books
"""

import argparse
import json
import sys
from pathlib import Path

import audible
from audible.localization import Locale
from audible.login import build_oauth_url, create_code_verifier
from audible.register import register as register_device

PENDING = Path(__file__).resolve().parent.parent / "output_files" / "audible_auth_pending.json"
AUDIBLE_DIR = Path.home() / ".audible"


def cmd_start(name):
    verifier = create_code_verifier()
    import secrets
    serial = secrets.token_hex(16).upper()
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


def cmd_complete(name, response_url):
    state = json.loads(PENDING.read_text())[name]
    verifier = state["verifier"].encode()
    from audible.login import extract_code_from_url
    authorization_code = extract_code_from_url(response_url)
    registration = register_device(
        authorization_code=authorization_code, code_verifier=verifier,
        domain="com", serial=state["serial"],
    )
    auth = audible.Authenticator()
    auth.locale = Locale("us")
    auth._update_attrs(with_username=False, **registration)
    AUDIBLE_DIR.mkdir(exist_ok=True)
    auth_file = AUDIBLE_DIR / f"{name}.json"
    auth.to_file(str(auth_file), encryption=False)
    print(f"auth saved: {auth_file}")

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
