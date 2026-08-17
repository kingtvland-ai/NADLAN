"""All photos an external ad publishes, fetched per listing on request.

Why this is not part of the harvest
-----------------------------------
AD, Komo and Madlan are harvested from their *search results*, and a search
card carries exactly one photo. That is the whole gallery those rows have ever
had: the stored `raw_json` fragment is a truncated card, so the lightbox opened
on a legacy listing and showed a single picture while the ad itself has ten or
twenty.

The rest of the gallery only exists on the ad page, one HTTP request per
listing. Fetching that for 4,988 rows on the chance someone clicks would be a
crawl of somebody else's site to fill a cache nobody asked for. So it happens
when a person opens the photo, the way `yad2_contact` looks up a phone number,
and the answer is cached so opening the same ad twice costs one request.

What is extracted, and what deliberately is not
----------------------------------------------
Both sites show other people's ads on the same page - "similar listings"
carousels, promoted slots, even cars. Scraping every `<img>` would attach a
neighbour's kitchen to this listing, which is worse than showing one true
photo. So each source is read only inside the container that holds its own
gallery:

* **AD** puts them in ``#product_slider``, as ``<id>-400_<n>.jpg`` for one
  image id and consecutive n. The same id without the size marker is the
  full-resolution file, which is what the lightbox gets.
* **Komo** puts them in ``.tmunotDesktop`` / ``.flgallery``, as
  ``showPic/list/?picSize=b&picNum=<n>``; each photo is its own picNum.
* **Madlan** stores structured JSON with no image reference at all, so it has
  no gallery to find and is reported as such rather than guessed at.

Nothing is downloaded or re-hosted - the URLs point at the publisher's own CDN,
so they serve the image, keep their logs, and can withdraw it at any time.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone

import db
from legacy.tools import http_client

#: How long a cached gallery is trusted. An ad's photos rarely change, and a
#: stale entry costs nothing worse than a missing picture, but a week keeps a
#: refreshed ad from being wrong forever.
MAX_AGE_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS listing_galleries (
    url        TEXT PRIMARY KEY,
    source     TEXT,
    images_json TEXT,
    status     TEXT NOT NULL,
    note       TEXT,
    fetched_at TEXT NOT NULL,
    /* The advertiser's number, when the ad text publishes it. Same fetch as
     * the gallery - one request answers both questions. */
    phone      TEXT
);
"""

_LOCK = threading.Lock()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(listing_galleries)")}
    if "phone" not in columns:
        conn.execute("ALTER TABLE listing_galleries ADD COLUMN phone TEXT")
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fetch_html(url: str, timeout: float = 25.0) -> str:
    session = http_client.build_session()
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


#: Where an ad's own gallery starts, per source.
_CONTAINERS = {
    "ad": re.compile(r'id=["\']product_slider["\']', re.I),
    "komo": re.compile(r'class=["\'][^"\']*(?:tmunotDesktop|flgallery)[^"\']*["\']', re.I),
}

#: Markup that begins somebody else's listings. Both sites put a "similar ads"
#: carousel further down the same page, and reading past it attaches a
#: neighbour's photos to this ad - the failure mode this whole module exists to
#: avoid. The gallery block is cut at the first of these.
_FOREIGN = re.compile(r"sameads|cItemWrap|similar|View_Ad_Details|</main|</footer",
                      re.I)


def _block(html: str, source: str) -> str:
    start = _CONTAINERS[source].search(html)
    if not start:
        return ""
    rest = html[start.end():]
    stop = _FOREIGN.search(rest)
    return rest[:stop.start()] if stop else rest


def _ad_images(block: str) -> list[str]:
    """AD's gallery, at full resolution.

    Every photo of one ad shares a single image id and differs only in the
    trailing index (`<id>-400_0`, `<id>-400_1`, ...), while each *other* ad on
    the page contributes its own id at index 0. Keeping only the first id seen
    is therefore what separates this listing's gallery from its neighbours'.
    Dropping the `-400` size marker gives the original the ad page itself
    displays.
    """
    matches = re.findall(
        r"(?://|https?://)(img\d*\.ad\.co\.il)/([A-Za-z]+Images)/"
        r"(\d+)(?:-\d+)?_(\d+)\.jpg", block, re.I)
    if not matches:
        return []
    own_id = matches[0][2]
    seen, out = set(), []
    for host, folder, image_id, index in matches:
        if image_id != own_id:
            continue
        url = f"https://{host}/{folder}/{image_id}_{index}.jpg"
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _komo_images(block: str) -> list[str]:
    """Komo's gallery. Each photo is a distinct `picNum` on the same endpoint."""
    seen, out = set(), []
    for pic in re.findall(r"showPic/list/\?[^\"'>]*?picNum=(\d+)", block, re.I):
        if pic in seen or pic == "0":
            continue
        seen.add(pic)
        out.append(f"https://www.komo.co.il/api/modaot/tmunot/showPic/list/"
                   f"?picSize=b&picNum={pic}&luachNum=2")
    return out


_EXTRACTORS = {"ad": _ad_images, "komo": _komo_images}

#: Israeli mobile and landline, with the separators people actually type. The
#: negative lookarounds matter: an ad page is full of longer digit runs (ad ids,
#: timestamps, tracking blobs) whose middle happens to look like a number.
_PHONE = re.compile(r"(?<!\d)(0(?:5\d|[2-4]|7\d|8|9)[-\s.]?\d{3}[-\s.]?\d{4})(?!\d)")

