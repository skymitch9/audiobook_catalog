"""Pins the pure halves of scripts/sync_shelf_collections.py: which lists a
person/club gets, and what the sync does to the shelf (create / update /
delete) — above all that it never touches a collection it did not make."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sync_shelf_collections as sc  # noqa: E402

A, E = "lib-audio", "lib-ebooks"


def test_lists_split_tbr_from_read_and_fold_reviews_into_read():
    want = sc.desired_lists(
        reading_lists=[
            {"displayName": "Sam", "bookId": "dune", "status": "tbr"},
            {"displayName": "Sam", "bookId": "emma", "status": "read"},
            {"displayName": "", "bookId": "nobody", "status": "tbr"},
        ],
        reviews=[{"displayName": "Sam", "bookId": "it"}, {"displayName": "Jo", "bookId": "it"}],
        clubs=[("Side Babes", [{"bookTitle": "Portal to Nova Roma"}])],
    )
    assert want == {
        "Sam · TBR": {"dune"},
        "Sam · Read & reviewed": {"emma", "it"},
        "Jo · Read & reviewed": {"it"},
        "Book club · Side Babes": {"portal-to-nova-roma"},
    }


def test_a_book_lands_in_each_library_it_is_in():
    ids = {"dune": {A: "a1", E: "e1"}, "emma": {A: "a2"}}
    acts = sc.plan({"Sam · TBR": {"dune", "emma", "not-owned"}}, ids, [A, E], {})
    assert acts == [
        {"op": "create", "lib": A, "name": "Sam · TBR", "books": ["a1", "a2"]},
        {"op": "create", "lib": E, "name": "Sam · TBR", "books": ["e1"]},
    ]


def test_update_is_a_diff_and_an_unchanged_list_is_left_alone():
    ids = {"dune": {A: "a1"}, "emma": {A: "a2"}}
    existing = {(A, "Sam · TBR"): {"id": "c1", "books": ["a1", "a9"]},
                (A, "Jo · TBR"): {"id": "c2", "books": ["a1"]}}
    acts = sc.plan({"Sam · TBR": {"dune", "emma"}, "Jo · TBR": {"dune"}}, ids, [A], existing)
    assert acts == [{"op": "update", "lib": A, "name": "Sam · TBR", "id": "c1",
                     "add": ["a2"], "remove": ["a9"]}]


def test_owned_collections_that_went_empty_or_vanished_are_deleted():
    existing = {(A, "Sam · TBR"): {"id": "c1", "books": ["a1"]},
                (A, "Book club · Gone"): {"id": "c2", "books": ["a1"]}}
    acts = sc.plan({"Sam · TBR": {"not-owned"}}, {}, [A], existing)
    assert {(a["op"], a["id"]) for a in acts} == {("delete", "c1"), ("delete", "c2")}


def test_only_marked_collections_count_as_owned():
    # main() builds `existing` from collections whose description carries the
    # marker; a hand-made one never reaches plan(), so plan() cannot touch it.
    assert sc.MARKER in "x " + sc.MARKER
    acts = sc.plan({}, {}, [A], {})
    assert acts == []
