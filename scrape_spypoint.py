#!/usr/bin/env python3
"""
Fetches the newest photo from the Pineview crossing Spypoint trail camera and
rebuilds a rolling timelapse GIF covering the trailing TIMELAPSE_WINDOW_HOURS.

Requires SPYPOINT_USERNAME and SPYPOINT_PASSWORD as environment variables —
set these as GitHub Actions repository secrets (Settings > Secrets and
variables > Actions). Never hard-code them here.

Uses the unofficial pyspypoint client (https://github.com/hstern/pyspypoint),
which talks to Spypoint's undocumented restapi.spypoint.com. This is a
reverse-engineered API with no support guarantee from Spypoint — if this
script starts failing, check https://github.com/hstern/pyspypoint/issues
for a schema change before assuming the camera itself is offline.

Files touched:
  - images/pineview-cam-latest.jpg   (committed — overwritten each run)
  - images/pineview-cam-timelapse.gif (committed — overwritten each run)
  - pineview_cam.json                 (committed — small status file for the page)
  - cam_frame_buffer/                 (NOT committed — persisted via actions/cache
                                        between runs so we don't bloat git history
                                        with every individual frame)
"""

import json
import os
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

try:
    import spypoint
except ImportError:
    print("pyspypoint is not installed. Run: pip install pyspypoint", file=sys.stderr)
    raise

USERNAME = os.environ.get("SPYPOINT_USERNAME")
PASSWORD = os.environ.get("SPYPOINT_PASSWORD")

FRAME_BUFFER_DIR = Path("cam_frame_buffer")
LATEST_PHOTO_PATH = Path("images/pineview-cam-latest.jpg")
TIMELAPSE_PATH = Path("images/pineview-cam-timelapse.gif")
METADATA_PATH = Path("pineview_cam.json")
LAST_SEEN_PATH = FRAME_BUFFER_DIR / ".last_photo_id"

TIMELAPSE_WINDOW_HOURS = 6
GIF_FRAME_DURATION_MS = 250
GIF_MAX_DIMENSION = 900  # downscale frames for a reasonably small GIF


def parse_photo_date(photo, fallback):
    raw = getattr(photo, "date", None)
    if not raw:
        return fallback
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return fallback


