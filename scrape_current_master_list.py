#!/usr/bin/env python3
from __future__ import annotations

import csv, hashlib, json, re
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import Frame, Page, sync_playwright

PARENT_URL = "https://www.paralympic.org/swimming/classified-athletes"
IPC_URL = "https://www.ipc-services.org/sdms/web/cml/swm"
OUT = Path("data")
SEASONS_DIR = OUT / "seasons"
SNAPSHOTS = OUT / "snapshots"
REGIONS = {"AFR", "AMR", "ASR", "OCR"}


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def athlete_key(row: dict[str, str]) -> str:
    raw = "|".join(clean(row.get(k, "")).lower() for k in ("season", "sdms_id", "name", "npc", "gender"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def normalise(headers: list[str], cells: list[str]) -> dict[str, str]:
    aliases = {
        "athlete": "name", "athlete name": "name", "name": "name",
        "family name": "family_name", "given name": "given_name",
        "npc": "npc", "country": "npc", "nation": "npc",
        "gender": "gender", "sex": "gender",
        "sport class": "sport_class", "class": "sport_class",
        "status": "status", "sport class status": "status",
        "review date": "review_date", "fixed review date": "review_date", "frd": "review_date",
        "sdms id": "sdms_id", "id": "sdms_id", "s": "s", "sb": "sb", "sm": "sm", "exceptions": "exceptions",
    }
    row: dict[str, str] = {}
    for i, cell in enumerate(cells):
        source = clean(headers[i] if i < len(headers) else f"column_{i+1}").lower()
        field = aliases.get(source, re.sub(r"[^a-z0-9]+", "_", source).strip("_") or f"column_{i+1}")
        row[field] = clean(cell)
    if not row.get("name"):
        row["name"] = clean(f"{row.get('given_name', '')} {row.get('family_name', '')}")
    return row


def parse_table(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    best: list[dict[str, str]] = []
    for table in soup.find_all("table"):
        heads = table.select("thead th")
        if not heads:
            first = table.find("tr")
            heads = first.find_all(["th", "td"]) if first else []
        headers = [clean(x.get_text(" ", strip=True)) for x in heads]
        rows = []
        for tr in table.select("tbody tr") or table.find_all("tr")[1:]:
            cells = [clean(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
            if len(cells) >= 2:
                rows.append(normalise(headers, cells))
        if len(rows) > len(best):
            best = rows
    return best


def accept_cookies(page: Page) -> None:
    for selector in ["#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll", "button:has-text('Allow all')", "button:has-text('Accept all')"]:
        try:
            button = page.locator(selector).first
            if button.count() and button.is_visible():
                button.click(); page.wait_for_timeout(1200); return
        except Exception:
            pass


def wait_for_frame(page: Page) -> Frame | None:
    for _ in range(50):
        page.wait_for_timeout(400)
        frame = next((f for f in page.frames if "ipc-services.org/sdms/web/cml/swm" in f.url), None)
        if frame:
            return frame
    return None


def options(select) -> list[dict[str, str]]:
    return [{"value": o.get_attribute("value") or "", "text": clean(o.inner_text())} for o in select.locator("option").all()]


def submit_and_read(frame: Frame, page: Page) -> str:
    button = frame.locator("button[name='format'][value='html']").first
    if not button.count():
        raise RuntimeError("Show List button not found")
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/sdms/web/cml/swm" in r.url,
        timeout=60000,
    ) as info:
        button.click()
    response = info.value
    html = response.text()
    page.wait_for_timeout(300)
    return html


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)


def main() -> int:
    OUT.mkdir(exist_ok=True); SEASONS_DIR.mkdir(parents=True, exist_ok=True); SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc); day = now.strftime("%Y-%m-%d")
    diagnostics: dict[str, object] = {"source": PARENT_URL, "embedded_source": IPC_URL, "captured_at": now.isoformat()}
    combined: list[dict[str, str]] = []; season_index: list[dict[str, object]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1200}, locale="en-US", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36")
        page = context.new_page(); page.goto(PARENT_URL, wait_until="domcontentloaded", timeout=120000); page.wait_for_timeout(3000); accept_cookies(page)
        iframe = page.locator("iframe[src*='ipc-services.org']").first
        if not iframe.count(): raise RuntimeError("Official IPC Services iframe not found")
        iframe.evaluate("(el, src) => { el.classList.remove('cookieconsent-optin-statistics'); el.src = src; }", IPC_URL)
        frame = wait_for_frame(page)
        if frame is None: raise RuntimeError("Official IPC Services iframe did not load")
        selects = frame.locator("select")
        if selects.count() < 3: raise RuntimeError(f"Expected 3 selectors, found {selects.count()}")
        available = [o for o in options(selects.nth(0)) if o["value"]]
        diagnostics["available_seasons"] = available

        for pos, season_info in enumerate(available, 1):
            code = season_info["value"].upper(); label = season_info["text"] or code
            try:
                frame.goto(IPC_URL, wait_until="domcontentloaded", timeout=60000); page.wait_for_timeout(400)
                current = frame.locator("select")
                current.nth(0).select_option(season_info["value"]); current.nth(1).select_option(""); current.nth(2).select_option("")
                rows = parse_table(submit_and_read(frame, page))

                # A suspiciously small all-members result means the server retained
                # a previous NPC filter. In that case, rebuild the season NPC by NPC.
                if len(rows) < 500:
                    frame.goto(IPC_URL, wait_until="domcontentloaded", timeout=60000); page.wait_for_timeout(400)
                    npc_options = [o for o in options(frame.locator("select").nth(2)) if re.fullmatch(r"[A-Za-z]{3}", o["value"] or "") and o["value"].upper() not in REGIONS]
                    rebuilt: list[dict[str, str]] = []; seen: set[str] = set(); npc_counts: dict[str, int] = {}
                    for npc in npc_options:
                        frame.goto(IPC_URL, wait_until="domcontentloaded", timeout=60000); page.wait_for_timeout(220)
                        current = frame.locator("select")
                        current.nth(0).select_option(season_info["value"]); current.nth(1).select_option(""); current.nth(2).select_option(npc["value"])
                        found = parse_table(submit_and_read(frame, page)); npc_counts[npc["value"].upper()] = len(found)
                        for row in found:
                            row["npc"] = row.get("npc") or npc["value"].upper()
                            signature = f"{row.get('sdms_id','')}|{row.get('name','')}|{row['npc']}"
                            if signature not in seen: seen.add(signature); rebuilt.append(row)
                    rows = rebuilt; diagnostics.setdefault("npc_counts", {})[code] = npc_counts

                for row in rows:
                    row["season"] = code; row["season_label"] = label; row["athlete_id"] = athlete_key(row)
                fields = sorted({k for row in rows for k in row})
                write_csv(SEASONS_DIR / f"{code}.csv", rows, fields)
                (SEASONS_DIR / f"{code}.json").write_text(json.dumps({"season": code, "label": label, "captured_at": now.isoformat(), "records": len(rows), "fields": fields, "athletes": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
                combined.extend(rows); season_index.append({"season": code, "label": label, "records": len(rows), "json": f"data/seasons/{code}.json", "csv": f"data/seasons/{code}.csv"})
                print(f"[{pos}/{len(available)}] {code}: {len(rows)}")
            except Exception as exc:
                diagnostics.setdefault("season_errors", []).append({"season": code, "error": str(exc)[:500]})
        browser.close()

    diagnostics["seasons_downloaded"] = season_index; diagnostics["rows_extracted"] = len(combined)
    (OUT / "capture_diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
    if not combined: raise RuntimeError("No season returned athletes; previous dataset preserved")
    season_index.sort(key=lambda x: str(x["season"]), reverse=True)
    (OUT / "seasons.json").write_text(json.dumps(season_index, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = sorted({k for row in combined for k in row})
    write_csv(OUT / "latest.csv", combined, fields); write_csv(SNAPSHOTS / f"{day}.csv", combined, fields)
    (OUT / "latest.json").write_text(json.dumps({"captured_at": now.isoformat(), "source": PARENT_URL, "embedded_source": IPC_URL, "records": len(combined), "seasons": season_index, "fields": fields, "snapshot": f"data/snapshots/{day}.csv", "athletes": combined}, ensure_ascii=False, indent=2), encoding="utf-8")
    index_path = OUT / "snapshots.json"
    try: previous = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    except Exception: previous = []
    entry = {"date": day, "captured_at": now.isoformat(), "records": len(combined), "file": f"data/snapshots/{day}.csv"}
    previous = [x for x in previous if x.get("date") != day] + [entry]; previous.sort(key=lambda x: x.get("date", ""), reverse=True)
    index_path.write_text(json.dumps(previous, indent=2), encoding="utf-8")
    print(f"Extracted {len(combined)} records across {len(season_index)} seasons")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
