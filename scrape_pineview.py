#!/usr/bin/env python3
"""
Scrape Pineview Reservoir elevation data from USBR 40-Day dataset.
Writes pineview_elevation.json to the repo root.
Intended to run as a GitHub Action on a daily schedule.
"""

import urllib.request
import json
import re
from datetime import datetime

URL = "https://www.usbr.gov/rsvrWater/rsv40Day.html?siteid=946&reservoirtype=Reservoir"
TARGET_ELEVATION = 4835.0
FULL_POOL = 4900.0  # approximate full pool elevation

def fetch_data():
    req = urllib.request.Request(URL, headers={"User-Agent": "OgdenPipelineBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8")

    # Parse the HTML table rows
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

    # Latest reading
    latest = rows[0]
    
    # Daily change (if we have at least 2 rows)
    daily_change = round(latest["elevation"] - rows[1]["elevation"], 2) if len(rows) > 1 else None

    # 7-day average drop rate
    if len(rows) >= 8:
        seven_day_drop = round((rows[7]["elevation"] - latest["elevation"]) / 7, 2)
    else:
        seven_day_drop = abs(daily_change) if daily_change else None

    # Estimated days to target
    remaining = round(latest["elevation"] - TARGET_ELEVATION, 2)
    if seven_day_drop and seven_day_drop > 0:
        est_days = round(remaining / seven_day_drop)
    else:
        est_days = None

    # Progress percentage (from full pool to target)
    total_range = FULL_POOL - TARGET_ELEVATION  # 65 ft
    progress_ft = FULL_POOL - latest["elevation"]
    progress_pct = round(min(max(progress_ft / total_range * 100, 0), 100), 1)

    output = {
        "updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "USBR Water Operations 40-Day Dataset",
        "source_url": URL,
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

    return output

if __name__ == "__main__":
    data = fetch_data()
    with open("pineview_elevation.json", "w") as f:
        json.dump(data, f, indent=2)
    print(f"Elevation: {data['current']['elevation_ft']} ft")
    print(f"Date: {data['current']['date']}")
    print(f"Daily change: {data['trends']['daily_change_ft']} ft")
    print(f"7-day avg drop: {data['trends']['avg_7day_drop_ft']} ft/day")
    print(f"Remaining to target: {data['trends']['remaining_to_target_ft']} ft")
    print(f"Est days to target: {data['trends']['est_days_to_target']}")
    print(f"Progress: {data['trends']['drawdown_progress_pct']}%")