def download(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": "OgdenPipelineBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        dest_path.write_bytes(resp.read())


def prune_old_frames(now):
    cutoff = now.timestamp() - TIMELAPSE_WINDOW_HOURS * 3600
    for f in FRAME_BUFFER_DIR.glob("*.jpg"):
        try:
            ts = float(f.stem)
        except ValueError:
            continue
        if ts < cutoff:
            f.unlink()


def extract_camera_status(camera):
    """Pull the fields worth showing on the page out of the camera's status
    block. Defensive throughout: this is an undocumented API, so any field
    here can be missing, renamed, or reshaped without warning."""
    status = getattr(camera, "status", None)
    if status is None:
        return None

    result = {}

    temp = getattr(status, "temperature", None)
    if temp is not None and getattr(temp, "unit", "F") == "F":
        result["temperature_f"] = getattr(temp, "value", None)

    power = {}
    for src in getattr(status, "powerSources", None) or []:
        location = (getattr(src, "location", "") or "").upper()
        entry = {"pct": getattr(src, "percentage", None), "level": getattr(src, "level", None)}
        if location == "TRAY1":
            power["battery"] = entry
        elif location == "INTERNAL":
            power["solar"] = entry
        elif location == "EXTERNAL":
            power["external"] = entry
    if power:
        result["power"] = power

    signal = getattr(status, "signal", None)
    processed = getattr(signal, "processed", None) if signal else None
    if processed is not None:
        result["signal"] = {
            "pct": getattr(processed, "percentage", None),
            "level": getattr(processed, "level", None),
        }

    status_updated = getattr(status, "lastUpdate", None)
    if status_updated:
        result["status_updated"] = status_updated

    return result or None


def build_timelapse():
    frames = sorted(FRAME_BUFFER_DIR.glob("*.jpg"), key=lambda f: float(f.stem))
    if len(frames) < 2:
        return False, len(frames)

    images = []
    for f in frames:
        img = Image.open(f).convert("RGB")
        img.thumbnail((GIF_MAX_DIMENSION, GIF_MAX_DIMENSION))
        images.append(img)

    images[0].save(
        TIMELAPSE_PATH,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=GIF_FRAME_DURATION_MS,
        loop=0,
        optimize=True,
    )
    return True, len(frames)


def main():
    if not USERNAME or not PASSWORD:
        print(
            "SPYPOINT_USERNAME / SPYPOINT_PASSWORD are not set. "
            "Add them as GitHub Actions repository secrets.",
            file=sys.stderr,
        )
        sys.exit(1)

    FRAME_BUFFER_DIR.mkdir(exist_ok=True)
    LATEST_PHOTO_PATH.parent.mkdir(exist_ok=True)

    client = spypoint.Client(USERNAME, PASSWORD)

    cameras = client.cameras()
    if not cameras:
        print("Spypoint account returned no cameras.", file=sys.stderr)
        sys.exit(1)

    for cam in cameras:
        name = getattr(getattr(cam, "config", None), "name", "(unnamed)")
        print(f"Found camera: id={cam.id} name={name} status={getattr(cam, 'status', '?')}")
    camera = cameras[0]

    photos = client.photos(cameras=[camera], limit=5)
    if not photos:
        print("No photos returned for this camera yet.", file=sys.stderr)
        sys.exit(1)

    # Sort newest first by capture date (API order isn't documented, so don't rely on it).
    photos = sorted(photos, key=lambda p: getattr(p, "date", ""), reverse=True)
    latest = photos[0]

    camera_status = extract_camera_status(camera)

    photo_tags = getattr(latest, "tag", None) or []
    photo_tag = photo_tags[0] if photo_tags else None

    last_seen_id = LAST_SEEN_PATH.read_text().strip() if LAST_SEEN_PATH.exists() else None
    is_new_photo = latest.id != last_seen_id

    now = datetime.now(timezone.utc)
    capture_time = parse_photo_date(latest, fallback=now)

    if is_new_photo:
        photo_url = latest.url("large")
        frame_path = FRAME_BUFFER_DIR / f"{capture_time.timestamp():.0f}.jpg"
        download(photo_url, frame_path)
        LAST_SEEN_PATH.write_text(latest.id)
        print(f"Downloaded new photo {latest.id} captured {getattr(latest, 'date', '?')}")
    else:
        print(f"No new photo since last run (still {latest.id}); refreshing timelapse only.")

    prune_old_frames(now)
    built, frame_count = build_timelapse()

    # Always republish whatever is newest in the buffer as the "latest photo" —
    # this makes the site self-healing if a prior run downloaded a frame but
    # failed before publishing it (e.g. a git error), rather than getting
    # stuck with no photo until the camera's next real capture.
    newest_frames = sorted(FRAME_BUFFER_DIR.glob("*.jpg"), key=lambda f: float(f.stem))
    if newest_frames:
        shutil.copyfile(newest_frames[-1], LATEST_PHOTO_PATH)

    METADATA_PATH.write_text(
        json.dumps(
            {
                "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "photo_date": getattr(latest, "date", None),
                "photo_tag": photo_tag,
                "source": "SPYPOINT Flex-S-Dark (unofficial API)",
                "timelapse_window_hours": TIMELAPSE_WINDOW_HOURS,
                "timelapse_frame_count": frame_count,
                "timelapse_available": built,
                "camera_status": camera_status,
            },
            indent=2,
        )
        + "\n"
    )

    print(f"Done. Frame buffer has {frame_count} frame(s); timelapse_available={built}")


if __name__ == "__main__":
    main()
