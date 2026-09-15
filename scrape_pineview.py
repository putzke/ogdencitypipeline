#!/usr/bin/env python3
"""
Fetch Pineview Reservoir elevation/storage/flow data.

Primary source: USBR 40-Day dataset (HTML table scrape).
Fallback source: USACE Sacramento District CDA API, which republishes the
same USBR Salt Lake City raw feed via a public JSON endpoint. This is used
automatically when the USBR page is unreachable or its latest reading is
more than STALE_DAYS old (the USBR report page has occasionally stopped
updating for several days at a time).

Writes pineview_elevation.json to the repo root.
Intended to run as a GitHub Action on a daily schedule.
"""

import urllib.request
import urllib.parse
import json
import re
from datetime import datetime, timedelta

USBR_URL = "https://www.usbr.gov/rsvrWater/rsv40Day.html?siteid=946&reservoirtype=Reservoir"
USACE_BASE = "https://water.usace.army.mil/cda/reporting/providers/SPK/timeseries"
USACE_PAGE_URL = "https://water.usace.army.mil/overview/spk/locations/pineview"

TARGET_ELEVATION = 4835.0
FULL_POOL = 4900.0  # approximate full pool elevation
STALE_DAYS = 2       # if the newest USBR reading is older than this, use the fallback


def fetch_usbr_rows():
    """Scrape the USBR 40-Day HTML report and return rows, newest first."""
    req = urllib.request.Request(USBR_URL, headers={"User-Agent": "OgdenPipelineBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8")

    # Pattern: date | elevation | storage | inflow | release
    pattern = r"(\d{2}-\w{3}-\d{4})\s*</td>\s*<td[^>]*>\s*([\d.]+)\s*</td>\s*<td[^>]*>\s*([\d,-]+)\s*</td>\s*<td[^>]*>\s*([\d,.-]+)\s*</td>\s*<td[^>]*>\s*([\d,.-]+)"
    matches = re.findall(pattern, html)

    if not matches:
        raise ValueError("No data rows found in USBR response")

    rows = []
    for m in matches:
        rows.append({
            "date": m[0],
            "elevation": float(m[1]),
            "storage_af": int(m[2].replace(",", "")),
            "inflow_cfs": int(m[3].replace(",", "")),
            "release_cfs": int(m[4].replace(",", ""))
        })
    return rows


def fetch_usace_fallback_rows(days=15):
    """Fetch the same data from USACE's public CDA API, newest first."""
    end_dt = datetime.utcnow()
    begin_dt = end_dt - timedelta(days=days)
    begin = begin_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    series_map = {
        "elevation": "Pineview.Elev.Inst.~1Day.0.Raw-USBRSLC",
        "storage_af": "Pineview.Stor.Inst.~1Day.0.Raw-USBRSLC",
        "inflow_cfs": "Pineview.Flow-Res In.Ave.~1Day.1Day.Raw-USBRSLC",
        "release_cfs": "Pineview.Flow-Res Out.Ave.~1Day.1Day.Raw-USBRSLC",
    }

    series_data = {}
    for field, name in series_map.items():
        params = urllib.parse.urlencode({"name": name, "begin": begin, "end": end})
        url = f"{USACE_BASE}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "OgdenPipelineBot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        by_date = {}
        for ts, value in payload.get("values", []):
            date_key = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").strftime("%d-%b-%Y")
            by_date[date_key] = value
        series_data[field] = by_date

    if not series_data["elevation"]:
        raise ValueError("USACE fallback returned no elevation data")

    dates = sorted(
        series_data["elevation"].keys(),
        key=lambda d: datetime.strptime(d, "%d-%b-%Y"),
        reverse=True
    )

    rows = []
    for date_key in dates:
        rows.append({
            "date": date_key,
            "elevation": round(float(series_data["elevation"][date_key]), 2),
            "storage_af": int(round(series_data["storage_af"].get(date_key, 0))),
            "inflow_cfs": int(round(series_data["inflow_cfs"].get(date_key, 0))),
            "release_cfs": int(round(series_data["release_cfs"].get(date_key, 0))),
        })
    return rows


def is_stale(rows):
    if not rows:
        return True
    latest_date = datetime.strptime(rows[0]["date"], "%d-%b-%Y")
    return (datetime.utcnow() - latest_date).days > STALE_DAYS


def build_output(rows, source, source_url, source_short, fallback_used):
    latest = rows[0]

    daily_change = round(latest["elevation"] - rows[1]["elevation"], 2) if len(rows) > 1 else None

    if len(rows) >= 8:
        seven_day_drop = round((rows[7]["elevation"] - latest["elevation"]) / 7, 2)
    else:
        seven_day_drop = abs(daily_change) if daily_change else None

    remaining = round(latest["elevation"] - TARGET_ELEVATION, 2)
    if seven_day_drop and seven_day_drop > 0:
        est_days = round(remaining / seven_day_drop)
    else:
        est_days = None

    total_range = FULL_POOL - TARGET_ELEVATION  # 65 ft
    progress_ft = FULL_POOL - latest["elevation"]
    progress_pct = round(min(max(progress_ft / total_range * 100, 0), 100), 1)

    return {
        "updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "source_url": source_url,
        "source_short": source_short,
        "fallback_used": fallback_used,
        "target_elevation": TARGET_ELEVATION,
        "current": {
            "date": latest["date"],
            "elevation_ft": latest["elevation"],
            "storage_af": latest["storage_af"],
            "inflow_cfs": latest["inflow_cfs"],
            "release_cfs": latest["release_cfs"]
        },
        "trends": {
            "daily_change_ft": daily_change,
            "avg_7day_drop_ft": seven_day_drop,
            "remaining_to_target_ft": remaining,
            "est_days_to_target": est_days,
            "drawdown_progress_pct": progress_pct
        },
        "recent_history": rows[:7]
    }


if __name__ == "__main__":
    usbr_rows, usbr_error = None, None
    try:
        usbr_rows = fetch_usbr_rows()
    except Exception as e:
        usbr_error = str(e)
        print(f"USBR fetch failed: {usbr_error}")

    need_fallback = usbr_rows is None or is_stale(usbr_rows)

    fallback_rows, fallback_error = None, None
    if need_fallback:
        try:
            fallback_rows = fetch_usace_fallback_rows()
        except Exception as e:
            fallback_error = str(e)
            print(f"USACE fallback fetch failed: {fallback_error}")

    if usbr_rows is None and fallback_rows is None:
        raise SystemExit(
            f"Both sources failed. USBR error: {usbr_error}; USACE fallback error: {fallback_error}"
        )

    # Prefer the fallback only if we needed it AND it actually gives fresher data
    # than what USBR had (or USBR failed outright).
    use_fallback = fallback_rows is not None and (
        usbr_rows is None or not is_stale(fallback_rows)
    )

    if use_fallback:
        rows = fallback_rows
        data = build_output(
            rows,
            source="USACE Sacramento District CDA (republished USBR SLC raw feed)",
            source_url=USACE_PAGE_URL,
            source_short="USACE (backup)",
            fallback_used=True
        )
        print("Using USACE fallback source (USBR unavailable or stale)")
    else:
        rows = usbr_rows
        data = build_output(
            rows,
            source="USBR Water Operations 40-Day Dataset",
            source_url=USBR_URL,
            source_short="USBR",
            fallback_used=False
        )
        print("Using USBR primary source")

    with open("pineview_elevation.json", "w") as f:
        json.dump(data, f, indent=2)

    print(f"Elevation: {data['current']['elevation_ft']} ft")
    print(f"Date: {data['current']['date']}")
    print(f"Source: {data['source_short']} (fallback_used={data['fallback_used']})")
    print(f"Daily change: {data['trends']['daily_change_ft']} ft")
    print(f"7-day avg drop: {data['trends']['avg_7day_drop_ft']} ft/day")
    print(f"Remaining to target: {data['trends']['remaining_to_target_ft']} ft")
    print(f"Est days to target: {data['trends']['est_days_to_target']}")
    print(f"Progress: {data['trends']['drawdown_progress_pct']}%")
