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

Every frame downloaded into the rolling buffer (see cam_frame_buffer/ below)
has the camera's own firmware strip (date/time/temp-F/temp-C/moon phase/
SPYPOINT branding) cropped off the bottom and replaced with a small Ogden
City logo watermark + Date/Time/Temp(F)-only chip cards, overlaid directly
on the photo's lower-left corner. This happens once, at download time, so
both the latest-photo sync and the timelapse GIF (built straight from these
same buffered frames) automatically match. See apply_photo_overlay() below.
Uses the Open Sans font files bundled in fonts/ (Apache-2.0 licensed, see
fonts/LICENSE.txt) rather than relying on fonts being present on the CI
runner.

Files touched:
  - images/pineview-cam-latest.jpg   (committed — overwritten each run)
  - images/pineview-cam-timelapse.gif (committed — overwritten each run)
  - pineview_cam.json                 (committed — small status file for the page)
  - images/hd/<photo-id>.jpg           (committed — Full-HD gallery, grows over time)
  - pineview_cam_hd.json               (committed — Full-HD gallery manifest for the page)
  - images/daily-archive/<YYYY-MM-DD>.jpg (committed — one photo per local day, the first
                                        capture at/after 1pm Mountain; permanent, never
                                        pruned -- long-term daily timelapse source material,
                                        independent of the camera's own SD card)
  - pineview_cam_daily_archive.json    (committed — daily archive manifest)
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
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

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

# One photo per local day, permanently archived to the repo -- a long-term
# daily-interval timelapse source for the life of the project, independent
# of the camera's own SD card (capacity and overwrite behavior on the
# physical card are unconfirmed -- this gives Jeff a copy that can't be lost
# to either). Added 2026-10-01.
#
# DAILY_ARCHIVE_ENABLED is the project's own off switch: when construction
# wraps and there's no more point archiving daily stills, flip this to False
# (a one-line edit, safely doable right in GitHub's web file editor -- no
# need for a dev session or the local push workflow) and future runs skip
# archiving entirely. Nothing else about the script is affected: the main
# latest-photo sync and 6-hour timelapse keep running untouched, and every
# day already archived stays exactly as it is -- this only gates whether
# NEW days get added.
DAILY_ARCHIVE_ENABLED = True
DAILY_ARCHIVE_DIR = Path("images/daily-archive")
DAILY_ARCHIVE_MANIFEST_PATH = Path("pineview_cam_daily_archive.json")
DAILY_ARCHIVE_HOUR = 13  # 1:00 PM Mountain
MOUNTAIN_TZ = ZoneInfo("America/Denver")

POWER_LOG_PATH = Path("pineview_cam_power_log.jsonl")
POWER_LOG_MAX_LINES = 2000  # ~3 weeks of samples at a 15-min poll cadence

TIMELAPSE_WINDOW_HOURS = 6
GIF_FRAME_DURATION_MS = 250
GIF_MAX_DIMENSION = 900  # downscale frames for a reasonably small GIF

# --- Photo overlay: crop the SPYPOINT firmware strip, add a logo watermark
# + Date/Time/Temp(F) chip cards instead. Approved design (2026-10-02).
# Trimmed, transparent, high-res render of the official vector logo (images/ogdencity-logo.svg),
# downscaled per frame so it stays crisp.
LOGO_PATH = Path("images/ogdencity-logo-overlay.png")
FONT_SEMIBOLD = Path("fonts/OpenSans-SemiBold.ttf")
FONT_CONDBOLD = Path("fonts/OpenSans-CondBold.ttf")
# Measured height, in px, of the camera's firmware strip on a 720x406 "large"
# photo. If the source resolution ever changes, this scales proportionally
# (see apply_photo_overlay) rather than silently cropping the wrong amount.
FIRMWARE_STRIP_PX = 18
FIRMWARE_STRIP_REFERENCE_H = 406
OVERLAY_RUST = (139, 74, 43)
OVERLAY_WATER_DARK = (26, 69, 96)


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


def _load_font(path, size):
    try:
        return ImageFont.truetype(str(path), size)
    except Exception as exc:
        print(f"Font load failed for {path} ({exc}); falling back to default font.", file=sys.stderr)
        return ImageFont.load_default()


_overlay_fonts = None  # (label_font, label_font_big, value_font, value_font_big)
_logo_chip = None      # (resized logo RGBA, box_w, box_h, inner_pad) or None if no logo found
_overlay_assets_loaded = False


def _ensure_overlay_assets():
    """Lazily load the overlay fonts and pre-render the logo watermark chip
    once per process (not once per frame) -- these never change run to run."""
    global _overlay_fonts, _logo_chip, _overlay_assets_loaded
    if _overlay_assets_loaded:
        return
    _overlay_assets_loaded = True

    _overlay_fonts = (
        _load_font(FONT_SEMIBOLD, 7),   # label_font
        _load_font(FONT_SEMIBOLD, 14),  # label_font_big (TEMP chip)
        _load_font(FONT_CONDBOLD, 14),  # value_font
        _load_font(FONT_CONDBOLD, 28),  # value_font_big (TEMP chip)
    )

    if LOGO_PATH.exists():
        try:
            logo = Image.open(LOGO_PATH).convert("RGBA")
            lw, lh = logo.size
            target_h = 28
            scale = target_h / lh
            logo_small = logo.resize((max(1, int(lw * scale)), target_h), Image.LANCZOS)
            pad = 8
            box_w = logo_small.size[0] + pad * 2
            box_h = logo_small.size[1] + pad * 2
            _logo_chip = (logo_small, box_w, box_h, pad)
        except Exception as exc:
            print(f"Logo watermark prep failed (non-fatal, skipping logo): {exc}", file=sys.stderr)
            _logo_chip = None
    else:
        print(f"Logo not found at {LOGO_PATH}, skipping watermark.", file=sys.stderr)
        _logo_chip = None


def apply_photo_overlay(image_path, capture_time_utc, temp_f):
    """Crops the camera's own firmware strip off the bottom of a photo and
    replaces it with a small Ogden City logo watermark + Date/Time/Temp(F)
    chip cards, overlaid directly on the photo's lower-left corner (approved
    design, 2026-10-02). Applied once per frame at download time, so both
    the latest-photo sync and the timelapse GIF (built from these same
    buffered frames) pick it up automatically. Best-effort: on any failure
    this leaves the original downloaded photo untouched rather than taking
    down the main sync."""
    try:
        _ensure_overlay_assets()
        label_font, label_font_big, value_font, value_font_big = _overlay_fonts

        img = Image.open(image_path).convert("RGBA")
        W, H = img.size
        strip_px = (
            FIRMWARE_STRIP_PX if H == FIRMWARE_STRIP_REFERENCE_H
            else round(H * (FIRMWARE_STRIP_PX / FIRMWARE_STRIP_REFERENCE_H))
        )
        cropped = img.crop((0, 0, W, max(1, H - strip_px)))
        CW, CH = cropped.size

        local = capture_time_utc.astimezone(MOUNTAIN_TZ)
        date_str = local.strftime("%m/%d/%Y")
        time_str = local.strftime("%I:%M %p").lstrip("0")
        temp_str = f"{round(temp_f)}°F" if temp_f is not None else "--°F"

        margin_left, margin_bottom, gap = 10, 10, 8
        baseline = CH - margin_bottom

        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))

        def chip_spec(label, value, lfont, vfont, pad_x, h, radius, label_off, value_off):
            w = max(probe.textlength(label, font=lfont),
                    probe.textlength(value, font=vfont)) + pad_x * 2
            return dict(label=label, value=value, label_font=lfont, value_font=vfont,
                        w=w, h=h, radius=radius, label_off=label_off, value_off=value_off)

        chips = [
            chip_spec("DATE", date_str, label_font, value_font, 10, 31, 6, 3, 13),
            chip_spec("TIME", time_str, label_font, value_font, 10, 31, 6, 3, 13),
            # TEMP chip is roughly double the size of the other two (approved design)
            chip_spec("TEMP", temp_str, label_font_big, value_font_big, 20, 62, 12, 6, 26),
        ]

        overlay = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        x = margin_left

        if _logo_chip:
            logo_small, box_w, box_h, pad = _logo_chip
            box_y = baseline - box_h
            odraw.rounded_rectangle([x, box_y, x + box_w, box_y + box_h], radius=6,
                                     fill=(255, 255, 255, 255))
            x += box_w + gap

        chip_positions = []
        for c in chips:
            chip_y = baseline - c["h"]
            odraw.rounded_rectangle([x, chip_y, x + c["w"], chip_y + c["h"]], radius=c["radius"],
                                     fill=(255, 255, 255, 255))
            chip_positions.append((x, chip_y, c))
            x += c["w"] + gap

        final = Image.alpha_composite(cropped, overlay)

        if _logo_chip:
            logo_small, box_w, box_h, pad = _logo_chip
            box_y = baseline - box_h
            final.paste(logo_small, (margin_left + pad, box_y + pad), logo_small)

        fdraw = ImageDraw.Draw(final)
        for x0, chip_y, c in chip_positions:
            lbl_w = fdraw.textlength(c["label"], font=c["label_font"])
            fdraw.text((x0 + (c["w"] - lbl_w) / 2, chip_y + c["label_off"]), c["label"],
                       font=c["label_font"], fill=(*OVERLAY_RUST, 255))
            val_w = fdraw.textlength(c["value"], font=c["value_font"])
            fdraw.text((x0 + (c["w"] - val_w) / 2, chip_y + c["value_off"]), c["value"],
                       font=c["value_font"], fill=(*OVERLAY_WATER_DARK, 255))

        final.convert("RGB").save(image_path, quality=90)
    except Exception as exc:
        print(f"Photo overlay failed for {image_path} (non-fatal, original left as-is): {exc}", file=sys.stderr)


