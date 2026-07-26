#!/usr/bin/env python3
"""Collect historical World Para Swimming classification master lists.

Sources are restricted to official IPC / World Para Swimming URLs discovered
through the Internet Archive CDX index. Downloads are saved with provenance
and SHA-256 hashes in a CSV manifest.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

import requests

CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK = "https://web.archive.org/web/{timestamp}id_/{url}"
USER_AGENT = "WPS-Master-Lists-Archive/1.0 (+https://github.com/OpenWaterXT/1)"
OFFICIAL_HOSTS = ("paralympic.org", "ipc-services.org", "worldparaswimming.org")
EXTENSIONS = (".pdf", ".xls", ".xlsx", ".csv", ".zip", ".doc", ".docx")
KEYWORDS = ("master", "classification", "classified", "athlete", "swimming", "ipc swimming", "world para swimming")

SEARCH_PATTERNS = (
    "paralympic.org/swimming/*",
    "paralympic.org/sites/default/files/document/*",
    "paralympic.org/sites/default/files/*",
    "ipc-services.org/*swimming*",
    "worldparaswimming.org/*",
)

@dataclass(frozen=True)
class Candidate:
    timestamp: str
    original: str
    mimetype: str
    statuscode: str
    digest: str

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


def cdx_query(session: requests.Session, pattern: str, start_year: int, end_year: int) -> list[Candidate]:
    params = {
        "url": pattern,
        "from": str(start_year),
        "to": str(end_year),
        "output": "json",
        "fl": "timestamp,original,mimetype,statuscode,digest",
        "filter": "statuscode:200",
        "collapse": "digest",
        "matchType": "prefix" if pattern.endswith("*") else "exact",
        "limit": "150000",
    }
    response = session.get(CDX, params=params, timeout=90)
    response.raise_for_status()
    data = response.json()
    if not data:
        return []
    headers, *rows = data
    idx = {name: i for i, name in enumerate(headers)}
    return [Candidate(*(row[idx[k]] for k in ("timestamp", "original", "mimetype", "statuscode", "digest"))) for row in rows]


def plausible(candidate: Candidate) -> bool:
    url = unquote(candidate.original).lower()
    host = urlparse(url).netloc.lower()
    if not any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS):
        return False
    ext_ok = any(url.split("?", 1)[0].endswith(ext) for ext in EXTENSIONS)
    keyword_hits = sum(k in url for k in KEYWORDS)
    swimming_context = "swim" in url
    return swimming_context and (ext_ok or keyword_hits >= 2)


def safe_filename(url: str, timestamp: str, content_type: str) -> str:
    path_name = Path(unquote(urlparse(url).path)).name
    path_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path_name).strip("._")
    if not path_name or "." not in path_name:
        ext_map = {
            "application/pdf": ".pdf",
            "application/vnd.ms-excel": ".xls",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "text/csv": ".csv",
            "application/zip": ".zip",
        }
        path_name = f"master-list-{timestamp}{ext_map.get(content_type.split(';')[0].lower(), '.bin')}"
    return f"{timestamp[:8]}_{path_name}"


def content_is_relevant(data: bytes, content_type: str, url: str) -> bool:
    url_l = unquote(url).lower()
    strong_url = "swim" in url_l and ("master" in url_l or "classif" in url_l)
    if strong_url:
        return True
    if content_type.startswith("text/") or data[:1] in (b"<", b"{"):
        text = data[:250000].decode("utf-8", errors="ignore").lower()
        return "swimming" in text and ("master list" in text or "classification" in text)
    return False


def download_candidate(session: requests.Session, candidate: Candidate, out_root: Path) -> ManifestRow | None:
    archived = WAYBACK.format(timestamp=candidate.timestamp, url=candidate.original)
    try:
        response = session.get(archived, timeout=120, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException:
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
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == sha:
        pass
    else:
        if target.exists():
            target = target.with_name(f"{target.stem}_{sha[:8]}{target.suffix}")
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
        verified_by="official-domain + URL/content relevance",
    )


def write_manifest(rows: Iterable[ManifestRow], out_root: Path) -> None:
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
        "source": "Internet Archive captures of official IPC / World Para Swimming domains",
    }
    (out_root / "summary.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2012)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()
    if args.start_year > args.end_year:
        parser.error("--start-year must be <= --end-year")

    args.output.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    all_candidates: dict[tuple[str, str], Candidate] = {}
    for pattern in SEARCH_PATTERNS:
        try:
            for candidate in cdx_query(session, pattern, args.start_year, args.end_year):
                if plausible(candidate):
                    all_candidates[(candidate.timestamp, candidate.original)] = candidate
        except (requests.RequestException, ValueError) as exc:
            print(f"Warning: CDX query failed for {pattern}: {exc}")

    candidates = sorted(all_candidates.values(), key=lambda c: c.timestamp)
    print(f"Plausible archived candidates: {len(candidates)}")
    rows: list[ManifestRow] = []
    seen_hashes: set[str] = set()
    for index, candidate in enumerate(candidates, 1):
        row = download_candidate(session, candidate, args.output)
        if row and row.sha256 not in seen_hashes:
            seen_hashes.add(row.sha256)
            rows.append(row)
            print(f"[{index}/{len(candidates)}] saved {row.filename}")
        time.sleep(args.delay)

    write_manifest(rows, args.output)
    print(f"Unique documents saved: {len(rows)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
