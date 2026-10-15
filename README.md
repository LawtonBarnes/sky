# SKY

Loops the last 24 hours of GOES-19 GEOCOLOR satellite imagery full-screen
on a CRT -- a Raspberry Pi 3B on composite video, part of the METAL SHOP
McBrain fleet (launched by STRINGS, assigned from SCRUTE).

No overlays, no text: just the loop. It plays from 24 hours ago up to the
newest frame NOAA has published, holds there for a second, and starts
over. GEOCOLOR shows true-color clouds by day and city lights by night, so
every loop sweeps a full day/night cycle.

## Source

[NOAA STAR's GOES imagery CDN](https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/sp/GEOCOLOR/),
the Southern Plains (`sp`) sector -- a new 600x600 JPEG every 5 minutes
(288 a day), with about 10 days kept online. Each frame is cropped to a
4:3 slice (the middle 600x450 by default, which also leaves out NOAA's
caption bar and logo) and scaled to the 720x480 CRT frame.

## How it works

- **Fetcher** (`sky.py --fetch`, started by the player as a low-priority
  child process): polls the directory every 5 minutes, downloads new
  frames, and stores each one already cropped, scaled and packed into the
  framebuffer's raw pixel format under `cache/<W>x<H>x<bpp>/`. Frames
  older than the loop window are deleted. The first run backfills the
  whole day, about 5 minutes on a Pi 3B -- the loop grows as it goes.
- **Player** (`sky.py`): copies cached frames straight to `/dev/fb0`,
  vsynced. Decoding and scaling a JPEG takes ~55ms on a Pi 3B, which is
  why that's all done ahead of time in the fetcher; playback itself uses
  ~6% CPU.

If NOAA or the network goes away, the last day it got keeps looping (the
window is measured back from the newest *frame*, not the clock).

## Controls

| Key | Action |
|---|---|
| Left / Right | Loop speed: 5 / 10 / 15 / 20 / 30 fps (saved) |
| Back | ABOUT screen (source, loop length, newest frame, speed) |
| Power | Shutdown dialog |
| Home | Quit to SCRUTE |
| Q / Esc / Menu | Quit |

## Settings

Copy `settings.example.ini` to `settings.ini` (untracked, per machine);
every option is documented there: source URL and image size, loop length
in hours, fps, hold on the newest frame, and crop (`top` / `center` /
`bottom`). Changing the crop means deleting `cache/` so frames rebuild.

## Install

```sh
sudo git clone https://github.com/LawtonBarnes/sky /opt/sky
sudo chown -R metalshop:metalshop /opt/sky
cp /opt/sky/settings.example.ini /opt/sky/settings.ini
sudo install -m 755 /opt/sky/sky-launcher /usr/local/bin/sky
```

Requires `python3-pygame`, `python3-evdev` and `python3-numpy`, the same as
the other fleet apps.