def build_timelapse(temp_f=None):
    frames = sorted(FRAME_BUFFER_DIR.glob("*.jpg"), key=lambda f: float(f.stem))
    if len(frames) < 2:
        return False, len(frames)

    images = []
    for f in frames:
        img = Image.open(f)
        if img.size[1] == FIRMWARE_STRIP_REFERENCE_H:
            # Still a raw/unprocessed frame (e.g. left over in the cached
            # buffer from before the overlay feature shipped) -- backfill
            # the crop + overlay now rather than leaving it. Mixed frame
            # sizes in one GIF can garble the animation, so every frame
            # needs to go through this before being added. Best-effort
            # (temp_f may be a few minutes stale for an old frame, and the
            # frame's own filename timestamp is still used for date/time).
            try:
                capture_time = datetime.fromtimestamp(float(f.stem), tz=timezone.utc)
            except ValueError:
                capture_time = datetime.now(timezone.utc)
            apply_photo_overlay(f, capture_time, temp_f)
            img = Image.open(f)
        img = img.convert("RGB")
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


def sync_daily_archive(photos_asc):
    """Archive one photo per local calendar day -- the first capture at or
    after DAILY_ARCHIVE_HOUR (1:00 PM Mountain) -- into a permanent folder
    that gets committed to git. Unlike the rolling timelapse frame buffer,
    nothing here is ever pruned. A day already in the manifest is skipped,
    so this is safe to call every run. Best-effort: never let an archive
    problem take down the main sync.

    Coverage note: this only sees whatever photos_asc the main sync fetched
    this run (bounded by the API call's `limit`), so a gap longer than that
    window (a multi-day outage) could skip a day's archive photo entirely.
    Acceptable for a long-term daily timelapse -- an occasional missing day
    is a minor gap, not a reason to complicate this.
    """
    if not DAILY_ARCHIVE_ENABLED:
        return
    try:
        manifest = []
        if DAILY_ARCHIVE_MANIFEST_PATH.exists():
            try:
                manifest = json.loads(DAILY_ARCHIVE_MANIFEST_PATH.read_text()).get("days", [])
            except (json.JSONDecodeError, OSError):
                manifest = []
        archived_dates = {entry.get("date") for entry in manifest}

        new_count = 0
        for photo in photos_asc:
            raw = getattr(photo, "date", None)
            if not raw:
                continue
            try:
                capture_utc = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue

            local = capture_utc.astimezone(MOUNTAIN_TZ)
            date_str = local.strftime("%Y-%m-%d")
            if date_str in archived_dates or local.hour < DAILY_ARCHIVE_HOUR:
                continue  # already archived, or too early in the day -- a later run will catch it

            DAILY_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            dest = DAILY_ARCHIVE_DIR / f"{date_str}.jpg"
            try:
                download(photo.url("large"), dest)
            except Exception as exc:
                print(f"Daily archive download failed for {date_str}: {exc}", file=sys.stderr)
                continue

            manifest.append({
                "date": date_str,
                "captured_local": local.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "captured_utc": raw,
                "file": str(dest).replace("\\", "/"),
            })
            archived_dates.add(date_str)
            new_count += 1
            print(f"Daily archive: saved {date_str} ({local.strftime('%H:%M')} local)")

        if new_count:
            manifest.sort(key=lambda e: e.get("date") or "")
            DAILY_ARCHIVE_MANIFEST_PATH.write_text(json.dumps({"days": manifest}, indent=2) + "\n")
            print(f"Daily archive: {new_count} new day(s), {len(manifest)} total.")
    except Exception as exc:
        print(f"Daily archive sync failed (non-fatal): {exc}", file=sys.stderr)


