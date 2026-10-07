#!/usr/bin/env python3
"""
Fetches new photos since the last run from the Pineview crossing Spypoint trail camera and
rebuilds a rolling H.264 MP4 timelapse (trailing 10 hours, 12-second loop, 1280x720).

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

Raw frames are kept in the rolling buffer (cam_frame_buffer/raw/). Each time
the site media is rebuilt, every frame is rendered with the camera's own
firmware strip (date/time/temp-F/temp-C/moon phase/SPYPOINT branding) cropped
off and replaced by the Ogden City logo + stacked TEMP/DATE chip cards with a
TIME chip beside them (lower-left corner). The latest JPG and the MP4 both
come from the same renderer, so they always match. See apply_photo_overlay().

EMERGENCY HOLD: set {"hold": true} in camera_hold.json (editable right in
GitHub's web editor). The workflow runs immediately, deletes the latest photo
and timelapse from the site and stops publishing. Setting it back to false
resumes updates and permanently excludes everything captured during the hold
(optionally back-dated with "exclude_from"). See run_hold().
Uses the Open Sans font files bundled in fonts/ (Apache-2.0 licensed, see
fonts/LICENSE.txt) rather than relying on fonts being present on the CI
runner.

Files touched:
  - images/pineview-cam-latest.jpg   (committed — overwritten each run)
  - images/pineview-cam-timelapse.mp4 (committed — overwritten each run; replaces the old GIF)
  - camera_hold.json                  (committed — human-edited emergency hold switch)
  - pineview_cam_excluded.json        (committed — bot-owned record of held/excluded time ranges)
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
  - cam_frame_buffer/raw/             (NOT committed — persisted via actions/cache
                                        between runs so we don't bloat git history
                                        with every individual frame)
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from functools import lru_cache
from datetime import datetime, timedelta, timezone
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
TIMELAPSE_PATH = Path("images/pineview-cam-timelapse.mp4")
RAW_DIR = FRAME_BUFFER_DIR / "raw"        # untouched 720x406 camera frames, named <epoch>.jpg
RENDER_DIR = FRAME_BUFFER_DIR / "render"  # scratch: overlay-rendered frames fed to ffmpeg
HOLD_PATH = Path("camera_hold.json")      # human-edited emergency switch (see run_hold below)
EXCLUSIONS_PATH = Path("pineview_cam_excluded.json")  # bot-owned: held/excluded time ranges
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
# latest-photo sync and 10-hour timelapse keep running untouched, and every
# day already archived stays exactly as it is -- this only gates whether
# NEW days get added.
DAILY_ARCHIVE_ENABLED = True
DAILY_ARCHIVE_DIR = Path("images/daily-archive")
DAILY_ARCHIVE_MANIFEST_PATH = Path("pineview_cam_daily_archive.json")
DAILY_ARCHIVE_HOUR = 13  # 1:00 PM Mountain
MOUNTAIN_TZ = ZoneInfo("America/Denver")

POWER_LOG_PATH = Path("pineview_cam_power_log.jsonl")
POWER_LOG_MAX_LINES = 2000  # ~3 weeks of samples at a 15-min poll cadence

# Rolling timelapse (H.264 MP4): trailing 10 hours of 5-minute captures
# (~120 frames) played over a 12-second loop (~10 fps). Frames are rendered
# 1280 px wide, then padded top/bottom to a broadcast-friendly 1280x720.
TIMELAPSE_WINDOW_HOURS = 10
TIMELAPSE_LOOP_SECONDS = 12
TIMELAPSE_MIN_FPS = 10
VIDEO_W, VIDEO_H = 1280, 720
OUTPUT_FPS = 30   # container frame rate -- frames are repeated, which broadcast ingest likes
OUT_W = 1280      # width the overlay-rendered frames (and the latest JPG) are produced at

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
    for f in RAW_DIR.glob("*.jpg"):
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


@lru_cache(maxsize=None)
def _font(path, size):
    try:
        return ImageFont.truetype(str(path), size)
    except Exception as exc:
        print(f"Font load failed for {path} ({exc}); falling back to default font.", file=sys.stderr)
        return ImageFont.load_default()


@lru_cache(maxsize=None)
def _logo_for_scale(s):
    """Logo watermark pre-rendered for scale factor s (cached). Returns
    (logo RGBA, box_w, box_h, inner_pad) or None if the logo file is missing."""
    if not LOGO_PATH.exists():
        print(f"Logo not found at {LOGO_PATH}, skipping watermark.", file=sys.stderr)
        return None
    try:
        logo = Image.open(LOGO_PATH).convert("RGBA")
        lw, lh = logo.size
        target_h = round(28 * s)
        logo_small = logo.resize((max(1, int(lw * target_h / lh)), target_h), Image.LANCZOS)
        pad = round(8 * s)
        return (logo_small, logo_small.size[0] + pad * 2, logo_small.size[1] + pad * 2, pad)
    except Exception as exc:
        print(f"Logo watermark prep failed (non-fatal, skipping logo): {exc}", file=sys.stderr)
        return None


def apply_photo_overlay(src_path, dst_path, capture_time_utc, temp_f):
    """Renders one finished frame from a RAW camera photo: crops the camera's
    own firmware strip off the bottom, scales the photo up to OUT_W wide, and
    draws the Ogden City logo + stacked TEMP-over-DATE chip cards with a TIME
    chip beside them in the lower-left corner (approved stacked layout,
    2026-10-07). Everything is drawn at the final resolution (not upscaled
    afterward) so the text stays crisp in the 1280-wide MP4 and the latest
    JPG. Returns True on success; on any failure it falls back to writing the
    plain cropped/resized photo (or a straight copy) so the main sync never
    dies over cosmetics."""
    try:
        img = Image.open(src_path).convert("RGBA")
        W, H = img.size
        strip_px = (
            FIRMWARE_STRIP_PX if H == FIRMWARE_STRIP_REFERENCE_H
            else round(H * (FIRMWARE_STRIP_PX / FIRMWARE_STRIP_REFERENCE_H))
        )
        cropped = img.crop((0, 0, W, max(1, H - strip_px)))
        s = OUT_W / W
        CW, CH = OUT_W, max(1, round(cropped.size[1] * s))
        cropped = cropped.resize((CW, CH), Image.LANCZOS)
    except Exception as exc:
        print(f"Could not open/crop {src_path}: {exc}", file=sys.stderr)
        return False

    try:
        label_font = _font(FONT_SEMIBOLD, round(7 * s))
        label_font_big = _font(FONT_SEMIBOLD, round(14 * s))
        value_font = _font(FONT_CONDBOLD, round(14 * s))
        value_font_big = _font(FONT_CONDBOLD, round(28 * s))
        S = lambda v: round(v * s)

        local = capture_time_utc.astimezone(MOUNTAIN_TZ)
        date_str = local.strftime("%m/%d/%Y")
        time_str = local.strftime("%I:%M %p").lstrip("0")
        temp_str = f"{round(temp_f)}°F" if temp_f is not None else "--°F"

        margin, gap, stack_gap = S(10), S(8), S(6)
        base = CH - margin
        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        tw = lambda text, font: probe.textlength(text, font=font)

        small_h, big_h = S(31), S(62)
        date_w = tw(date_str, value_font) + S(20)
        time_w = tw(time_str, value_font) + S(20)
        temp_w = tw(temp_str, value_font_big) + S(40)
        col_w = max(date_w, temp_w)  # TEMP and DATE share one column width

        overlay = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        white = (255, 255, 255, 255)

        x = margin
        logo = _logo_for_scale(s)
        logo_xy = None
        if logo:
            logo_small, box_w, box_h, pad = logo
            od.rounded_rectangle([x, base - box_h, x + box_w, base], radius=S(6), fill=white)
            logo_xy = (x + pad, base - box_h + pad)
            x += box_w + gap

        col_x = x
        date_y = base - small_h
        temp_y = date_y - stack_gap - big_h
        od.rounded_rectangle([col_x, date_y, col_x + col_w, base], radius=S(6), fill=white)
        od.rounded_rectangle([col_x, temp_y, col_x + col_w, temp_y + big_h], radius=S(12), fill=white)
        time_x = col_x + col_w + gap
        od.rounded_rectangle([time_x, date_y, time_x + time_w, base], radius=S(6), fill=white)

        final = Image.alpha_composite(cropped, overlay)
        if logo:
            final.paste(logo[0], logo_xy, logo[0])

        fd = ImageDraw.Draw(final)

        def put(x0, w, y, label, value, lfont, vfont, label_off, value_off):
            fd.text((x0 + (w - tw(label, lfont)) / 2, y + S(label_off)), label,
                    font=lfont, fill=(*OVERLAY_RUST, 255))
            fd.text((x0 + (w - tw(value, vfont)) / 2, y + S(value_off)), value,
                    font=vfont, fill=(*OVERLAY_WATER_DARK, 255))

        put(col_x, col_w, temp_y, "TEMP", temp_str, label_font_big, value_font_big, 6, 26)
        put(col_x, col_w, date_y, "DATE", date_str, label_font, value_font, 3, 13)
        put(time_x, time_w, date_y, "TIME", time_str, label_font, value_font, 3, 13)

        final.convert("RGB").save(dst_path, quality=92)
        return True
    except Exception as exc:
        print(f"Photo overlay failed for {src_path} (non-fatal, plain photo used): {exc}", file=sys.stderr)
        try:
            cropped.convert("RGB").save(dst_path, quality=92)
            return True
        except Exception:
            return False


def load_temp_samples():
    """(epoch, temp_f) samples from the power log, oldest first. Used to give
    each frame the temperature closest to its own capture time instead of
    stamping every frame in a batch with the same reading."""
    samples = []
    if POWER_LOG_PATH.exists():
        for line in POWER_LOG_PATH.read_text().splitlines():
            try:
                e = json.loads(line)
                if e.get("temp_f") is None:
                    continue
                samples.append((datetime.fromisoformat(e["t"].replace("Z", "+00:00")).timestamp(), float(e["temp_f"])))
            except (ValueError, KeyError, TypeError):
                continue
    samples.sort()
    return samples


def temp_at(epoch, samples, fallback):
    """Linear interpolation between the two power-log samples around `epoch`
    (clamped to the nearest sample if within 2 h, else the fallback)."""
    if not samples:
        return fallback
    if epoch <= samples[0][0]:
        return samples[0][1] if samples[0][0] - epoch <= 7200 else fallback
    if epoch >= samples[-1][0]:
        return samples[-1][1] if epoch - samples[-1][0] <= 7200 else fallback
    for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
        if t0 <= epoch <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (epoch - t0) / (t1 - t0)
    return fallback


def raw_frames():
    return sorted(RAW_DIR.glob("*.jpg"), key=lambda f: float(f.stem))


def build_timelapse(temp_f=None):
    """Renders every buffered raw frame with the overlay, then encodes them
    into the 10-hour / 12-second H.264 MP4. Returns (built, frame_count)."""
    frames = raw_frames()
    if len(frames) < 2:
        return False, len(frames)
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found on PATH -- cannot build MP4 timelapse.", file=sys.stderr)
        return False, len(frames)

    samples = load_temp_samples()
    if RENDER_DIR.exists():
        shutil.rmtree(RENDER_DIR)
    RENDER_DIR.mkdir(parents=True)
    n = 0
    for f in frames:
        epoch = float(f.stem)
        capture_time = datetime.fromtimestamp(epoch, tz=timezone.utc)
        if apply_photo_overlay(f, RENDER_DIR / f"{n:05d}.jpg", capture_time, temp_at(epoch, samples, temp_f)):
            n += 1
    if n < 2:
        shutil.rmtree(RENDER_DIR, ignore_errors=True)
        return False, n

    # ~120 frames over 12 s => 10 fps; if the buffer ever holds more than 120
    # frames, speed up slightly so the loop stays 12 seconds.
    fps = max(TIMELAPSE_MIN_FPS, n / TIMELAPSE_LOOP_SECONDS)
    vf = (f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p")
    tmp_out = TIMELAPSE_PATH.with_name("pineview-cam-timelapse.tmp.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", f"{fps:.4f}",
           "-i", str(RENDER_DIR / "%05d.jpg"), "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", "23",
           "-profile:v", "high", "-level", "4.0", "-r", str(OUTPUT_FPS),
           "-movflags", "+faststart", "-an", str(tmp_out)]
    try:
        subprocess.run(cmd, check=True, timeout=300)
        tmp_out.replace(TIMELAPSE_PATH)
    except Exception as exc:
        print(f"ffmpeg encode failed: {exc}", file=sys.stderr)
        tmp_out.unlink(missing_ok=True)
        return False, n
    finally:
        shutil.rmtree(RENDER_DIR, ignore_errors=True)
    return True, n


def publish_latest(temp_f):
    """Render the newest buffered raw frame as the public latest photo."""
    frames = raw_frames()
    if not frames:
        return
    newest = frames[-1]
    epoch = float(newest.stem)
    capture_time = datetime.fromtimestamp(epoch, tz=timezone.utc)
    if not apply_photo_overlay(newest, LATEST_PHOTO_PATH, capture_time,
                               temp_at(epoch, load_temp_samples(), temp_f)):
        shutil.copyfile(newest, LATEST_PHOTO_PATH)


# --- Emergency hold + excluded time ranges -----------------------------------
# camera_hold.json is the human-edited switch ({"hold": true, ...}). Editing it
# (e.g. right in GitHub's web editor) triggers the workflow immediately.
#   * While hold is true: the latest photo and timelapse MP4 are deleted from
#     the site, nothing new is fetched or published, and the page shows
#     "temporarily offline".
#   * When hold goes back to false: everything captured while on hold (plus
#     any back-dated `exclude_from`) is permanently skipped, so incident
#     footage never reappears in the timelapse; live updates resume.
# State about the held window lives in pineview_cam_excluded.json (bot-owned).

def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def parse_when(value):
    """ISO timestamp -> aware UTC datetime. A value with no timezone is read
    as Mountain time (what a person typing a time would mean)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        print(f"Unparseable time in camera_hold.json: {value!r}", file=sys.stderr)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOUNTAIN_TZ)
    return dt.astimezone(timezone.utc)


def iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_exclusions():
    d = _read_json(EXCLUSIONS_PATH, {})
    return {"active_since": d.get("active_since"), "ranges": list(d.get("ranges") or [])}


def save_exclusions(state):
    EXCLUSIONS_PATH.write_text(json.dumps(state, indent=2) + "\n")


def is_excluded(dt, ranges):
    ts = dt.timestamp()
    for r in ranges:
        a, b = parse_when(r.get("from")), parse_when(r.get("to"))
        if a and b and a.timestamp() <= ts <= b.timestamp():
            return True
    return False


def purge_excluded_frames(ranges):
    removed = 0
    for f in raw_frames():
        if is_excluded(datetime.fromtimestamp(float(f.stem), tz=timezone.utc), ranges):
            f.unlink()
            removed += 1
    if removed:
        print(f"Removed {removed} buffered frame(s) that fall in excluded ranges.")


def run_hold(cfg, state, now):
    """Camera hold is ON: take the public photo + video down and stop."""
    candidates = [parse_when(state.get("active_since")), parse_when(cfg.get("exclude_from")), now]
    since = min(c for c in candidates if c is not None)
    state["active_since"] = iso_z(since)
    save_exclusions(state)

    for path in (LATEST_PHOTO_PATH, TIMELAPSE_PATH):
        path.unlink(missing_ok=True)
    shutil.rmtree(RENDER_DIR, ignore_errors=True)
    # Drop buffered frames from the held window so they can't be re-published.
    for f in raw_frames():
        if float(f.stem) >= since.timestamp():
            f.unlink()

    meta = _read_json(METADATA_PATH, {})
    for k in ("photo_date", "photo_tag", "camera_status", "timelapse_updated"):
        meta.pop(k, None)
    meta.update({
        "updated": iso_z(now),
        "hold": True,
        "hold_since": iso_z(since),
        "timelapse_available": False,
        "timelapse_frame_count": 0,
    })
    METADATA_PATH.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"CAMERA HOLD ACTIVE since {iso_z(since)} -- latest photo and timelapse removed, no new media published.")


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
    now = datetime.now(timezone.utc)
    FRAME_BUFFER_DIR.mkdir(exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_PHOTO_PATH.parent.mkdir(exist_ok=True)

    # One-time migration: older runs kept already-overlaid frames directly in
    # cam_frame_buffer/. The overlay is now rendered from raw frames on every
    # build (so layout changes apply to the whole loop), so discard the legacy
    # frames and the last-seen marker -- the next sync re-downloads the most
    # recent ~8 hours of raw frames from SPYPOINT and the buffer grows back
    # to the full 10-hour window.
    legacy = list(FRAME_BUFFER_DIR.glob("*.jpg"))
    if legacy:
        for f in legacy:
            f.unlink()
        LAST_SEEN_PATH.unlink(missing_ok=True)
        print(f"Migrated frame buffer: dropped {len(legacy)} legacy overlaid frame(s).")

    # Emergency hold / release handling happens BEFORE we even log in.
    hold_cfg = _read_json(HOLD_PATH, {})
    excl = load_exclusions()
    if hold_cfg.get("hold"):
        run_hold(hold_cfg, excl, now)
        return
    if excl.get("active_since"):
        since = parse_when(excl["active_since"]) or now
        excl["ranges"].append({"from": iso_z(since), "to": iso_z(now), "note": "camera hold"})
        excl["active_since"] = None
        save_exclusions(excl)
        print(f"Hold released: excluding {iso_z(since)} -> {iso_z(now)} from all published media.")
    ranges = excl["ranges"]
    purge_excluded_frames(ranges)

    if not USERNAME or not PASSWORD:
        print(
            "SPYPOINT_USERNAME / SPYPOINT_PASSWORD are not set. "
            "Add them as GitHub Actions repository secrets.",
            file=sys.stderr,
        )
        sys.exit(1)

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
    # rate. The 10-hour timelapse window itself is enforced separately in
    # prune_old_frames() by each frame's real timestamp, not by this fetch
    # limit -- frames persist across runs in the cached buffer.)
    photos = client.photos(cameras=[camera], limit=100)
    if not photos:
        print("No photos returned for this camera yet.", file=sys.stderr)
        sys.exit(1)

    # Oldest first -- with scheduled ("X times per day") cellular sync, several
    # queued photos can land on Spypoint's server in a single batch, so more
    # than one photo can be new since our last poll.
    photos_all_asc = sorted(photos, key=lambda p: getattr(p, "date", ""))
    latest_any = photos_all_asc[-1]
    # Anything captured inside a held/excluded time range is never published.
    photos_asc = [p for p in photos_all_asc if not is_excluded(parse_photo_date(p, fallback=now), ranges)]
    if not photos_asc:
        print("Every returned photo falls in an excluded range; nothing to publish.", file=sys.stderr)
        sys.exit(0)
    latest = photos_asc[-1]

    sync_daily_archive(photos_asc)

    camera_status = extract_camera_status(camera)
    append_power_log(now=datetime.now(timezone.utc), camera_status=camera_status)

    photo_tags = getattr(latest, "tag", None) or []
    photo_tag = photo_tags[0] if photo_tags else None

    last_seen_id = LAST_SEEN_PATH.read_text().strip() if LAST_SEEN_PATH.exists() else None

    if last_seen_id is None:
        new_photos = photos_all_asc
    else:
        seen_ids = [p.id for p in photos_all_asc]
        if last_seen_id in seen_ids:
            new_photos = photos_all_asc[seen_ids.index(last_seen_id) + 1:]
        else:
            # Last-seen photo aged out of the API's returned window (a long
            # gap between runs) -- best effort, take everything we were handed.
            new_photos = photos_all_asc
    new_photos = [p for p in new_photos if not is_excluded(parse_photo_date(p, fallback=now), ranges)]

    now = datetime.now(timezone.utc)
    temp_f = camera_status.get("temperature_f") if camera_status else None

    if new_photos:
        # Download every new photo, not just the newest one, so a batched
        # sync doesn't silently skip frames and leave the timelapse thinner
        # than TIMELAPSE_WINDOW_HOURS implies.
        for photo in new_photos:
            capture_time = parse_photo_date(photo, fallback=now)
            photo_url = photo.url("large")
            frame_path = RAW_DIR / f"{capture_time.timestamp():.0f}.jpg"
            download(photo_url, frame_path)  # raw; the overlay is rendered at build time
            print(f"Downloaded new photo {photo.id} captured {getattr(photo, 'date', '?')}")
        LAST_SEEN_PATH.write_text(latest_any.id)
    else:
        print(f"No new photo since last run (still {latest.id}); refreshing timelapse only.")

    prune_old_frames(now)
    built, frame_count = build_timelapse(temp_f)

    # Always republish whatever is newest in the buffer as the "latest photo" —
    # this makes the site self-healing if a prior run downloaded a frame but
    # failed before publishing it (e.g. a git error), rather than getting
    # stuck with no photo until the camera's next real capture.
    publish_latest(temp_f)

    METADATA_PATH.write_text(
        json.dumps(
            {
                "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "photo_date": getattr(latest, "date", None),
                "photo_tag": photo_tag,
                "source": "SPYPOINT Flex-S-Dark (unofficial API)",
                "hold": False,
                "timelapse_window_hours": TIMELAPSE_WINDOW_HOURS,
                "timelapse_loop_seconds": TIMELAPSE_LOOP_SECONDS,
                "timelapse_frame_count": frame_count,
                "timelapse_available": built,
                "timelapse_updated": now.strftime("%Y-%m-%dT%H:%M:%SZ") if built else None,
                "camera_status": camera_status,
            },
            indent=2,
        )
        + "\n"
    )

    print(f"Done. Frame buffer has {frame_count} frame(s); timelapse_available={built}")


if __name__ == "__main__":
    main()
