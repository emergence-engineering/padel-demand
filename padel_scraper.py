#!/usr/bin/env python3
"""
Budapest padel court occupancy scraper (Playtomic).

Discovers padel clubs in and around Budapest on playtomic.com, pulls the
free-slot availability for the next N days, and computes occupancy estimates
per club / per day / per daypart. Results are written to data/ as JSON + CSV.

Usage:
    python3 padel_scraper.py            # full run: discover + scrape + compute
    python3 padel_scraper.py --days 7   # horizon override

No third-party dependencies (stdlib only), so it runs under any macOS python3.
"""

import argparse
import csv
import json
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://playtomic.com"
TZ = ZoneInfo("Europe/Budapest")
UTC = timezone.utc

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Search queries used for club discovery (Budapest + agglomeration).
SEARCH_QUERIES = [
    "Budapest", "Budaörs", "Szentendre", "Dunakeszi", "Vecsés", "Érd",
    "Törökbálint", "Gödöllő", "Biatorbágy", "Szigetszentmiklós", "Fót",
    "Veresegyház", "Diósd", "Halásztelek", "Pilisvörösvár",
]

BUDAPEST_CENTER = (47.4979, 19.0402)
MAX_KM = 35.0          # keep clubs within this radius of the city center
SLOT_MIN = 30          # occupancy grid resolution in minutes
PRIME = (17, 22)       # prime time window, local hours [start, end)
CORE = (7, 23)         # core daytime window used for the headline metric