def _debug_jsonable(obj, _depth=0):
    """Recursively convert an _AttrDict-style object (attributes set
    dynamically from a dict, as pyspypoint's Camera/Photo objects are) into
    plain dicts/lists/primitives so it can be dumped with json.dumps. Purely
    a one-off diagnostic helper -- not used by the rest of the script."""
    if _depth > 6:
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_debug_jsonable(v, _depth + 1) for v in obj]
    if isinstance(obj, dict):
        return {k: _debug_jsonable(v, _depth + 1) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return {k: _debug_jsonable(v, _depth + 1) for k, v in vars(obj).items()}
    return str(obj)


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

    # --- TEMPORARY DEBUG: added 2026-10-01, remove once we've confirmed
    # whether Spypoint's API exposes SD card / storage usage anywhere on the
    # camera object. pyspypoint wraps the raw API response dynamically (see
    # client.py's _AttrDict), so any field Spypoint actually returns -- not
    # just the ones extract_camera_status() currently reads -- will show up
    # here. Check this run's GitHub Actions log for the
    # "=== RAW CAMERA DEBUG DUMP ===" marker, look for anything storage/SD/
    # memory-related, then delete this block (and _debug_jsonable above it)
    # once we know the real field name (or that it isn't exposed at all).
    print("=== RAW CAMERA DEBUG DUMP ===", file=sys.stderr)
    print(json.dumps(_debug_jsonable(camera), indent=2, default=str), file=sys.stderr)
    print("=== END RAW CAMERA DEBUG DUMP ===", file=sys.stderr)
    # --- END TEMPORARY DEBUG ---

    sync_hd_gallery(client, camera)

    # limit=100 gives enough headroom to catch every photo from a batched
    # cellular sync at a 5-min capture cadence (12 photos/hour) even after a
    # multi-hour gap between runs -- the GitHub Actions scheduler has silently
    # skipped runs for 2+ hours before (and once for ~4.5 hours, see the
    # workflow file), and at 5-min captures that alone is 24-54+ new photos to
    # catch up on. 100 covers a full ~8-hour outage with a little margin to
    # spare. (Raised from 48 on 2026-10-01 when the capture interval was
    # tightened from 10 min to 5 min -- 48 only covered ~4 hours at the new
    # rate. The 6-hour timelapse window itself is enforced separately in
    # prune_old_frames() by each frame's real timestamp, not by this fetch
    # limit -- frames persist across runs in the cached buffer.)
    photos = client.photos(cameras=[camera], limit=100)
    if not photos:
        print("No photos returned for this camera yet.", file=sys.stderr)
        sys.exit(1)

    # Oldest first -- with scheduled ("X times per day") cellular sync, several
    # queued photos can land on Spypoint's server in a single batch, so more
    # than one photo can be new since our last poll.
    photos_asc = sorted(photos, key=lambda p: getattr(p, "date", ""))
    latest = photos_asc[-1]

    sync_daily_archive(photos_asc)

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
    temp_f = camera_status.get("temperature_f") if camera_status else None

    if new_photos:
        # Download every new photo, not just the newest one, so a batched
        # sync doesn't silently skip frames and leave the timelapse thinner
        # than TIMELAPSE_WINDOW_HOURS implies.
        for photo in new_photos:
            capture_time = parse_photo_date(photo, fallback=now)
            photo_url = photo.url("large")
            frame_path = FRAME_BUFFER_DIR / f"{capture_time.timestamp():.0f}.jpg"
            download(photo_url, frame_path)
            apply_photo_overlay(frame_path, capture_time, temp_f)
            print(f"Downloaded new photo {photo.id} captured {getattr(photo, 'date', '?')}")
        LAST_SEEN_PATH.write_text(latest.id)
    else:
        print(f"No new photo since last run (still {latest.id}); refreshing timelapse only.")

    prune_old_frames(now)
    built, frame_count = build_timelapse(temp_f)

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
