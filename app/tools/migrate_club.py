"""
Copy a club (doc + all subcollections) between the dev and prod lanes,
preserving every document ID so read references and avatarReadId stay valid.

Copies: the club doc, members, requests, tbr, reads, and each read's
comments / progress / quotes. Overwrites existing docs with the same IDs on
the target lane (idempotent — safe to re-run).

Usage:
    python -m app.tools.migrate_club --club-id <id>              # dev -> prod
    python -m app.tools.migrate_club --club-id <id> --to-dev     # prod -> dev
    python -m app.tools.migrate_club --list                      # show clubs on both lanes
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request

BASE = "https://firestore.googleapis.com/v1/projects/audiobook-catalog/databases/(default)/documents"
API_KEY = "AIzaSyDgAblkxzVxl7nFbd7jXOo6PpuNPsJw11Y"  # public web key; rules gate writes

READ_SUBCOLS = ["comments", "progress", "quotes"]
CLUB_SUBCOLS = ["members", "requests", "tbr", "reads"]


def enc(path):
    return "/".join(urllib.parse.quote(seg, safe="()") for seg in path.split("/"))


def get_doc(path):
    url = f"{BASE}/{enc(path)}?key={API_KEY}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def list_docs(path):
    docs, token = [], None
    while True:
        url = f"{BASE}/{enc(path)}?key={API_KEY}&pageSize=300"
        if token:
            url += f"&pageToken={token}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise
        docs.extend(data.get("documents", []))
        token = data.get("nextPageToken")
        if not token:
            return docs


def put_doc(path, fields):
    """Full-document create-or-replace at an exact path."""
    req = urllib.request.Request(
        f"{BASE}/{enc(path)}?key={API_KEY}",
        data=json.dumps({"fields": fields}).encode(),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def copy_collection(src_col, dst_col):
    n = 0
    for d in list_docs(src_col):
        doc_id = d["name"].split("/")[-1]
        put_doc(f"{dst_col}/{doc_id}", d.get("fields", {}))
        n += 1
    return n


def migrate(club_id, src_root, dst_root):
    club = get_doc(f"{src_root}/{club_id}")
    if club is None:
        print(f"club {club_id} not found in {src_root}")
        return 1
    name = club["fields"].get("name", {}).get("stringValue", "?")
    print(f"migrating '{name}' ({club_id}): {src_root} -> {dst_root}")

    put_doc(f"{dst_root}/{club_id}", club["fields"])
    print("  club doc copied")

    for sub in CLUB_SUBCOLS:
        n = copy_collection(f"{src_root}/{club_id}/{sub}", f"{dst_root}/{club_id}/{sub}")
        print(f"  {sub}: {n}")
        if sub == "reads":
            for read in list_docs(f"{src_root}/{club_id}/reads"):
                read_id = read["name"].split("/")[-1]
                for rsub in READ_SUBCOLS:
                    rn = copy_collection(
                        f"{src_root}/{club_id}/reads/{read_id}/{rsub}",
                        f"{dst_root}/{club_id}/reads/{read_id}/{rsub}",
                    )
                    if rn:
                        print(f"    reads/{read_id}/{rsub}: {rn}")
    print("done")
    return 0


def show_clubs():
    for root in ("clubs", "clubs_dev"):
        docs = list_docs(root)
        names = {d["name"].split("/")[-1]: d["fields"].get("name", {}).get("stringValue", "?")
                 for d in docs}
        print(f"{root}: {json.dumps(names, indent=2)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--club-id", help="club document id to migrate")
    parser.add_argument("--to-dev", action="store_true", help="copy prod -> dev (default dev -> prod)")
    parser.add_argument("--list", action="store_true", help="list clubs on both lanes")
    args = parser.parse_args()

    if args.list:
        show_clubs()
        return 0
    if not args.club_id:
        parser.error("--club-id required (or --list)")
    src, dst = ("clubs", "clubs_dev") if args.to_dev else ("clubs_dev", "clubs")
    return migrate(args.club_id, src, dst)


if __name__ == "__main__":
    sys.exit(main())
