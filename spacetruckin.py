#!/usr/bin/env python3
"""SPACE TRUCKIN (named SKY until 1.1) -- loops the last 24 hours of
GOES-19 GEOCOLOR satellite imagery full-screen on a CRT. Raspberry Pi
3B + composite video, same appliance setup as the sibling apps under
/opt (bars, loudness, channel38...): the framebuffer-write, console-mode, splash, ABOUT and power-dialog code
below is copied from loudness.py rather than shared, per this fleet's
no-shared-library convention -- each app is its own self-contained
directory.

Two processes. The player (this file, run normally) only ever copies
ready-made frames to /dev/fb0. The fetcher (this same file run with
--fetch, started by the player as a low-priority child) polls NOAA's
image directory every few minutes, downloads any new 600x600 frames,
crops each to 4:3, scales it to the framebuffer's size and stores it
already converted to the framebuffer's raw pixel format. Decoding +
cropping + scaling a frame measured ~55ms on a Pi 3B -- doing that on
the fly would eat the whole frame budget, and doing it on a thread in
the player would stall playback under the GIL, hence the separate
process.

The player stamps each frame's capture time (Central) in the lower
right, over the Gulf: WHITE while the fetcher is still building the
loop, YELLOW once it holds the complete 24 hours -- see COMPLETE_MARKER.
"""
import argparse
import configparser
import datetime
import fcntl
import io
import mmap
import os
import re
import selectors
import signal
import struct
import subprocess
import sys
import time
import urllib.request
import zoneinfo
from pathlib import Path

import evdev
import numpy as np
from evdev import ecodes

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402  (must come after SDL env vars are set)

VERSION = "1.1"

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.ini"
CACHE_DIR = BASE_DIR / "cache"
FONT_PATH = BASE_DIR / "VCR_OSD_MONO_1.001.ttf"
SPLASH_PATH = BASE_DIR / "splash.png"  # see build_splash_canvases()
SPLASH_SECONDS = 5.0
LOGO_PATH = BASE_DIR / "metalshop-logo.png"  # ABOUT screen -- see draw_about_screen()
SPLASH_Y_OFFSET = 40  # pixels to shift the splash image up from dead-center
SPLASH_VERSION_FONT_SIZE = 22
SPLASH_VERSION_GAP = 20
SPLASH_WAITING_FONT_SIZE = 26
SPLASH_WAITING_GAP = 14

FRAME_W, FRAME_H = 720, 480
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
ORANGE = (0xFF, 0xA5, 0x00)
YELLOW = (0xFF, 0xFF, 0x00)

# Capture-time stamp, lower right over the Gulf of Mexico. Margins keep
# it inside the CRT's visible area (same 48px as ABOUT_MARGIN_Y).
STAMP_FONT_SIZE = 28
STAMP_MARGIN_X = 44
STAMP_MARGIN_Y = 48
STAMP_SHADOW = 2  # px black drop shadow, so it stays readable over white cloud tops
STAMP_FORMAT = "%I:%M %p %m-%d-%y"  # 08:56 AM 10-15-26
STAMP_TZ = zoneinfo.ZoneInfo("America/Chicago")

# NOAA STAR's CDN keeps ~10 days of every sector at several sizes, one
# frame every 5 minutes, named <YYYYDDDHHMM UTC>_GOES19-ABI-sp-GEOCOLOR-
# 600x600.jpg ("sp" = the Southern Plains sector). The bottom ~60px of
# each frame are NOAA's caption bar and logo -- a 4:3 crop out of the
# middle (crop = center) leaves both out.
DEFAULT_SOURCE_URL = "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/sp/GEOCOLOR/"
DEFAULT_IMAGE_SIZE = "600x600"
CROPS = ("top", "center", "bottom")
FPS_STEPS = (5, 10, 15, 20, 30)  # Left/Right steps through these
FETCH_INTERVAL = 300  # seconds between directory polls -- NOAA adds a frame every 5 minutes
FETCH_RETRY = 60  # seconds before retrying after a failed poll
HTTP_TIMEOUT = 30
USER_AGENT = f"SPACETRUCKIN/{VERSION} (Raspberry Pi CRT satellite loop)"
RESCAN_SECONDS = 30  # how often the player looks for new cached frames while waiting

POWER_OPTIONS = ["NO", "YES", "RESTART"]
MOUSE_MOVE_THRESHOLD = 12  # cumulative REL_X/REL_Y units before it counts as one direction press

# See menu.py -- Home exits with this so the launcher knows to hand off
# to Health Monitor instead of redrawing its own app list.
EXIT_GOTO_HOME = 42

KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01
FBIO_WAITFORVSYNC = 0x40044620  # _IOW('F', 0x20, __u32)


def load_settings():
    parser = configparser.ConfigParser()
    parser["spacetruckin"] = {
        "source_url": DEFAULT_SOURCE_URL,
        "image_size": DEFAULT_IMAGE_SIZE,
        "hours": "24",
        "fps": "15",
        "hold_seconds": "1.0",
        "crop": "center",
    }
    parser.read(SETTINGS_PATH)
    section = parser["spacetruckin"]
    crop = section.get("crop")
    if crop not in CROPS:
        print(f"Unknown crop '{crop}' in settings.ini -- falling back to 'center'.", file=sys.stderr)
        crop = "center"
    source_url = section.get("source_url")
    if not source_url.endswith("/"):
        source_url += "/"
    return {
        "source_url": source_url,
        "image_size": section.get("image_size"),
        "hours": section.getfloat("hours"),
        "fps": max(1, section.getint("fps")),
        "hold_seconds": section.getfloat("hold_seconds"),
        "crop": crop,
    }


def save_setting(section, key, value):
    # Surgical text edit rather than configparser's .write(), which would
    # drop every comment in settings.ini -- copied from loudness.py.
    text = SETTINGS_PATH.read_text() if SETTINGS_PATH.exists() else ""
    lines = text.splitlines(keepends=True)
    section_header = f"[{section}]"
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    in_section = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == section_header:
            in_section = True
            continue
        if in_section and stripped.startswith("[") and stripped != section_header:
            lines.insert(i, f"{key} = {value}\n")
            SETTINGS_PATH.write_text("".join(lines))
            return
        if in_section and key_re.match(line):
            lines[i] = f"{key} = {value}\n"
            SETTINGS_PATH.write_text("".join(lines))
            return
    if in_section:
        lines.append(f"{key} = {value}\n")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"\n{section_header}\n{key} = {value}\n")
    SETTINGS_PATH.write_text("".join(lines))


def fb_geometry(dev="/dev/fb0"):
    sys_dir = Path("/sys/class/graphics") / Path(dev).name
    width, height = (int(x) for x in (sys_dir / "virtual_size").read_text().split(","))
    bpp = int((sys_dir / "bits_per_pixel").read_text())
    return width, height, bpp


def surface_to_raw(surface, bpp, rgb565=None):
    """Packs a surface into the framebuffer's raw pixel format -- RGB565
    via an SDL blit into a 16-bit surface (see FrameBuffer.write_surface),
    or BGRX for a 32bpp framebuffer."""
    width, height = surface.get_size()
    if bpp == 16:
        if rgb565 is None:
            rgb565 = pygame.Surface((width, height), 0, 16)
        rgb565.blit(surface, (0, 0))
        return rgb565.get_buffer().raw
    arr = pygame.surfarray.pixels3d(surface).transpose(1, 0, 2)  # (H, W, RGB) uint8
    alpha = np.zeros((height, width, 1), dtype=np.uint8)
    return np.concatenate([arr[:, :, ::-1], alpha], axis=2).astype(np.uint8).tobytes()


