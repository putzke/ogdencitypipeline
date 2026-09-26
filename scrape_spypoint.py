#!/usr/bin/env python3
"""
Fetches new photos since the last run from the Pineview crossing Spypoint trail camera and
rebuilds a rolling timelapse GIF covering the trailing TIMELAPSE_WINDOW_HOURS.

Also syncs a small "Full-HD Site Photos" gallery: photos manually requested as
Full-HD from the Spypoint app/gallery arrive asynchronously on a later camera
transmission (see Spypoint's "How to request a Full-HD photos and videos"
support article). This script polls the same hd=True filter the app's own
gallery uses and keeps whatever has arrived so far in a small local gallery.

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
  - images/hd/<photo-id>.jpg           (committed — Full-HD gallery, grows over time)
  - pineview_cam_hd.json               (committed — Full-HD gallery manifest for the page)
  - pineview_cam_power_log.jsonl       (committed — one line per run: battery/solar/12V/signal/
                                        temp snapshot, for evaluating whether capture/sync
                                        frequency can safely be increased)
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

HD_DIR = Path("images/hd")
HD_MANIFEST_PATH = Path("pineview_cam_hd.json")
HD_PHOTO_LIMIT = 50  # matches the purchased Full-HD photo request pack

POWER_LOG_PATH = Path("pineview_cam_power_log.jsonl")
POWER_LOG_MAX_LINES = 1000  # ~3 weeks of samples at a 30-min poll cadence

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


def best_hd_url(photo):
    """Prefer the highest-resolution URL available on this photo object.
    Undocumented API -- try likely size keys in descending preference,
    falling back to 'large' (the size the regular latest-photo sync already
    relies on) if nothing more specific is present on this photo."""
    for size in ("original", "hd", "highres", "xlarge", "full"):
        section = getattr(photo, size, None)
        if section is not None and getattr(section, "host", None) and getattr(section, "path", None):
            return photo.url(size)
    return photo.url("large")


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


def append_power_log(now, camera_status):
    """Append one snapshot of the camera's reported power/signal/temp state.
    This is what lets us actually judge -- from real data instead of a couple
    of spot-checked app screenshots -- whether the 12V/solar supply has
    headroom to support a shorter capture/sync interval. Best-effort: never
    let a logging problem take down the main sync."""
    if not camera_status:
        return
    try:
        power = camera_status.get("power") or {}
        battery = power.get("battery") or {}
        solar = power.get("solar") or {}
        external = power.get("external") or {}
        signal = camera_status.get("signal") or {}

        entry = {
            "t": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status_updated": camera_status.get("status_updated"),
            "temp_f": camera_status.get("temperature_f"),
            "battery_pct": battery.get("pct"),
            "battery_level": battery.get("level"),
            "solar_pct": solar.get("pct"),
            "solar_level": solar.get("level"),
            "external_pct": external.get("pct"),
            "external_level": external.get("level"),
            "signal_pct": signal.get("pct"),
        }

        lines = []
        if POWER_LOG_PATH.exists():
            lines = POWER_LOG_PATH.read_text().splitlines()
        lines.append(json.dumps(entry))
        if len(lines) > POWER_LOG_MAX_LINES:
            lines = lines[-POWER_LOG_MAX_LINES:]
        POWER_LOG_PATH.write_text("\n".join(lines) + "\n")
    except Exception as exc:
        print(f"Power log append failed (non-fatal): {exc}", file=sys.stderr)


def sync_hd_gallery(client, camera):
    """Pull whatever Full-HD-requested photos are available (the API's own
    hd=True filter -- the same one the Spypoint app's gallery uses) and keep
    a small local gallery of them. Existing entries are never re-downloaded;
    failures here are logged but never abort the main latest-photo sync."""
    try:
        hd_photos = client.photos(cameras=[camera], hd=True, limit=HD_PHOTO_LIMIT)
    except Exception as exc:  # undocumented API -- don't let this break the main sync
        print(f"HD photo fetch failed, skipping this run: {exc}", file=sys.stderr)
        return

    if not hd_photos:
        return

    HD_DIR.mkdir(parents=True, exist_ok=True)

    manifest = []
    if HD_MANIFEST_PATH.exists():
        try:
            manifest = json.loads(HD_MANIFEST_PATH.read_text()).get("photos", [])
        except (json.JSONDecodeError, OSError):
            manifest = []
    known_ids = {entry.get("id") for entry in manifest}

    new_count = 0
    for photo in hd_photos:
        pid = getattr(photo, "id", None)
        if not pid or pid in known_ids:
            continue
        tag_list = getattr(photo, "tag", None) or []
        dest = HD_DIR / f"{pid}.jpg"
        try:
            download(best_hd_url(photo), dest)
        except Exception as exc:
            print(f"Failed to download HD photo {pid}: {exc}", file=sys.stderr)
            continue
        manifest.append({
            "id": pid,
            "date": getattr(photo, "date", None),
            "tag": tag_list[0] if tag_list else None,
            "file": str(dest).replace("\\", "/"),
        })
        known_ids.add(pid)
        new_count += 1
        print(f"Downloaded new Full-HD photo {pid} captured {getattr(photo, 'date', '?')}")

    # Keep only the newest HD_PHOTO_LIMIT, in case the pack is ever topped up
    # past what we want to keep publishing.
    manifest.sort(key=lambda e: e.get("date") or "", reverse=True)
    if len(manifest) > HD_PHOTO_LIMIT:
        for stale in manifest[HD_PHOTO_LIMIT:]:
            stale_path = Path(stale["file"])
            if stale_path.exists():
                stale_path.unlink()
        manifest = manifest[:HD_PHOTO_LIMIT]

    HD_MANIFEST_PATH.write_text(json.dumps({"photos": manifest}, indent=2) + "\n")

    if new_count:
        print(f"HD gallery: {new_count} new photo(s), {len(manifest)} total.")


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

    sync_hd_gallery(client, camera)

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
    append_power_log(now=datetime.now(timezone.utc), camera_status=camera_status)

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
