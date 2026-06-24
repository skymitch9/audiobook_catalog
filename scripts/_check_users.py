"""Check user accounts breakdown."""
import requests

API_KEY = "AIzaSyDgAblkxzVxl7nFbd7jXOo6PpuNPsJw11Y"
PROJECT = "audiobook-catalog"
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"

# Passphrase accounts (users collection)
r = requests.get(f"{BASE}/users", params={"key": API_KEY})
users = r.json().get("documents", [])
print(f"Passphrase accounts: {len(users)}")
for u in users:
    name = u.get("fields", {}).get("displayName", {}).get("stringValue", "?")
    print(f"  - {name}")

# Profiles (Google users have photoURL)
r2 = requests.get(f"{BASE}/profiles", params={"key": API_KEY})
profiles = r2.json().get("documents", [])
google_count = 0
passphrase_count = 0
print(f"\nAll profiles: {len(profiles)}")
for p in profiles:
    name = p.get("fields", {}).get("displayName", {}).get("stringValue", "?")
    photo = p.get("fields", {}).get("photoURL", {}).get("stringValue", "")
    if photo:
        google_count += 1
        print(f"  - {name} (Google)")
    else:
        passphrase_count += 1
        print(f"  - {name} (Passphrase)")

print(f"\n--- Summary ---")
print(f"  Google SSO: {google_count}")
print(f"  Passphrase: {passphrase_count}")
print(f"  Total profiles: {len(profiles)}")
print(f"  Legacy passphrase accounts (users collection): {len(users)}")