class FrameBuffer:
    """Direct writer for /dev/fb0, bypassing DRM page-flips entirely --
    copied from loudness.py (see bars.py's module docstring for why
    flip()-based rendering doesn't work on this hardware/driver combo).
    Adds write_raw() for frames the fetcher already packed into the
    framebuffer's own pixel format."""

    def __init__(self, dev="/dev/fb0"):
        sys_dir = Path("/sys/class/graphics") / Path(dev).name
        self.width, self.height, self.bpp = fb_geometry(dev)
        self.stride = int((sys_dir / "stride").read_text())
        self.bypp = self.bpp // 8
        self.row_bytes = self.width * self.bypp
        size = self.stride * self.height
        self.fd = os.open(dev, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        if self.bpp not in (16, 32):
            raise RuntimeError(f"Unsupported framebuffer depth: {self.bpp}bpp")
        self.rgb565 = None  # built on first write -- see surface_to_raw()
        # Wait for vblank before each write so a frame never tears
        # mid-scanout -- see loudness.py (2026-10-14). Falls back to
        # unsynced writes if the driver lacks the ioctl.
        self.vsync = True

    def write_surface(self, surface):
        if surface.get_size() != (self.width, self.height):
            surface = pygame.transform.scale(surface, (self.width, self.height))
        if self.bpp == 16 and self.rgb565 is None:
            self.rgb565 = pygame.Surface((self.width, self.height), 0, 16)
        self.write_raw(surface_to_raw(surface, self.bpp, self.rgb565))

    def write_raw(self, raw):
        if self.vsync:
            try:
                fcntl.ioctl(self.fd, FBIO_WAITFORVSYNC, struct.pack("I", 0))
            except OSError as exc:
                print(f"FBIO_WAITFORVSYNC unavailable ({exc}) -- writing unsynced.", file=sys.stderr)
                self.vsync = False
        if self.stride == self.row_bytes:
            self.mm.seek(0)
            self.mm.write(raw)
        else:
            for y in range(self.height):
                self.mm.seek(y * self.stride)
                self.mm.write(raw[y * self.row_bytes : (y + 1) * self.row_bytes])

    def raw_to_surface(self, raw):
        """A 16bpp raw frame back into a surface, so the power dialog can
        be drawn over whatever frame is showing. None on 32bpp, where the
        dialog just goes over black."""
        if self.bpp != 16:
            return None
        surf = pygame.Surface((self.width, self.height), 0, 16)
        surf.get_buffer().write(raw, 0)
        return surf

    def close(self):
        self.mm.close()
        os.close(self.fd)


def build_splash_canvases():
    """Splash at the source image's own native resolution, centered on
    black, version number underneath -- copied from loudness.py. Returns
    (plain, waiting): `waiting` adds WAITING FOR SATELLITE under the
    version, and stays up until the fetcher has a first frame to show.
    (None, None) if the splash image won't load."""
    try:
        img = pygame.image.load(str(SPLASH_PATH)).convert()
    except (pygame.error, OSError) as exc:
        print(f"Splash load failed: {exc}", file=sys.stderr)
        return None, None
    canvas = pygame.Surface((FRAME_W, FRAME_H))
    canvas.fill(BLACK)
    img_w, img_h = img.get_size()
    img_x = (FRAME_W - img_w) // 2
    img_y = (FRAME_H - img_h) // 2 - SPLASH_Y_OFFSET
    canvas.blit(img, (img_x, img_y))
    version_font = pygame.font.Font(str(FONT_PATH), SPLASH_VERSION_FONT_SIZE)
    version_surf = version_font.render(f"VERSION {VERSION}", True, ORANGE)
    version_y = img_y + img_h + SPLASH_VERSION_GAP
    canvas.blit(version_surf, ((FRAME_W - version_surf.get_width()) // 2, version_y))
    waiting = canvas.copy()
    waiting_font = pygame.font.Font(str(FONT_PATH), SPLASH_WAITING_FONT_SIZE)
    waiting_surf = waiting_font.render("WAITING FOR SATELLITE", True, WHITE)
    waiting.blit(waiting_surf, ((FRAME_W - waiting_surf.get_width()) // 2,
                                version_y + version_surf.get_height() + SPLASH_WAITING_GAP))
    return canvas, waiting


# ABOUT screen -- same layout as every other app in the fleet (copied from
# loudness.py). BACK toggles it. Title top center, settings in the middle,
# METAL SHOP logo bottom center, stretched by 720/640 since it's
# square-pixel art shown on the CRT's narrower 720x480 pixels.
ABOUT_MARGIN_Y = 48  # keeps title/logo inside the CRT's visible area
ABOUT_LINE_GAP = 8
ABOUT_COLUMN_GAP = 16
LOGO_X_STRETCH = 720 / 640


def load_about_logo():
    try:
        img = pygame.image.load(str(LOGO_PATH)).convert_alpha()
    except (pygame.error, OSError) as exc:
        print(f"Logo load failed: {exc}", file=sys.stderr)
        return None
    w, h = img.get_size()
    return pygame.transform.smoothscale(img, (round(w * LOGO_X_STRETCH), h))


def draw_about_screen(canvas, title_font, body_font, logo, title, rows):
    """`rows` is a list of (label, value) -- labels right-aligned in an
    orange column, values left-aligned in white beside them, the block
    centered in the space between the title and the logo."""
    frame_w, frame_h = canvas.get_size()
    canvas.fill(BLACK)
    title_surf = title_font.render(title, True, ORANGE)
    canvas.blit(title_surf, ((frame_w - title_surf.get_width()) // 2, ABOUT_MARGIN_Y))
    top = ABOUT_MARGIN_Y + title_surf.get_height()
    bottom = frame_h - ABOUT_MARGIN_Y
    if logo is not None:
        bottom -= logo.get_height()
        canvas.blit(logo, ((frame_w - logo.get_width()) // 2, bottom))
    if not rows:
        return
    label_surfs = [body_font.render(label, True, ORANGE) if label else None for label, _ in rows]
    value_surfs = [body_font.render(str(value), True, WHITE) for _, value in rows]
    label_w = max((l.get_width() for l in label_surfs if l), default=0)
    value_w = max(v.get_width() for v in value_surfs)
    line_h = body_font.get_linesize() + ABOUT_LINE_GAP
    x0 = (frame_w - (label_w + ABOUT_COLUMN_GAP + value_w)) // 2
    y = top + (bottom - top - line_h * len(rows)) // 2
    for label_surf, value_surf in zip(label_surfs, value_surfs):
        if label_surf:
            canvas.blit(label_surf, (x0 + label_w - label_surf.get_width(), y))
        canvas.blit(value_surf, (x0 + label_w + ABOUT_COLUMN_GAP, y))
        y += line_h


def find_keyboard_devices():
    devices = []
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if dev.capabilities().get(ecodes.EV_KEY):
            devices.append(dev)
    if not devices:
        # Puppets run unattended with no remote attached -- just play.
        print("No keyboard input device found -- running headless/unattended.", file=sys.stderr)
    return devices


# --- Frame cache -------------------------------------------------------
#
# One file per frame, <YYYYDDDHHMM>.raw, under cache/<W>x<H>x<bpp>/ (so a
# framebuffer of a different size or depth never plays mismatched data).
# The fetcher writes each to a .tmp first and renames it into place, so
# the player never sees a half-written frame.
#
# COMPLETE_MARKER in the same directory means the fetcher's last poll found
# every frame in NOAA's 24-hour window already cached. The player deletes
# it before starting the fetcher (so a stale cache from before a poweroff
# never counts as complete), the fetcher writes it after a poll that leaves
# nothing missing, and deletes it again whenever a poll finds a real
# backlog. The routine one new frame every 5 minutes isn't a backlog, so
# the stamp doesn't blink white on every arrival.
COMPLETE_MARKER = "complete"
BACKLOG_FRAMES = 3  # more missing than this in one poll = still building

FRAME_NAME_RE = re.compile(r'href="((\d{11})_[^"]*-(\d+x\d+)\.jpg)"')


def stamp_to_datetime(stamp):
    """NOAA's YYYYDDDHHMM (day-of-year, UTC) to an aware datetime."""
    year, day, hour, minute = int(stamp[:4]), int(stamp[4:7]), int(stamp[7:9]), int(stamp[9:11])
    return (datetime.datetime(year, 1, 1, hour, minute, tzinfo=datetime.timezone.utc)
            + datetime.timedelta(days=day - 1))


def cache_dir_for(width, height, bpp):
    return CACHE_DIR / f"{width}x{height}x{bpp}"


def cached_stamps(cache_dir):
    if not cache_dir.is_dir():
        return []
    return sorted(p.stem for p in cache_dir.glob("*.raw"))


def window_stamps(stamps, hours):
    """The stamps within `hours` of the newest one -- measured from the
    newest *frame*, not the clock, so if NOAA or the network goes away
    the last day it saw keeps looping instead of draining to nothing."""
    if not stamps:
        return []
    cutoff = stamp_to_datetime(stamps[-1]) - datetime.timedelta(hours=hours)
    return [s for s in stamps if stamp_to_datetime(s) >= cutoff]


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read()
        expected = resp.headers.get("Content-Length")
    if expected is not None and len(data) != int(expected):
        raise OSError(f"short read: {len(data)} of {expected} bytes")
    return data


def jpeg_complete(data):
    """True if the JPEG ends in its EOI marker. A truncated JPEG decodes
    without any error -- libjpeg just pads the missing rows with flat
    color, which is what put a frame with a solid yellow lower half into
    SKY 1.0's loop."""
    return data.rstrip(b"\0\r\n ").endswith(b"\xff\xd9")


def crop_rect(src_w, src_h, crop):
    """Largest 4:3 rect of the source, positioned vertically per `crop`."""
    w = src_w
    h = min(src_h, round(src_w * 3 / 4))
    if crop == "top":
        y = 0
    elif crop == "bottom":
        y = src_h - h
    else:
        y = (src_h - h) // 2
    return pygame.Rect(0, y, w, h)


def run_fetcher(width, height, bpp):
    """--fetch: poll NOAA forever, keeping the cache to the last `hours`
    of frames. Run at low priority so it never competes with playback."""
    try:
        os.nice(10)
    except OSError:
        pass
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    pygame.init()
    settings = load_settings()
    cache_dir = cache_dir_for(width, height, bpp)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for leftover in cache_dir.glob("*.tmp"):
        leftover.unlink(missing_ok=True)
    rgb565 = pygame.Surface((width, height), 0, 16) if bpp == 16 else None
    marker = cache_dir / COMPLETE_MARKER

    while True:
        try:
            listing = http_get(settings["source_url"]).decode("utf-8", "replace")
        except OSError as exc:
            print(f"[fetch] directory listing failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(FETCH_RETRY)
            continue
        names = {stamp: name for name, stamp, size in FRAME_NAME_RE.findall(listing)
                 if size == settings["image_size"]}
        available = sorted(names)
        wanted = window_stamps(available, settings["hours"])
        have = set(cached_stamps(cache_dir))
        missing = [s for s in wanted if s not in have]
        if missing:
            print(f"[fetch] {len(missing)} new frame(s), {len(have)} cached", file=sys.stderr, flush=True)
        if len(missing) > BACKLOG_FRAMES:
            marker.unlink(missing_ok=True)
        failed = 0
        # Oldest first, so during the first backfill the loop grows in
        # time order rather than starting with a jump.
        for stamp in missing:
            name = names[stamp]
            try:
                data = http_get(settings["source_url"] + name)
                if not jpeg_complete(data):
                    raise OSError("truncated JPEG (no EOI marker), retrying next poll")
                img = pygame.image.load(io.BytesIO(data), name)
            except (OSError, pygame.error) as exc:
                print(f"[fetch] {name}: {exc}", file=sys.stderr, flush=True)
                failed += 1
                continue
            rect = crop_rect(*img.get_size(), settings["crop"])
            frame = pygame.transform.smoothscale(img.subsurface(rect), (width, height))
            tmp = cache_dir / f"{stamp}.tmp"
            tmp.write_bytes(surface_to_raw(frame, bpp, rgb565))
            tmp.rename(cache_dir / f"{stamp}.raw")
        if wanted and not failed:
            marker.touch()
        # Prune frames that have aged out of the window. An empty listing
        # prunes nothing -- a stale loop beats a black screen.
        if wanted:
            keep = set(wanted)
            for stamp in cached_stamps(cache_dir):
                if stamp not in keep and stamp < wanted[0]:
                    (cache_dir / f"{stamp}.raw").unlink(missing_ok=True)
        time.sleep(FETCH_INTERVAL)


# --- Player ------------------------------------------------------------

class SpaceTruckinApp:
    def __init__(self):
        self.settings = load_settings()
        self._quit_requested = False
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        pygame.init()
        pygame.display.set_mode((FRAME_W, FRAME_H))  # headless (dummy driver)
        self.clock = pygame.time.Clock()
        self.fb = FrameBuffer()
        self.cache_dir = cache_dir_for(self.fb.width, self.fb.height, self.fb.bpp)
        self.frame_bytes = self.fb.width * self.fb.height * self.fb.bypp

        self.fps = self.settings["fps"]
        self.frames = []  # stamps in the current loop
        self.index = 0
        self.current_raw = None
        self.hold_left = 0  # frames left to hold the newest frame before looping
        self.next_rescan = 0.0
        self.complete = False  # COMPLETE_MARKER seen at the last rescan -- sets the stamp color
        self.stamp_font = pygame.font.Font(str(FONT_PATH), STAMP_FONT_SIZE)
        self.stamp_cache = {}  # (stamp, complete) -> (x, y, pixels, mask) -- see draw_stamp()

        self.osd_font = pygame.font.Font(str(FONT_PATH), 36)
        self.option_font = pygame.font.Font(str(FONT_PATH), 32)
        self.about_body_font = pygame.font.Font(str(FONT_PATH), 26)
        self.about_logo = load_about_logo()
        self.about_canvas = None  # built once per BACK press -- see toggle_about()
        self.splash_canvas, self.waiting_canvas = build_splash_canvases()
        self.power_dialog_active = False
        self.power_dialog_selection = 0  # index into POWER_OPTIONS, defaults to NO
        self.pending_power_action = None  # None, "shutdown", or "restart"
        self.pending_exit_code = 0  # 0 or EXIT_GOTO_HOME
        self._rel_accum = {"x": 0, "y": 0}

        self.kbd_devices = find_keyboard_devices()
        self.selector = selectors.DefaultSelector()
        for dev in self.kbd_devices:
            self.selector.register(dev, selectors.EVENT_READ)
            try:
                dev.grab()  # exclusive -- see loudness.py
            except OSError as exc:
                print(f"Couldn't grab {dev.path} exclusively: {exc}", file=sys.stderr)

        # Console-graphics-mode dance copied from bars.py/loudness.py --
        # keeps the kernel's text console from drawing over our writes.
        self.tty_fd = None
        self.console_graphics_mode = False
        try:
            self.tty_fd = os.open("/dev/tty", os.O_RDWR)
            fcntl.ioctl(self.tty_fd, KDSETMODE, KD_GRAPHICS)
            self.console_graphics_mode = True
        except OSError as exc:
            print(f"Console graphics mode not available: {exc}", file=sys.stderr)

        # Started before the splash so the first backfill is already
        # under way while it's up. The marker goes first: completeness is
        # only ever vouched for by the fetcher running now.
        (self.cache_dir / COMPLETE_MARKER).unlink(missing_ok=True)
        self.fetcher = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--fetch",
             str(self.fb.width), str(self.fb.height), str(self.fb.bpp)])

        # Splash for SPLASH_SECONDS. With nothing cached yet it carries
        # WAITING FOR SATELLITE, and render() keeps it up until there's a
        # frame to play.
        self.rescan()
        splash = self.splash_canvas if self.frames else self.waiting_canvas
        if splash is not None:
            self.fb.write_surface(splash)
            time.sleep(SPLASH_SECONDS)
        if self.waiting_canvas is None:
            self.waiting_canvas = self.build_waiting_canvas()

    def _handle_signal(self, signum, frame):
        self._quit_requested = True

    # Loop bookkeeping

    def rescan(self):
        # Marker checked first: the fetcher only writes it after the frames
        # it vouches for, so a loop listed after seeing it is the whole day.
        self.complete = (self.cache_dir / COMPLETE_MARKER).exists()
        self.frames = window_stamps(cached_stamps(self.cache_dir), self.settings["hours"])
        self.next_rescan = time.monotonic() + RESCAN_SECONDS

    def advance(self):
        """Steps to the next frame, holding on the newest for hold_seconds
        and picking up newly fetched frames each time the loop wraps."""
        if self.hold_left > 0:
            self.hold_left -= 1
            return
        self.index += 1
        if self.index >= len(self.frames):
            self.rescan()
            self.index = 0
        elif self.index == len(self.frames) - 1:
            self.hold_left = round(self.settings["hold_seconds"] * self.fps)

    def load_current(self):
        """Reads the current frame's raw bytes, or None if it's gone (the
        fetcher pruned it) or truncated."""
        try:
            raw = (self.cache_dir / f"{self.frames[self.index]}.raw").read_bytes()
        except (OSError, IndexError):
            return None
        return raw if len(raw) == self.frame_bytes else None

    def draw_stamp(self, raw):
        """The current frame's capture time over its lower right corner,
        drawn straight into a copy of the raw frame bytes -- WHITE while
        the loop is still building, YELLOW once it's the complete day.
        The pixel font is rendered without antialiasing, so this is a
        plain masked copy with no blending."""
        stamp = self.frames[self.index]
        key = (stamp, self.complete)
        cached = self.stamp_cache.get(key)
        if cached is None:
            text = stamp_to_datetime(stamp).astimezone(STAMP_TZ).strftime(STAMP_FORMAT)
            fg = self.stamp_font.render(text, False, YELLOW if self.complete else WHITE)
            fw, fh = fg.get_size()
            w, h = fw + STAMP_SHADOW, fh + STAMP_SHADOW
            surf = pygame.Surface((w, h))
            surf.fill(BLACK)  # the shadow is just these black pixels let through by the mask
            surf.blit(fg, (0, 0))
            text_mask = pygame.surfarray.array_red(pygame.mask.from_surface(fg).to_surface()).T > 0
            mask = np.zeros((h, w), dtype=bool)
            mask[STAMP_SHADOW:, STAMP_SHADOW:] |= text_mask
            mask[:fh, :fw] |= text_mask
            dtype = np.uint16 if self.fb.bpp == 16 else np.uint32
            pixels = np.frombuffer(surface_to_raw(surf, self.fb.bpp), dtype=dtype).reshape(h, w)
            x = self.fb.width - STAMP_MARGIN_X - w
            y = self.fb.height - STAMP_MARGIN_Y - h
            if len(self.stamp_cache) > 2 * len(self.frames) + 16:
                self.stamp_cache.clear()  # drop stamps for frames that aged out
            cached = self.stamp_cache[key] = (x, y, pixels.copy(), mask)
        x, y, pixels, mask = cached
        frame = np.frombuffer(raw, dtype=pixels.dtype).reshape(self.fb.height, self.fb.width).copy()
        region = frame[y:y + pixels.shape[0], x:x + pixels.shape[1]]
        region[mask] = pixels[mask]
        return frame.tobytes()

    def set_fps(self, direction):
        steps = list(FPS_STEPS)
        nearest = min(range(len(steps)), key=lambda i: abs(steps[i] - self.fps))
        self.fps = steps[max(0, min(len(steps) - 1, nearest + direction))]
        print(f"fps: {self.fps}", file=sys.stderr)
        save_setting("spacetruckin", "fps", self.fps)

    # Screens

    def toggle_about(self):
        if self.about_canvas is not None:
            self.about_canvas = None
            return
        s = self.settings
        if self.frames:
            newest = stamp_to_datetime(self.frames[-1]).astimezone(STAMP_TZ)
            newest_text = newest.strftime("%d %b %I:%M %p %Z").upper().lstrip("0")
            span = f"{len(self.frames)} FRAMES" + ("" if self.complete else ", BUILDING")
        else:
            newest_text = "NONE YET"
            span = "DOWNLOADING"
        canvas = pygame.Surface((FRAME_W, FRAME_H))
        draw_about_screen(canvas, self.osd_font, self.about_body_font, self.about_logo,
                          f"SPACE TRUCKIN {VERSION}", [
                              ("SOURCE", "GOES-19 GEOCOLOR"),
                              ("SECTOR", "SOUTHERN PLAINS"),
                              ("LOOP", f"{s['hours']:g} HOURS"),
                              ("", span),
                              ("NEWEST", newest_text),
                              ("SPEED", f"{self.fps} FPS"),
                              ("CROP", s["crop"].upper()),
                          ])
        self.about_canvas = canvas

    def build_waiting_canvas(self):
        # Fallback for when splash.png won't load.
        canvas = pygame.Surface((FRAME_W, FRAME_H))
        canvas.fill(BLACK)
        surf = self.about_body_font.render("WAITING FOR SATELLITE", True, ORANGE)
        canvas.blit(surf, ((FRAME_W - surf.get_width()) // 2, (FRAME_H - surf.get_height()) // 2))
        return canvas

    def draw_power_dialog(self, canvas):
        lines = ["ARE YOU SURE YOU WANT", "TO SHUT DOWN?"]
        line_surfs = [self.osd_font.render(line, True, WHITE) for line in lines]
        option_surfs = [
            self.option_font.render(opt, True, BLACK if i == self.power_dialog_selection else ORANGE)
            for i, opt in enumerate(POWER_OPTIONS)
        ]
        pad_x, pad_y, gap = 40, 24, 40
        options_w = sum(s.get_width() for s in option_surfs) + gap * (len(option_surfs) - 1)
        content_w = max(max(s.get_width() for s in line_surfs), options_w)
        content_h = (sum(s.get_height() for s in line_surfs) + 10 * (len(line_surfs) - 1)
                     + 30 + option_surfs[0].get_height())
        box = pygame.Surface((content_w + pad_x * 2, content_h + pad_y * 2))
        box.fill(BLACK)
        pygame.draw.rect(box, ORANGE, box.get_rect(), 3)
        y = pad_y
        for surf in line_surfs:
            box.blit(surf, ((box.get_width() - surf.get_width()) // 2, y))
            y += surf.get_height() + 10
        y += 20
        x = (box.get_width() - options_w) // 2
        for i, surf in enumerate(option_surfs):
            if i == self.power_dialog_selection:
                highlight = pygame.Rect(x - 10, y - 6, surf.get_width() + 20, surf.get_height() + 12)
                pygame.draw.rect(box, ORANGE, highlight)
            box.blit(surf, (x, y))
            x += surf.get_width() + gap
        frame_w, frame_h = canvas.get_size()
        canvas.blit(box, ((frame_w - box.get_width()) // 2, (frame_h - box.get_height()) // 2))

    # Input

    def handle_keycode(self, code):
        # Same key map as the rest of the fleet: Home/Q/Esc/Menu quit, BACK
        # toggles ABOUT, Power opens the shutdown dialog. Left/Right step
        # the loop speed.
        if self.power_dialog_active:
            return self.handle_power_dialog_keycode(code)
        if code in (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME):
            return "quit_home"
        elif code in (ecodes.KEY_Q, ecodes.KEY_ESC, ecodes.KEY_COMPOSE):
            return "quit"
        elif code == ecodes.KEY_BACK:
            self.toggle_about()
        elif code == ecodes.KEY_POWER:
            self.power_dialog_active = True
            self.power_dialog_selection = 0
        elif self.about_canvas is not None:
            return None
        elif code == ecodes.KEY_LEFT:
            self.set_fps(-1)
        elif code == ecodes.KEY_RIGHT:
            self.set_fps(1)
        return None

    def handle_power_dialog_keycode(self, code):
        if code in (ecodes.KEY_LEFT, ecodes.KEY_UP):
            self.power_dialog_selection = (self.power_dialog_selection - 1) % len(POWER_OPTIONS)
        elif code in (ecodes.KEY_RIGHT, ecodes.KEY_DOWN):
            self.power_dialog_selection = (self.power_dialog_selection + 1) % len(POWER_OPTIONS)
        elif code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT, ecodes.BTN_MOUSE):
            choice = POWER_OPTIONS[self.power_dialog_selection]
            if choice == "NO":
                self.power_dialog_active = False
            else:
                return "shutdown" if choice == "YES" else "restart"
        elif code in (ecodes.KEY_ESC, ecodes.KEY_BACK, ecodes.KEY_POWER):
            self.power_dialog_active = False
        return None

    def handle_rel_event(self, code, value):
        """Air-mouse movement -> discrete D-pad presses, as in loudness.py."""
        if code == ecodes.REL_X:
            axis = "x"
        elif code == ecodes.REL_Y:
            axis = "y"
        else:
            return None
        self._rel_accum[axis] += value
        accum = self._rel_accum[axis]
        if abs(accum) < MOUSE_MOVE_THRESHOLD:
            return None
        self._rel_accum[axis] = 0
        if axis == "x":
            synthetic = ecodes.KEY_RIGHT if accum > 0 else ecodes.KEY_LEFT
        else:
            synthetic = ecodes.KEY_DOWN if accum > 0 else ecodes.KEY_UP
        return self.handle_keycode(synthetic)

    # Main loop

    def render(self):
        if self.about_canvas is not None:
            canvas = self.about_canvas.copy()
            if self.power_dialog_active:
                self.draw_power_dialog(canvas)
            self.fb.write_surface(canvas)
            return
        if self.power_dialog_active:
            # Playback pauses under the dialog.
            canvas = self.fb.raw_to_surface(self.current_raw) if self.current_raw else None
            if canvas is None:
                canvas = pygame.Surface((self.fb.width, self.fb.height))
                canvas.fill(BLACK)
            self.draw_power_dialog(canvas)
            self.fb.write_surface(canvas)
            return
        if not self.frames:
            if time.monotonic() >= self.next_rescan:
                self.rescan()
                self.index = 0
            if not self.frames:
                if self.waiting_canvas is None:
                    self.waiting_canvas = self.build_waiting_canvas()
                self.fb.write_surface(self.waiting_canvas)
                return
        raw = self.load_current()
        if raw is None:
            # Pruned out from under us -- pick the loop back up from scratch.
            self.rescan()
            self.index = 0
            return
        raw = self.draw_stamp(raw)
        self.current_raw = raw
        self.fb.write_raw(raw)
        self.advance()

    def run(self):
        try:
            running = True
            while running and not self._quit_requested:
                for key, _ in self.selector.select(timeout=0):
                    device = key.fileobj
                    for event in device.read():
                        if event.type == ecodes.EV_KEY and event.value == 1:  # 1 == key down
                            result = self.handle_keycode(event.code)
                        elif event.type == ecodes.EV_REL:
                            result = self.handle_rel_event(event.code, event.value)
                        else:
                            continue
                        if result == "quit":
                            running = False
                        elif result == "quit_home":
                            running = False
                            self.pending_exit_code = EXIT_GOTO_HOME
                        elif result in ("shutdown", "restart"):
                            self.pending_power_action = result
                            running = False
                if not running:
                    break
                if self.fetcher.poll() is not None:
                    sys.exit(f"Fetcher exited (code {self.fetcher.returncode})")
                self.render()
                # ABOUT/dialog/waiting screens don't change frame to frame,
                # so they idle at a low rate.
                idle = self.about_canvas is not None or self.power_dialog_active or not self.frames
                self.clock.tick(10 if idle else self.fps)
        finally:
            if self.fetcher.poll() is None:
                self.fetcher.terminate()
                try:
                    self.fetcher.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.fetcher.kill()
            for dev in self.kbd_devices:
                try:
                    dev.ungrab()
                except OSError:
                    pass
            self.fb.close()
            if self.console_graphics_mode:
                fcntl.ioctl(self.tty_fd, KDSETMODE, KD_TEXT)
                os.write(self.tty_fd, b"\033[2J\033[H")  # clear screen, home cursor
            if self.tty_fd is not None:
                os.close(self.tty_fd)
            pygame.quit()

        if self.pending_power_action == "shutdown":
            subprocess.run(["sudo", "shutdown", "-h", "now"])
        elif self.pending_power_action == "restart":
            subprocess.run(["sudo", "shutdown", "-r", "now"])

        sys.exit(self.pending_exit_code)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fetch", nargs=3, type=int, metavar=("WIDTH", "HEIGHT", "BPP"),
                        help="run the background fetcher (started by the player)")
    args = parser.parse_args()
    if args.fetch:
        run_fetcher(*args.fetch)
    else:
        SpaceTruckinApp().run()


if __name__ == "__main__":
    main()
