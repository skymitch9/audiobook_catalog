# app/enrich/hardcover.py
"""Optional build-time Hardcover.app enrichment.

The static GitHub Pages site must not call Hardcover directly because that would
expose the user's API token. This module runs during catalog generation, stores
lookups in a local cache, and publishes only safe derived catalog fields.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

from app.config import (
    HARDCOVER_API_URL,
    HARDCOVER_CACHE_PATH,
    HARDCOVER_ENABLED,
    HARDCOVER_MAX_RESULTS,
    HARDCOVER_MIN_CONFIDENCE,
    HARDCOVER_TIMEOUT_SECONDS,
    HARDCOVER_TOKEN,
)

CatalogRow = Dict[str, Any]
FILL_ONLY_FIELDS = ("desc", "year", "genre", "series")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _first_present(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _is_blank(value: Any) -> bool:
    return value is None or not str(value).strip()


def _hhmm_to_seconds(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 2:
            hours, minutes = int(parts[0]), int(parts[1])
            return (hours * 3600) + (minutes * 60)
        if len(parts) == 3:
            hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
            return (hours * 3600) + (minutes * 60) + seconds
    except ValueError:
        return None
    return None


def _seconds_to_hhmm(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return ""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


def _similarity(left: Any, right: Any) -> float:
    left_norm, right_norm = _norm(left), _norm(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _duration_similarity(local_seconds: Optional[int], remote_seconds: Optional[int]) -> float:
    if not local_seconds or not remote_seconds:
        return 0.0
    diff = abs(local_seconds - remote_seconds)
    # Treat audiobook durations within five minutes as essentially identical.
    if diff <= 300:
        return 1.0
    scale = max(local_seconds, remote_seconds, 1)
    return max(0.0, 1.0 - (diff / scale))


def _collect_names(value: Any) -> List[str]:
    names: List[str] = []
    for item in _as_list(value):
        if item is None:
            continue
        if isinstance(item, str):
            names.extend(part.strip() for part in re.split(r"[,;/&]| and ", item) if part.strip())
            continue
        if isinstance(item, dict):
            for key in ("name", "author", "display_name", "full_name"):
                if item.get(key):
                    names.append(str(item[key]).strip())
            # Search results sometimes nest people under a contribution/author object.
            for nested_key in ("author", "person", "contributor"):
                nested = item.get(nested_key)
                if isinstance(nested, dict) and nested.get("name"):
                    names.append(str(nested["name"]).strip())
    deduped: List[str] = []
    seen = set()
    for name in names:
        key = _norm(name)
        if key and key not in seen:
            seen.add(key)
            deduped.append(name)
    return deduped


def _split_people(value: Any) -> List[str]:
    return _collect_names(value)


def _candidate_url(candidate: CatalogRow) -> str:
    if candidate.get("hardcover_url"):
        return str(candidate["hardcover_url"])
    slug = candidate.get("slug") or candidate.get("hardcover_slug")
    if slug:
        return f"https://hardcover.app/books/{slug}"
    book_id = candidate.get("id") or candidate.get("book_id") or candidate.get("hardcover_book_id")
    if book_id:
        return f"https://hardcover.app/books/{book_id}"
    return ""


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


@dataclass
class HardcoverMatch:
    candidate: CatalogRow
    confidence: float


class HardcoverError(RuntimeError):
    pass


class HardcoverCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: Dict[str, Any] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {"version": 1, "lookups": {}}
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {"version": 1, "lookups": {}}
        if not isinstance(self._data.get("lookups"), dict):
            self._data["lookups"] = {}

    def get(self, key: str) -> Optional[CatalogRow]:
        item = self._data.get("lookups", {}).get(key)
        if isinstance(item, dict):
            return item
        return None

    def set(self, key: str, value: CatalogRow) -> None:
        self._data.setdefault("lookups", {})[key] = value
        self._dirty = True

    def flush(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        self._dirty = False


def cache_key_for_row(row: CatalogRow) -> str:
    parts = [row.get("title", ""), row.get("author", ""), row.get("series", ""), row.get("duration_hhmm", "")]
    raw = "|".join(_norm(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class HardcoverClient:
    def __init__(
        self,
        token: str,
        api_url: str = HARDCOVER_API_URL,
        timeout_seconds: float = HARDCOVER_TIMEOUT_SECONDS,
        max_results: int = HARDCOVER_MAX_RESULTS,
    ) -> None:
        self.token = token
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds
        self.max_results = max(1, int(max_results or 8))

    def _post(self, query: str, variables: CatalogRow) -> CatalogRow:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "audiobook-catalog-hardcover-enrichment/1.0",
        }
        response = requests.post(
            self.api_url,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise HardcoverError(f"Hardcover HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if payload.get("errors"):
            raise HardcoverError(str(payload["errors"][:2]))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise HardcoverError("Hardcover response did not include data")
        return data

    def search(self, row: CatalogRow) -> List[CatalogRow]:
        query_text = " ".join(part for part in [str(row.get("title", "")).strip(), str(row.get("author", "")).strip()] if part)
        if not query_text:
            return []

        candidates: List[CatalogRow] = []
        for searcher in (self._search_endpoint, self._books_ilike):
            try:
                candidates.extend(searcher(row, query_text))
            except HardcoverError as exc:
                print(f"[WARN] Hardcover search strategy failed for {row.get('title', '')!r}: {exc}")
            if candidates:
                break

        if candidates:
            try:
                self._attach_audio_editions(candidates)
            except HardcoverError as exc:
                print(f"[WARN] Hardcover audiobook edition lookup failed for {row.get('title', '')!r}: {exc}")

        return _dedupe_candidates(candidates)

    def _search_endpoint(self, row: CatalogRow, query_text: str) -> List[CatalogRow]:
        query = """
        query SearchBooks($query: String!, $perPage: Int!, $page: Int!) {
          search(query: $query, query_type: "Book", per_page: $perPage, page: $page) {
            ids
            results
          }
        }
        """
        data = self._post(query, {"query": query_text, "perPage": self.max_results, "page": 1})
        search = data.get("search") or {}
        raw_results = search.get("results") or []
        if isinstance(raw_results, str):
            try:
                raw_results = json.loads(raw_results)
            except json.JSONDecodeError:
                raw_results = []

        candidates = [_normalize_candidate(item, source="hardcover_search") for item in _as_list(raw_results)]
        ids = [int(book_id) for book_id in _as_list(search.get("ids")) if str(book_id).isdigit()]
        if ids:
            detail_by_id = {str(c.get("id")): c for c in self._books_by_ids(ids)}
            for candidate in candidates:
                detail = detail_by_id.get(str(candidate.get("id")))
                if detail:
                    candidate.update({k: v for k, v in detail.items() if v not in (None, "", [])})
            missing_ids = [book_id for book_id in ids if str(book_id) not in {str(c.get("id")) for c in candidates}]
            if missing_ids:
                candidates.extend(self._books_by_ids(missing_ids))
        return [c for c in candidates if c.get("title") or c.get("id")]

    def _books_ilike(self, row: CatalogRow, query_text: str) -> List[CatalogRow]:
        title = str(row.get("title", "")).strip()
        if not title:
            return []
        query = """
        query BooksByTitle($titleLike: String!, $limit: Int!) {
          books(where: {title: {_ilike: $titleLike}}, limit: $limit) {
            id
            title
            slug
            release_year
            description
            cached_image
            rating
            ratings_count
            users_count
            has_audiobook
          }
        }
        """
        data = self._post(query, {"titleLike": f"%{title}%", "limit": self.max_results})
        return [_normalize_candidate(item, source="hardcover_books") for item in _as_list(data.get("books"))]

    def _books_by_ids(self, ids: List[int]) -> List[CatalogRow]:
        if not ids:
            return []
        query = """
        query BooksByIds($ids: [Int!]) {
          books(where: {id: {_in: $ids}}) {
            id
            title
            slug
            release_year
            description
            cached_image
            rating
            ratings_count
            users_count
            has_audiobook
          }
        }
        """
        data = self._post(query, {"ids": ids})
        return [_normalize_candidate(item, source="hardcover_books") for item in _as_list(data.get("books"))]

    def _attach_audio_editions(self, candidates: List[CatalogRow]) -> None:
        ids = [_safe_int(c.get("id")) for c in candidates]
        ids = [i for i in ids if i is not None]
        if not ids:
            return
        query = """
        query AudioEditions($bookIds: [Int!], $limit: Int!) {
          editions(where: {book_id: {_in: $bookIds}, audio_seconds: {_gt: 0}}, limit: $limit) {
            id
            book_id
            asin
            audio_seconds
            edition_format
            physical_format
            cached_image
          }
        }
        """
        data = self._post(query, {"bookIds": ids, "limit": max(len(ids) * 3, self.max_results)})
        editions_by_book: Dict[str, List[CatalogRow]] = {}
        for edition in _as_list(data.get("editions")):
            if not isinstance(edition, dict):
                continue
            editions_by_book.setdefault(str(edition.get("book_id")), []).append(edition)
        for candidate in candidates:
            editions = editions_by_book.get(str(candidate.get("id"))) or []
            if not editions:
                continue
            # Prefer the longest/most complete audio edition if several exist.
            best = max(editions, key=lambda item: int(item.get("audio_seconds") or 0))
            candidate["edition"] = best
            candidate["has_audiobook"] = True
            if best.get("audio_seconds"):
                candidate["audio_seconds"] = best.get("audio_seconds")


def _normalize_candidate(item: Any, source: str) -> CatalogRow:
    if not isinstance(item, dict):
        return {"source": source}
    doc = item.get("document") or item.get("book") or item
    if not isinstance(doc, dict):
        doc = item

    authors = _collect_names(
        doc.get("authors")
        or doc.get("author_names")
        or doc.get("cached_authors")
        or doc.get("cached_contributors")
        or doc.get("contributions")
    )

    genres = _collect_label_values(doc.get("genres") or doc.get("cached_genres"))
    tags = _collect_label_values(doc.get("tags") or doc.get("cached_tags"))
    moods = _collect_label_values(doc.get("moods") or doc.get("cached_moods"))
    series = _collect_series_names(
        doc.get("series") or doc.get("series_names") or doc.get("cached_series") or doc.get("book_series")
    )

    normalized: CatalogRow = {
        "id": _first_present(doc.get("id"), doc.get("book_id")),
        "title": _first_present(doc.get("title"), doc.get("title_exact"), doc.get("name")),
        "slug": _first_present(doc.get("slug")),
        "release_year": _first_present(doc.get("release_year"), doc.get("publication_year"), doc.get("year")),
        "description": _first_present(doc.get("description"), doc.get("summary"), doc.get("cached_description")),
        "cached_image": _first_present(doc.get("cached_image"), doc.get("image_url"), doc.get("cover_url")),
        "rating": _first_present(doc.get("rating"), doc.get("average_rating"), doc.get("user_rating")),
        "ratings_count": _first_present(doc.get("ratings_count"), doc.get("rating_count"), doc.get("reviews_count")),
        "users_count": _first_present(doc.get("users_count")),
        "has_audiobook": _truthy(doc.get("has_audiobook") or doc.get("has_audio") or doc.get("audio_seconds")),
        "audio_seconds": _safe_int(doc.get("audio_seconds")),
        "authors": authors,
        "genres": genres,
        "tags": tags,
        "moods": moods,
        "series": series,
        "source": source,
    }
    return normalized


def _collect_label_values(value: Any) -> List[str]:
    labels: List[str] = []
    for item in _as_list(value):
        if item is None:
            continue
        if isinstance(item, str):
            labels.extend(part.strip() for part in item.split(",") if part.strip())
        elif isinstance(item, dict):
            for key in ("name", "tag", "genre", "mood"):
                if item.get(key):
                    labels.append(str(item[key]).strip())
    deduped: List[str] = []
    seen = set()
    for label in labels:
        key = _norm(label)
        if key and key not in seen:
            seen.add(key)
            deduped.append(label)
    return deduped


def _collect_series_names(value: Any) -> List[str]:
    names: List[str] = []
    for item in _as_list(value):
        if item is None:
            continue
        if isinstance(item, str):
            names.extend(part.strip() for part in re.split(r"[,;/]| and ", item) if part.strip())
            continue
        if isinstance(item, dict):
            for key in ("name", "title", "series_name", "cached_name"):
                if item.get(key):
                    names.append(str(item[key]).strip())
            nested = item.get("series")
            if isinstance(nested, str):
                names.append(nested.strip())
            elif isinstance(nested, dict):
                for key in ("name", "title", "series_name"):
                    if nested.get(key):
                        names.append(str(nested[key]).strip())
    deduped: List[str] = []
    seen = set()
    for name in names:
        key = _norm(name)
        if key and key not in seen:
            seen.add(key)
            deduped.append(name)
    return deduped


def _dedupe_candidates(candidates: Iterable[CatalogRow]) -> List[CatalogRow]:
    out: List[CatalogRow] = []
    seen = set()
    for candidate in candidates:
        key = str(candidate.get("id") or candidate.get("slug") or _norm(candidate.get("title")))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def score_candidate(row: CatalogRow, candidate: CatalogRow) -> float:
    title_score = _similarity(row.get("title"), candidate.get("title"))
    score = title_score * 0.62
    weight = 0.62

    row_authors = _split_people(row.get("author"))
    candidate_authors = _split_people(candidate.get("authors"))
    if row_authors and candidate_authors:
        best_author = max(_similarity(a, b) for a in row_authors for b in candidate_authors)
        score += best_author * 0.18
        weight += 0.18

    local_seconds = _hhmm_to_seconds(row.get("duration_hhmm"))
    remote_seconds = _safe_int(candidate.get("audio_seconds"))
    if local_seconds and remote_seconds:
        score += _duration_similarity(local_seconds, remote_seconds) * 0.15
        weight += 0.15

    if candidate.get("has_audiobook"):
        score += 0.05
        weight += 0.05

    if not weight:
        return 0.0
    return round(min(score / weight, 1.0), 4)


def choose_best_match(row: CatalogRow, candidates: Iterable[CatalogRow]) -> Optional[HardcoverMatch]:
    scored = [HardcoverMatch(candidate=c, confidence=score_candidate(row, c)) for c in candidates]
    scored = [item for item in scored if item.confidence > 0]
    if not scored:
        return None
    return max(scored, key=lambda item: item.confidence)


def _candidate_to_fields(match: HardcoverMatch) -> CatalogRow:
    c = match.candidate
    edition = c.get("edition") if isinstance(c.get("edition"), dict) else {}
    audio_seconds = _safe_int(edition.get("audio_seconds") if edition else c.get("audio_seconds"))
    return {
        "hardcover_book_id": _first_present(c.get("id")),
        "hardcover_edition_id": _first_present(edition.get("id") if edition else ""),
        "hardcover_slug": _first_present(c.get("slug")),
        "hardcover_series": ", ".join(c.get("series") or []),
        "hardcover_url": _candidate_url(c),
        "hardcover_match_confidence": f"{match.confidence:.2f}",
        "hardcover_rating": _first_present(c.get("rating")),
        "hardcover_ratings_count": _first_present(c.get("ratings_count")),
        "hardcover_has_audiobook": "yes" if c.get("has_audiobook") else "",
        "hardcover_audio_duration_hhmm": _seconds_to_hhmm(audio_seconds),
        "hardcover_metadata_source": c.get("source") or "hardcover",
    }


def apply_match_to_row(row: CatalogRow, match: HardcoverMatch) -> CatalogRow:
    out = dict(row)
    c = match.candidate
    out.update(_candidate_to_fields(match))

    # Local file tags remain the source of truth. Hardcover fills blanks only.
    if _is_blank(out.get("desc")) and c.get("description"):
        out["desc"] = str(c["description"]).strip()
    if _is_blank(out.get("year")) and c.get("release_year"):
        out["year"] = str(c["release_year"]).strip()
    if _is_blank(out.get("genre")) and c.get("genres"):
        out["genre"] = ", ".join(c.get("genres") or [])
    if _is_blank(out.get("series")) and c.get("series"):
        out["series"] = ", ".join(c.get("series") or [])
    return out


def apply_cached_fields_to_row(row: CatalogRow, fields: CatalogRow) -> CatalogRow:
    out = dict(row)
    for key, value in fields.items():
        if key.startswith("hardcover_"):
            out[key] = value
    for key in FILL_ONLY_FIELDS:
        if _is_blank(out.get(key)) and not _is_blank(fields.get(key)):
            out[key] = fields[key]
    return out


def enrich_rows_with_hardcover(rows: List[CatalogRow]) -> List[CatalogRow]:
    if not HARDCOVER_ENABLED:
        return rows
    if not HARDCOVER_TOKEN:
        print("[WARN] HARDCOVER_ENABLED=true but HARDCOVER_TOKEN is not set; skipping Hardcover enrichment.")
        return rows

    client = HardcoverClient(HARDCOVER_TOKEN)
    cache = HardcoverCache(HARDCOVER_CACHE_PATH)
    enriched: List[CatalogRow] = []
    matched = 0

    for row in rows:
        key = cache_key_for_row(row)
        cached = cache.get(key)
        if cached and cached.get("status") == "matched" and isinstance(cached.get("fields"), dict):
            out = apply_cached_fields_to_row(row, cached["fields"])
            enriched.append(out)
            matched += 1
            continue
        if cached and cached.get("status") == "miss":
            enriched.append(row)
            continue

        try:
            candidates = client.search(row)
            match = choose_best_match(row, candidates)
            if match and match.confidence >= HARDCOVER_MIN_CONFIDENCE:
                out = apply_match_to_row(row, match)
                fields = {k: out.get(k, "") for k in out.keys() if k.startswith("hardcover_")}
                # Preserve any fill-only metadata in the cache too.
                for fill_key in FILL_ONLY_FIELDS:
                    if out.get(fill_key) and out.get(fill_key) != row.get(fill_key):
                        fields[fill_key] = out[fill_key]
                cache.set(key, {"status": "matched", "matched_at": _utc_now(), "fields": fields})
                enriched.append(out)
                matched += 1
            else:
                cache.set(key, {"status": "miss", "matched_at": _utc_now()})
                enriched.append(row)
        except Exception as exc:
            print(f"[WARN] Hardcover enrichment failed for {row.get('title', '')!r}: {exc}")
            enriched.append(row)

    cache.flush()
    print(f"[INFO] Hardcover enrichment matched {matched} of {len(rows)} rows")
    return enriched