def fetch(url, retries=3, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def rsc_blob(html):
    """Join the Next.js RSC payload chunks embedded in a page."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    blob = "".join(c.encode("utf-8").decode("unicode_escape") for c in chunks)
    # unicode_escape mangles multi-byte UTF-8 into latin-1 chars; round-trip repairs it
    return blob.encode("latin-1", errors="replace").decode("utf-8", errors="replace")


def extract_json_object(blob, anchor):
    """Extract the JSON object that starts at the '{' preceding `anchor`."""
    i = blob.find(anchor)
    if i == -1:
        return None
    start = blob.rfind("{", 0, i)
    depth = 0
    for j in range(start, len(blob)):
        ch = blob[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(blob[start:j + 1])
                except json.JSONDecodeError:
                    return None
    return None


def discover_slugs():
    slugs = set()
    for q in SEARCH_QUERIES:
        url = f"{BASE}/search?sport=PADEL&q={urllib.parse.quote(q)}"
        try:
            html = fetch(url)
        except Exception as e:
            print(f"  search '{q}' failed: {e}", file=sys.stderr)
            continue
        found = set(re.findall(r'/clubs/([a-z0-9-]+)', html))
        slugs |= found
        time.sleep(0.4)
    return sorted(slugs)


def haversine_km(a, b):
    from math import radians, sin, cos, asin, sqrt
    la1, lo1, la2, lo2 = map(radians, [a[0], a[1], b[0], b[1]])
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))


def get_tenant(slug):
    html = fetch(f"{BASE}/clubs/{slug}")
    blob = rsc_blob(html)
    t = extract_json_object(blob, '"tenant_id"')
    if not t or "tenant_id" not in t:
        return None
    addr = t.get("address", {})
    coord = addr.get("coordinate", {})
    padel_courts = [r for r in t.get("resources", []) if r.get("sport") == "PADEL"]
    return {
        "slug": slug,
        "tenant_id": t["tenant_id"],
        "name": t.get("tenant_name", slug),
        "city": addr.get("city", ""),
        "street": addr.get("street", ""),
        "postal_code": addr.get("postal_code", ""),
        "country_code": addr.get("country_code", ""),
        "lat": coord.get("lat"),
        "lon": coord.get("lon"),
        "courts": len(padel_courts),
        "court_features": sorted({f for r in padel_courts for f in r.get("features", [])}),
        "resource_ids": [r["resourceId"] for r in padel_courts],
        "opening_hours": t.get("opening_hours", {}),
    }


WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


def open_window_minutes(opening_hours, day):
    """Return (open_min, close_min) local minutes for the given date; 24h -> (0, 1440)."""
    oh = opening_hours.get(WEEKDAYS[day.weekday()])
    if not oh:
        return (0, 1440)
    def to_min(s):
        h, m = map(int, s.split(":")[:2])
        return h * 60 + m
    o, c = to_min(oh.get("opening_time", "00:00")), to_min(oh.get("closing_time", "00:00"))
    if o == 0 and c == 0:
        return (0, 1440)
    if c <= o:
        c = 1440
    return (o, c)


def get_availability(tenant_id, day):
    """Returns (blocks, ok). ok=False means the club does not expose web booking
    (HTTP 403 / non-JSON response) — occupancy cannot be measured for it."""
    url = f"{BASE}/api/clubs/availability?tenant_id={tenant_id}&date={day.isoformat()}&sport_id=PADEL"
    try:
        raw = fetch(url)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return [], False
        raise
    try:
        return json.loads(raw), True
    except json.JSONDecodeError:
        return [], False


def free_cells_for_day(avail, target_day):
    """
    Map the API's UTC free slots onto a per-court set of free 30-min cells of
    the local `target_day`. Returns {resource_id: set(cell_index)}.
    """
    cells = {}
    for block in avail:
        rid = block.get("resource_id")
        d = block.get("start_date")
        for slot in block.get("slots", []):
            h, m, *_ = map(int, slot["start_time"].split(":"))
            start_utc = datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=UTC)
            dur = int(slot.get("duration", 60))
            for k in range(0, dur, SLOT_MIN):
                t_local = (start_utc + timedelta(minutes=k)).astimezone(TZ)
                if t_local.date() == target_day:
                    idx = t_local.hour * 60 // SLOT_MIN + t_local.minute // SLOT_MIN
                    cells.setdefault(rid, set()).add(idx)
    return cells


def window_cells(start_min, end_min):
    return set(range(start_min // SLOT_MIN, end_min // SLOT_MIN))


def occupancy_for_tenant(tenant, days):
    out = []
    n_courts = tenant["courts"]
    if n_courts == 0:
        return out
    rids = set(tenant["resource_ids"])
    now = datetime.now(TZ)
    for day in days:
        try:
            avail, ok = get_availability(tenant["tenant_id"], day)
        except Exception as e:
            print(f"  avail {tenant['slug']} {day}: {e}", file=sys.stderr)
            continue
        if not ok:
            out.append({
                "slug": tenant["slug"], "name": tenant["name"],
                "date": day.isoformat(),
                "weekday": WEEKDAYS[day.weekday()].capitalize(),
                "courts": n_courts, "data_ok": False,
                "occ_open_pct": None, "occ_core_pct": None, "occ_prime_pct": None,
                "open_court_hours": 0, "free_court_hours_open": 0,
                "core_court_hours": 0, "free_court_hours_core": 0,
                "prime_court_hours": 0, "free_court_hours_prime": 0,
            })
            continue
        free = free_cells_for_day(avail, day)
        o_min, c_min = open_window_minutes(tenant["opening_hours"], day)
        # for today only count cells that are still in the future (30 min lead)
        if day == now.date():
            lead = now.hour * 60 + now.minute + 30
            o_min = max(o_min, lead)
            if o_min >= c_min:
                continue  # nothing left of today
        open_cells = window_cells(o_min, c_min)
        core_cells = window_cells(max(o_min, CORE[0] * 60), min(c_min, CORE[1] * 60))
        prime_cells = window_cells(max(o_min, PRIME[0] * 60), min(c_min, PRIME[1] * 60))

        def agg(window):
            total = len(window) * n_courts
            if total == 0:
                return None, 0.0, 0.0
            free_ct = sum(len((free.get(r, set())) & window) for r in rids)
            # courts that never appear in the availability response still count
            # as fully busy/closed -> free_ct simply stays lower.
            occ = 1 - free_ct / total
            return round(occ * 100, 1), total * SLOT_MIN / 60, free_ct * SLOT_MIN / 60

        occ_open, hours_open, free_open = agg(open_cells)
        occ_core, hours_core, free_core = agg(core_cells)
        occ_prime, hours_prime, free_prime = agg(prime_cells)
        # raw per-court free cells (30-min grid, local day) — the historical
        # "final occupancy" reconstruction in generate_report.py needs these
        free_cells = {rid: sorted(cells) for rid, cells in free.items() if rid in rids}
        out.append({
            "slug": tenant["slug"],
            "name": tenant["name"],
            "date": day.isoformat(),
            "weekday": WEEKDAYS[day.weekday()].capitalize(),
            "courts": n_courts,
            "data_ok": True,
            "occ_open_pct": occ_open,
            "occ_core_pct": occ_core,     # 07-23h
            "occ_prime_pct": occ_prime,   # 17-22h
            "open_court_hours": hours_open,
            "free_court_hours_open": round(free_open, 1),
            "core_court_hours": hours_core,
            "free_court_hours_core": round(free_core, 1),
            "prime_court_hours": hours_prime,
            "free_court_hours_prime": round(free_prime, 1),
            "free_cells": free_cells,
        })
        time.sleep(0.25)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--outdir", default=str(Path(__file__).parent / "data"))
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(TZ).date()
    days = [today + timedelta(days=i) for i in range(args.days)]
    stamp = datetime.now(TZ).strftime("%Y-%m-%d_%H%M")

    print("1/3 Discovering clubs…")
    slugs = discover_slugs()
    print(f"    {len(slugs)} candidate slugs")

    print("2/3 Fetching club metadata…")
    tenants = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(get_tenant, s): s for s in slugs}
        for f in as_completed(futs):
            try:
                t = f.result()
            except Exception as e:
                print(f"  club {futs[f]}: {e}", file=sys.stderr)
                continue
            if not t or t["country_code"] != "HU" or not t["lat"] or t["courts"] == 0:
                continue
            t["km_from_center"] = round(haversine_km((t["lat"], t["lon"]), BUDAPEST_CENTER), 1)
            if t["km_from_center"] <= MAX_KM:
                tenants.append(t)
    tenants.sort(key=lambda t: -t["courts"])
    print(f"    {len(tenants)} Budapest-area padel clubs, "
          f"{sum(t['courts'] for t in tenants)} courts")

    print("3/3 Scraping availability…")
    rows = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(occupancy_for_tenant, t, days): t for t in tenants}
        for f in as_completed(futs):
            rows.extend(f.result())
            print(f"    done: {futs[f]['name']}")
    # A club with zero free hours across the whole horizon (including nights)
    # is almost certainly not publishing real inventory -> mark as no-data.
    free_by_slug = {}
    for r in rows:
        free_by_slug[r["slug"]] = free_by_slug.get(r["slug"], 0) + r["free_court_hours_open"]
    for r in rows:
        if free_by_slug.get(r["slug"], 0) == 0:
            r["data_ok"] = False
            r["occ_open_pct"] = r["occ_core_pct"] = r["occ_prime_pct"] = None
    rows.sort(key=lambda r: (r["date"], -(r["occ_core_pct"] or 0)))

    snapshot = {
        "scraped_at": datetime.now(TZ).isoformat(),
        "horizon_days": args.days,
        "clubs": tenants,
        "occupancy": rows,
    }
    jpath = outdir / f"snapshot_{stamp}.json"
    jpath.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1))
    (outdir / "latest.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=1))

    cpath = outdir / f"occupancy_{stamp}.csv"
    if rows:
        fields = [k for k in rows[0] if k != "free_cells"]
        with open(cpath, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    print(f"\nSaved: {jpath.name}, {cpath.name} ({len(rows)} club-day rows)")


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (used in discover_slugs)
    main()
