"""The purchase audit's title match must be whole-word, not substring.

A substring match hid two real purchases on 2026-10-20: "Bound" made
"Bog Standard Isekai: Warbound" look owned, and "Mine" did the same for
"The Eminence in Shadow, Vol. 6".
"""
from app.tools.audit_new_purchases import missing_purchases, norm


def _missing(purchase_titles, catalog_titles):
    books = [{"title": t, "purchase_date": f"2026-10-{i + 1:02d}"} for i, t in enumerate(purchase_titles)]
    return [t for _, t in missing_purchases(books, {norm(c) for c in catalog_titles}, top=100)]


def test_a_short_title_inside_a_longer_WORD_does_not_count_as_owned():
    assert _missing(["Bog Standard Isekai: Warbound"], ["Bound"]) == ["Bog Standard Isekai: Warbound"]
    assert _missing(["The Eminence in Shadow, Vol. 6"], ["Mine"]) == ["The Eminence in Shadow, Vol. 6"]


def test_whole_word_containment_still_matches_in_both_directions():
    # catalog title decorated with series text, purchase uses the short title
    assert _missing(["Firefight"], ["Firefight - The Reckoners, Book 2"]) == []
    # purchase title longer than the catalog's
    assert _missing(["Bog Standard Isekai: Scarred - A LitRPG Isekai Adventure"], ["Bog Standard Isekai: Scarred"]) == []


def test_a_volume_number_is_a_word_so_vol_1_does_not_claim_vol_10():
    assert _missing(["Space Knight Book 10"], ["Space Knight Book 1"]) == ["Space Knight Book 10"]