#: Some ad pages publish the number in a ``tel:`` link or data attribute rather
#: than visible body text. That is still the ad's own published phone, so it is
#: valid to harvest when the user opens that listing.
_TEL_LINK = re.compile(r"""(?is)
    (?:href|data-phone|data-tel)\s*=\s*["']
    tel:
    (?P<phone>[^"' <]+)
""", re.X)

#: Digits that are a phone number's shape but nobody's phone. Israeli service
#: lines and the placeholder both appear in page furniture.
_NOT_A_SELLER = {"0500000000", "0000000000", "0521234567", "0501234567"}


def _published_phone(html: str) -> str | None:
    """The advertiser's number, when the ad text publishes it.

    Roughly one AD ad in six writes the number into the description ("פרטים
    נוספים בטלפון 05x-xxxxxxx"); the rest keep it behind a reveal button and
    are correctly reported as having none. Scripts are stripped first - an
    analytics payload is a wall of digits, and matching inside one invents a
    phone number that belongs to nobody, which is worse than an empty field.
    """
    text = re.sub(r"(?is)<script.*?</script>", " ", html)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    for candidate in _PHONE.findall(text):
        digits = re.sub(r"\D", "", candidate)
        if digits not in _NOT_A_SELLER:
            return digits

    for candidate in _TEL_LINK.findall(html):
        digits = re.sub(r"\D", "", candidate)
        if digits and digits not in _NOT_A_SELLER:
            phone = digits
            if phone.startswith("972"):
                phone = "0" + phone[3:]
            if len(phone) in (9, 10) and phone.startswith("0"):
                return phone
    return None


def _extract(html: str, source: str) -> list[str]:
    key = (source or "").strip().lower()
    if key not in _CONTAINERS or key not in _EXTRACTORS:
        return []
    block = _block(html, key)
    return _EXTRACTORS[key](block) if block else []


def _cached(conn: sqlite3.Connection, url: str) -> dict | None:
    row = conn.execute(
        "SELECT images_json, status, note, fetched_at, phone FROM listing_galleries "
        "WHERE url = ?", (url,)).fetchone()
    if not row:
        return None
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(row["fetched_at"])).days
    except (TypeError, ValueError):
        age = 10**6
    if age > MAX_AGE_DAYS:
        return None
    return {"images": json.loads(row["images_json"] or "[]"),
            "status": row["status"], "note": row["note"],
            "phone": row["phone"],
            "fetched_at": row["fetched_at"], "cached": True}


def known_phones(urls) -> dict:
    """Phones already learned, keyed by ad URL. Never fetches.

    The gallery request reads the photos and the advertiser's number in one
    pass, so a listing someone has opened once has its number on record. This
    hands those back to the table, which is why a number appears in the row
    afterwards and not only inside the lightbox.
    """
    urls = [u for u in (urls or []) if u]
    if not urls:
        return {}
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        out = {}
        # Chunked: SQLite caps variables per statement, and this is called with
        # a whole page of rows.
        for start in range(0, len(urls), 400):
            chunk = urls[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                    f"SELECT url, phone FROM listing_galleries "
                    f"WHERE phone IS NOT NULL AND url IN ({placeholders})", chunk):
                out[row["url"]] = row["phone"]
        return out
    finally:
        conn.close()


def gallery(url: str, source: str) -> dict:
    """Every photo published on one ad page.

    Returns ``{"images": [...], "status": ...}``. ``status`` is ``ok`` when the
    page was read, ``unsupported`` when the source publishes no gallery, and
    ``failed`` when the fetch itself did not succeed - never an empty list
    pretending the ad has no photos.
    """
    url = (url or "").strip()
    source = (source or "").strip().lower()
    if not url.startswith(("http://", "https://")):
        return {"images": [], "status": "failed", "note": "כתובת מודעה חסרה"}
    if source not in _EXTRACTORS:
        return {"images": [], "status": "unsupported",
                "note": f"למקור {source or '—'} אין גלריה שניתן לקרוא מדף המודעה"}

    conn = db.get_conn()
    try:
        ensure_schema(conn)
        hit = _cached(conn, url)
        if hit:
            return hit
        with _LOCK:
            hit = _cached(conn, url)
            if hit:
                return hit
            try:
                html = _fetch_html(url)
                images = _extract(html, source)
                phone = _published_phone(html)
                status = "ok" if images else "empty"
                note = "" if images else "לא נמצאו תמונות בדף המודעה"
            except Exception as exc:  # network, TLS, WAF, HTTP error
                images, phone, status = [], None, "failed"
                note = f"{type(exc).__name__}: {exc}"[:200]
            conn.execute(
                """INSERT INTO listing_galleries
                        (url, source, images_json, status, note, fetched_at, phone)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                        source=excluded.source, images_json=excluded.images_json,
                        status=excluded.status, note=excluded.note,
                        fetched_at=excluded.fetched_at, phone=excluded.phone""",
                (url, source, json.dumps(images, ensure_ascii=False), status,
                 note, _now(), phone))
            conn.commit()
            return {"images": images, "status": status, "note": note,
                    "phone": phone, "fetched_at": _now(), "cached": False}
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    print(json.dumps(gallery(sys.argv[1], sys.argv[2]), ensure_ascii=False,
                     indent=2))
