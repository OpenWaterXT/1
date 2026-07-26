#!/usr/bin/env python3
"""Collect historical World Para Swimming classification master lists.

The collector searches old IPC Swimming / World Para Swimming domains through
Internet Archive's CDX index, inspects archived HTML index pages for linked
files, and downloads relevant documents with full provenance and SHA-256 hashes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse

import requests

CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK = "https://web.archive.org/web/{timestamp}id_/{url}"
USER_AGENT = "WPS-Master-Lists-Archive/2.0 (+https://github.com/OpenWaterXT/1)"

OFFICIAL_HOSTS = (
    "paralympic.org",
    "ipc-services.org",
    "worldparaswimming.org",
    "ipc-swimming.org",
    "paralympic-swimming.org",
)

EXTENSIONS = (".pdf", ".xls", ".xlsx", ".csv", ".zip", ".doc", ".docx", ".ods", ".txt")
DOCUMENT_MIMES = (
    "application/pdf",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "application/zip",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
)

STRONG_TERMS = (
    "master list",
    "masterlist",
    "classification master",
    "classified athletes",
    "classified swimmers",
    "athlete classification",
    "classification database",
    "classification list",
)
SWIM_TERMS = ("swim", "swimming", "world para swimming", "ipc swimming")
CLASS_TERMS = ("classif", "master", "athlete", "swimmer", "database", "list")

# Broad host/path prefixes. CDX wildcards are deliberately avoided; prefix
# matching is more reliable and filtering is performed locally.
SEARCH_PREFIXES = (
    "https://www.paralympic.org/swimming/",
    "https://www.paralympic.org/sites/default/files/document/",
    "https://www.paralympic.org/sites/default/files/",
    "https://www.ipc-swimming.org/",
    "http://www.ipc-swimming.org/",
    "https://ipc-swimming.org/",
    "http://ipc-swimming.org/",
    "https://www.worldparaswimming.org/",
    "http://www.worldparaswimming.org/",
    "https://worldparaswimming.org/",
    "https://www.ipc-services.org/",
    "https://db.ipc-services.org/",
)

SEED_PAGES = (
    "https://www.paralympic.org/swimming/classified-athletes",
    "https://www.paralympic.org/swimming/classification",
    "https://www.paralympic.org/swimming/rules-and-regulations/classification",
    "http://www.ipc-swimming.org/Classifications/",
    "http://www.ipc-swimming.org/Classification/",
    "http://www.ipc-swimming.org/Results/",
)


@dataclass(frozen=True)
class Candidate:
    timestamp: str
    original: str
    mimetype: str
    statuscode: str
    digest: str
    source: str = "cdx"


@dataclass
class ManifestRow:
    year: int
    capture_timestamp: str
    original_url: str
    archived_url: str
    filename: str
    format: str
    sha256: str
    size_bytes: int
    verified_by: str


def official_host(url: str) -> bool:
    host = urlparse(url).netloc.lower().split(":", 1)[0]
    return any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS)


def cdx_query(session: requests.Session, prefix: str, start_year: int, end_year: int,
              *, limit: int = 50000, collapse: str = "digest") -> list[Candidate]:
    params = {
        "url": prefix,
        "matchType": "prefix",
        "from": str(start_year),
        "to": str(end_year),
        "output": "json",
        "fl": "timestamp,original,mimetype,statuscode,digest",
        "filter": "statuscode:200",
        "collapse": collapse,
        "limit": str(limit),
    }
    response = session.get(CDX, params=params, timeout=120)
    response.raise_for_status()
    data = response.json()
    if not data:
        return []
    headers, *rows = data
    idx = {name: i for i, name in enumerate(headers)}
    result = []
    for row in rows:
        try:
            result.append(Candidate(*(row[idx[k]] for k in (
                "timestamp", "original", "mimetype", "statuscode", "digest"
            ))))
        except (IndexError, KeyError):
            continue
    return result


def text_score(value: str) -> int:
    value = html.unescape(unquote(value)).lower().replace("_", " ").replace("-", " ")
    score = 0
    if any(term in value for term in STRONG_TERMS):
        score += 8
    score += 2 * sum(term in value for term in SWIM_TERMS)
    score += sum(term in value for term in CLASS_TERMS)
    if any(value.split("?", 1)[0].endswith(ext) for ext in EXTENSIONS):
        score += 2
    return score


def plausible_document(candidate: Candidate) -> bool:
    if not official_host(candidate.original):
        return False
    url = unquote(candidate.original).lower()
    ext = any(url.split("?", 1)[0].endswith(x) for x in EXTENSIONS)
    mime = candidate.mimetype.lower() in DOCUMENT_MIMES
    return (ext or mime) and text_score(url) >= 4


def plausible_html(candidate: Candidate) -> bool:
    if not official_host(candidate.original):
        return False
    mime = candidate.mimetype.lower()
    return ("html" in mime or not mime) and text_score(candidate.original) >= 2


def extract_links(page_url: str, body: bytes) -> list[str]:
    text = body.decode("utf-8", errors="ignore")
    hrefs = re.findall(r'''(?:href|src)\s*=\s*["']([^"']+)["']''', text, flags=re.I)
    links: list[str] = []
    for href in hrefs:
        href = html.unescape(href.strip())
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        absolute = urljoin(page_url, href)
        # Strip Wayback wrapper if an archived page contains rewritten links.
        match = re.search(r"/web/\d+(?:id_)?/(https?://.+)$", absolute)
        if match:
            absolute = match.group(1)
        if official_host(absolute) and text_score(absolute) >= 3:
            links.append(absolute)
    return links


def archived_capture(session: requests.Session, candidate: Candidate) -> requests.Response | None:
    archived = WAYBACK.format(timestamp=candidate.timestamp, url=candidate.original)
    try:
        response = session.get(archived, timeout=150, allow_redirects=True)
        response.raise_for_status()
        return response
    except requests.RequestException:
        return None


def discover_from_html(session: requests.Session, pages: Iterable[Candidate],
                       start_year: int, end_year: int, delay: float) -> list[Candidate]:
    discovered: dict[tuple[str, str], Candidate] = {}
    for page in pages:
        response = archived_capture(session, page)
        if response is None or len(response.content) < 100:
            continue
        for link in extract_links(page.original, response.content):
            # Search captures of each concrete linked file around the page's year.
            page_year = int(page.timestamp[:4])
            year_from = max(start_year, page_year - 1)
            year_to = min(end_year, page_year + 1)
            try:
                captures = cdx_query(session, link, year_from, year_to, limit=100, collapse="digest")
            except (requests.RequestException, ValueError):
                captures = []
            for capture in captures:
                linked = Candidate(
                    capture.timestamp, capture.original, capture.mimetype,
                    capture.statuscode, capture.digest, source="html-link"
                )
                if plausible_document(linked):
                    discovered[(linked.timestamp, linked.original)] = linked
            time.sleep(delay)
    return list(discovered.values())


def safe_filename(url: str, timestamp: str, content_type: str) -> str:
    path_name = Path(unquote(urlparse(url).path)).name
    path_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path_name).strip("._")
    ext_map = {
        "application/pdf": ".pdf",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "text/csv": ".csv",
        "application/zip": ".zip",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    }
    if not path_name or "." not in path_name:
        path_name = f"master-list-{timestamp}{ext_map.get(content_type.split(';')[0].lower(), '.bin')}"
    return f"{timestamp[:8]}_{path_name}"


def content_is_relevant(data: bytes, content_type: str, url: str) -> bool:
    url_score = text_score(url)
    if url_score >= 7:
        return True
    ctype = content_type.lower()
    if ctype.startswith("text/") or "html" in ctype or data[:1] in (b"<", b"{"):
        text = data[:500000].decode("utf-8", errors="ignore").lower()
        return any(s in text for s in SWIM_TERMS) and any(c in text for c in CLASS_TERMS)
    # Binary files cannot reliably be text-inspected without specialist parsers;
    # require a stronger URL/name score to avoid unrelated documents.
    return url_score >= 5


def download_candidate(session: requests.Session, candidate: Candidate,
                       out_root: Path) -> ManifestRow | None:
    archived = WAYBACK.format(timestamp=candidate.timestamp, url=candidate.original)
    response = archived_capture(session, candidate)
    if response is None:
        return None
    data = response.content
    if not data or len(data) < 100:
        return None
    content_type = response.headers.get("Content-Type", candidate.mimetype or "application/octet-stream")
    if not content_is_relevant(data, content_type, candidate.original):
        return None
    sha = hashlib.sha256(data).hexdigest()
    year = int(candidate.timestamp[:4])
    year_dir = out_root / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(candidate.original, candidate.timestamp, content_type)
    target = year_dir / filename
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != sha:
        target = target.with_name(f"{target.stem}_{sha[:8]}{target.suffix}")
    if not target.exists():
        target.write_bytes(data)
    return ManifestRow(
        year=year,
        capture_timestamp=candidate.timestamp,
        original_url=candidate.original,
        archived_url=archived,
        filename=str(target.relative_to(out_root.parent)),
        format=target.suffix.lower().lstrip(".") or content_type,
        sha256=sha,
        size_bytes=len(data),
        verified_by=f"official-domain + relevance ({candidate.source})",
    )


def write_manifest(rows: Iterable[ManifestRow], out_root: Path,
                   candidate_count: int, html_pages: int) -> None:
    unique: dict[str, ManifestRow] = {}
    for row in rows:
        unique.setdefault(row.sha256, row)
    ordered = sorted(unique.values(), key=lambda r: (r.year, r.capture_timestamp, r.filename))
    fields = list(ManifestRow.__annotations__)
    with (out_root / "manifest.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(row) for row in ordered)
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "documents": len(ordered),
        "years": sorted({r.year for r in ordered}),
        "candidates_checked": candidate_count,
        "archived_html_pages_checked": html_pages,
        "source": "Internet Archive captures of official IPC / World Para Swimming domains",
    }
    (out_root / "summary.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2012)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--max-html-pages", type=int, default=500)
    args = parser.parse_args()
    if args.start_year > args.end_year:
        parser.error("--start-year must be <= --end-year")

    args.output.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    all_captures: dict[tuple[str, str], Candidate] = {}
    for prefix in SEARCH_PREFIXES:
        try:
            captures = cdx_query(session, prefix, args.start_year, args.end_year)
            print(f"CDX {prefix}: {len(captures)} captures")
            for candidate in captures:
                all_captures[(candidate.timestamp, candidate.original)] = candidate
        except (requests.RequestException, ValueError) as exc:
            print(f"Warning: CDX query failed for {prefix}: {exc}")

    # Ensure key pages are searched even if their surrounding path changed.
    for seed in SEED_PAGES:
        try:
            for candidate in cdx_query(session, seed, args.start_year, args.end_year, limit=500):
                all_captures[(candidate.timestamp, candidate.original)] = candidate
        except (requests.RequestException, ValueError):
            pass

    direct_docs = [c for c in all_captures.values() if plausible_document(c)]
    html_pages = sorted(
        (c for c in all_captures.values() if plausible_html(c)),
        key=lambda c: (-text_score(c.original), c.timestamp),
    )[:args.max_html_pages]
    print(f"Direct document candidates: {len(direct_docs)}")
    print(f"Archived HTML pages selected: {len(html_pages)}")

    linked_docs = discover_from_html(
        session, html_pages, args.start_year, args.end_year, args.delay
    )
    print(f"Document candidates discovered from HTML: {len(linked_docs)}")

    candidates: dict[tuple[str, str], Candidate] = {}
    for candidate in direct_docs + linked_docs:
        candidates[(candidate.timestamp, candidate.original)] = candidate
    ordered_candidates = sorted(candidates.values(), key=lambda c: c.timestamp)

    rows: list[ManifestRow] = []
    seen_hashes: set[str] = set()
    for index, candidate in enumerate(ordered_candidates, 1):
        row = download_candidate(session, candidate, args.output)
        if row and row.sha256 not in seen_hashes:
            seen_hashes.add(row.sha256)
            rows.append(row)
            print(f"[{index}/{len(ordered_candidates)}] saved {row.filename}")
        time.sleep(args.delay)

    write_manifest(rows, args.output, len(ordered_candidates), len(html_pages))
    print(f"Unique documents saved: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
