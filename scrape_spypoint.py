#!/usr/bin/env python3
"""
Fetches new photos since the last run from the Pineview crossing Spypoint trail camera and
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

TIMELAPSE_WINDOW_HOURS = 12
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

    # limit=12 gives enough headroom to catch every photo from a batched
    # cellular sync (e.g. syncing 12x/day with hourly captures queues ~2
    # photos per sync) plus margin for an occasional missed run.
    photos = client.photos(cameras=[camera], limit=12)
    if not photos:
        print("No photos returned for this camera yet.", file=sys.stderr)
        sys.exit(1)

    # Oldest first -- with scheduled ("X times per day") cellular sync, several
    # queued photos can land on Spypoint's server in a single batch, so more
    # than one photo can be new since our last poll.
    photos_asc = sorted(photos, key=lambda p: getattr(p, "date", ""))
    latest = photos_asc[-1]

    camera_status = extract_camera_status(camera)

    photo_tags = getattr(latest, "tag", None) or []
    photo_tag = photo_tags[0] if photo_tags else None

    last_seen_id = LAST_SEEN_PATH.read_text().strip() if LAST_SEEN_PATH.exists() else None

    if last_seen_id is None:
        new_photos = photos_asc
    else:
        seen_ids = [p.id for p in photos_asc]
        if last_seen_id in seen_ids:
            new_photos = photos_asc[seen_ids.index(last_seen_id) + 1:]
        else:
            # Last-seen photo aged out of the API's returned window (a long
            # gap between runs) -- best effort, take everything we were handed.
            new_photos = photos_asc

    now = datetime.now(timezone.utc)

    if new_photos:
        # Download every new photo, not just the newest one, so a batched
        # sync doesn't silently skip frames and leave the timelapse thinner
        # than TIMELAPSE_WINDOW_HOURS implies.
        for photo in new_photos:
            capture_time = parse_photo_date(photo, fallback=now)
            photo_url = photo.url("large")
            frame_path = FRAME_BUFFER_DIR / f"{capture_time.timestamp():.0f}.jpg"
            download(photo_url, frame_path)
            print(f"Downloaded new photo {photo.id} captured {getattr(photo, 'date', '?')}")
        LAST_SEEN_PATH.write_text(latest.id)
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
