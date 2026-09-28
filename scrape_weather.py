#!/usr/bin/env python3
"""
Fetch estimated rainfall totals and a 7-day forecast for the Pineview
Reservoir / Huntsville, UT job site area from Open-Meteo (free, no API key).

Open-Meteo's daily precipitation figures are a weather-model estimate, not
a physical rain-gauge reading -- this is called out explicitly in the JSON
output and should stay labeled as an estimate anywhere it's displayed.

Writes pineview_weather.json to the repo root.
Intended to run as a GitHub Action on a periodic schedule.
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone

# Huntsville, UT 84317 (Open-Meteo geocoding API, feature id 5776212)
LATITUDE = 41.26077
LONGITUDE = -111.76994
LOCATION_LABEL = "Huntsville, UT 84317"

API_URL = "https://api.open-meteo.com/v1/forecast"

WEATHERCODE_LABELS = {
    0: ("Clear", "\U00002600\U0000FE0F"),
    1: ("Mostly clear", "\U0001F324\U0000FE0F"),
    2: ("Partly cloudy", "\U000026C5"),
    3: ("Overcast", "\U00002601\U0000FE0F"),
    45: ("Fog", "\U0001F32B\U0000FE0F"),
    48: ("Fog", "\U0001F32B\U0000FE0F"),
    51: ("Light drizzle", "\U0001F326\U0000FE0F"),
    53: ("Drizzle", "\U0001F326\U0000FE0F"),
    55: ("Heavy drizzle", "\U0001F326\U0000FE0F"),
    56: ("Freezing drizzle", "\U0001F327\U0000FE0F"),
    57: ("Freezing drizzle", "\U0001F327\U0000FE0F"),
    61: ("Light rain", "\U0001F327\U0000FE0F"),
    63: ("Rain", "\U0001F327\U0000FE0F"),
    65: ("Heavy rain", "\U0001F327\U0000FE0F"),
    66: ("Freezing rain", "\U0001F327\U0000FE0F"),
    67: ("Freezing rain", "\U0001F327\U0000FE0F"),
    71: ("Light snow", "\U0001F328\U0000FE0F"),
    73: ("Snow", "\U0001F328\U0000FE0F"),
    75: ("Heavy snow", "\U0001F328\U0000FE0F"),
    77: ("Snow grains", "\U0001F328\U0000FE0F"),
    80: ("Rain showers", "\U0001F326\U0000FE0F"),
    81: ("Rain showers", "\U0001F326\U0000FE0F"),
    82: ("Heavy showers", "\U000026C8\U0000FE0F"),
    85: ("Snow showers", "\U0001F328\U0000FE0F"),
    86: ("Snow showers", "\U0001F328\U0000FE0F"),
    95: ("Thunderstorm", "\U000026C8\U0000FE0F"),
    96: ("Thunderstorm", "\U000026C8\U0000FE0F"),
    99: ("Thunderstorm", "\U000026C8\U0000FE0F"),
}


def fetch_daily():
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,weathercode",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": "America/Denver",
        # 6 days strictly before today, plus today as day 1 of the 7-day
        # forecast block -- gives us "past week including today" for the
        # rainfall totals and a clean 7-day (today + 6) forecast in one call.
        "past_days": 6,
        "forecast_days": 7,
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "OgdenPipelineBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["daily"]


def build_output(daily):
    dates = daily["time"]
    precip = daily["precipitation_sum"]
    hi = daily["temperature_2m_max"]
    lo = daily["temperature_2m_min"]
    prob = daily["precipitation_probability_max"]
    code = daily["weathercode"]

    today_idx = 6  # index of today given past_days=6

    last_7d_in = round(sum(precip[0:today_idx + 1]), 2)
    last_24h_in = round(precip[today_idx], 2)

    forecast = []
    for i in range(today_idx, len(dates)):
        wc = code[i]
        label, icon = WEATHERCODE_LABELS.get(wc, ("--", "\U00002753"))
        forecast.append({
            "date": dates[i],
            "hi_f": round(hi[i]) if hi[i] is not None else None,
            "lo_f": round(lo[i]) if lo[i] is not None else None,
            "precip_in": round(precip[i], 2),
            "precip_prob_pct": prob[i],
            "weathercode": wc,
            "label": label,
            "icon": icon,
        })

    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "location": LOCATION_LABEL,
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "source": "Open-Meteo forecast model (estimate, not a rain gauge reading)",
        "source_url": "https://open-meteo.com/",
        "rainfall_estimate": {
            "as_of_date": dates[today_idx],
            "last_24h_in": last_24h_in,
            "last_7d_in": last_7d_in,
        },
        "forecast": forecast,
    }


if __name__ == "__main__":
    daily = fetch_daily()
    data = build_output(daily)

    with open("pineview_weather.json", "w") as f:
        json.dump(data, f, indent=2)

    r = data["rainfall_estimate"]
    print(f"As of {r['as_of_date']}: last 24h ~{r['last_24h_in']}in, last 7d ~{r['last_7d_in']}in (estimated)")
    print(f"7-day forecast: {[d['date'] for d in data['forecast']]}")
