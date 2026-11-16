"""Titles Audible no longer licenses are recorded once and never re-downloaded."""

from app.tools import audible_download, auto_acquire, license_refused

REFUSAL = ("download failed for B0X (T): Fallback to aaxc for T\n"
           "error: License not granted to customer [X] for asin [B0X]\nNo new files downloaded.")


def test_is_license_refusal_matches_only_the_refusal():
    assert license_refused.is_license_refusal(RuntimeError(REFUSAL))
    assert not license_refused.is_license_refusal(RuntimeError("ffmpeg convert failed: boom"))
    assert not license_refused.is_license_refusal(None)


def test_record_load_forget_roundtrip(tmp_path):
    store = tmp_path / "license_refused.json"
    assert license_refused.load(store) == {}
    assert license_refused.record("B0X", "T", "skylar", REFUSAL, path=store) is True
    assert license_refused.record("B0X", "T", "skylar", REFUSAL, path=store) is False
    assert license_refused.load(store)["B0X"]["title"] == "T"
    assert license_refused.forget("B0X", path=store) is True
    assert license_refused.forget("B0X", path=store) is False
    assert license_refused.load(store) == {}


def test_download_missing_records_refusal_and_keeps_real_failures(tmp_path, monkeypatch, capsys):
    store = tmp_path / "license_refused.json"
    monkeypatch.setattr(license_refused, "STORE", store)

    def fake(asin, title, profile, narrator=None):
        if asin == "B0X":
            raise RuntimeError(REFUSAL)
        raise RuntimeError("ffmpeg convert failed: boom")

    monkeypatch.setattr(audible_download, "download_and_convert", fake)
    books = [{"title": "T", "asin": "B0X", "profile": "skylar"},
             {"title": "U", "asin": "B0Y", "profile": "skylar"}]
    downloaded, failed, refused = auto_acquire.download_missing(
        [("2026-01-01", "T"), ("2026-01-02", "U")], books)

    assert downloaded == [] and refused == ["T"]
    assert [t for t, _ in failed] == ["U"]
    assert list(license_refused.load(store)) == ["B0X"]
    out = capsys.readouterr().out
    # purchase_audit counts lines that START with "FAILED:" as errors
    assert [ln for ln in out.splitlines() if ln.startswith("FAILED:")] == [
        "FAILED: U: ffmpeg convert failed: boom"]
