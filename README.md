# DJI Frame Prep

Turn DJI drone video into geotagged still frames ready for WebODM orthomosaic mapping.

DJI Frame Prep reads your drone's `.MP4` recordings and their matching `.SRT` GPS sidecar files, pulls evenly spaced still frames out of the video, geotags each one, and writes the `geo.txt` file that WebODM understands. It also filters out takeoff, landing, and transit frames automatically so they don't pollute your 3D model.

If you fly a roof or a parcel and want a clean, satellite-style orthomosaic out the other side, this gets your imagery into WebODM without hand-tagging anything.

---

## Features

- **Video-to-frames extraction** — pulls evenly spaced stills from DJI `.MP4` files
- **Automatic geotagging** — reads GPS from the `.SRT` sidecar and writes a WebODM-ready `geo.txt`
- **Altitude filtering** — drops takeoff/landing/transit frames that sit at the wrong height
- **Flight-pattern presets** — Crosshatch and Lawnmower presets set frame density for you
- **Preview before you commit** — estimated frame count and output size before extraction
- **Non-destructive** — rejected frames are moved to a `rejected/` subfolder, never deleted
- **Full run record** — every run writes a `manifest.json` capturing settings and results

---

## Requirements

| Component | Version |
|-----------|---------|
| Windows   | 10 or 11 |
| Python    | 3.8 or newer |
| ffmpeg    | any recent build (used for frame extraction) |
| PyQt6     | installed automatically |

`run.bat` installs anything missing on first launch. If it tells you to restart, close the window and double-click it again.

---

## Installation

1. Download the latest release from the [Releases](../../releases) page (or clone this repo).
2. Unzip it anywhere you like.
3. Double-click **`run.bat`**.
   - On first launch it installs any missing dependencies.
   - If prompted to restart, close the window and run `run.bat` again.

That's it. There's nothing else to configure to get started.

---

## Usage

1. **Select DJI Folder** — point it at the folder containing your `.MP4` and `.SRT` files. The tool scans and lists your flights in the tree on the left.
2. **Check your settings** on the right:
   - **Flight Pattern** — `Crosshatch` (flew a grid, two directions) or `Lawnmower` (back-and-forth rows, one direction).
   - **Mapping Height** — auto-detected from GPS; usually correct as-is.
   - **Altitude Filter** — leave ON to remove takeoff/landing frames.
3. **Preview** — see the estimated frame count and file size.
4. **Run** (or press Enter) — extraction can take several minutes; progress shows in the log area.
5. **Open Output Folder** — your `.jpg` frames and `geo.txt` are inside. Import that folder into WebODM.

---

## Settings

### Standard

- **Flight Pattern** — controls how densely frames are extracted. Crosshatch flights have built-in overlap, so fewer frames are needed; Lawnmower flights need more for good coverage.
- **Mapping Height (m)** — the altitude of your mapping pass. Auto-detected, but you can type a known planned height instead.
- **Altitude Filter** — when ON, discards frames from takeoff, landing, and transit. Rejected frames go to a `rejected/` subfolder.

### Advanced (click to expand)

- **Frame Interval (sec)** — seconds between extracted frames. Smaller = more frames = bigger output and longer processing. Set automatically by Flight Pattern; override if you want.
- **Altitude Band (± m)** — allowed altitude variation around the mapping height. Default is 5 m, so a 12 m flight keeps frames from 7 m to 17 m and rejects the rest.
- **Altitude Source** — `rel_alt` (default) is height above the takeoff point; `abs_alt` is height above sea level. Only use `abs_alt` if your workflow specifically needs sea-level altitudes.
- **JPEG Quality (1–31)** — lower number = better quality and larger files. Default `2` is recommended for photogrammetry; don't go above `5` unless you're saving disk space.

---

## Output

After a successful run, the output folder contains:

```
group_flight1/          Folder with all extracted frames
  *.jpg                 The geotagged frame images
  geo.txt               GPS coordinates for WebODM
  rejected/             Frames removed by the altitude filter
flight_pattern.txt      Note about which pattern was used
manifest.json           Full record of settings and results
```

Import the `group_flight1/` folder into WebODM to build your orthomosaic.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| **Run button is grayed out** | ffmpeg isn't installed. Close the app, run `winget install ffmpeg`, then restart. |
| **"No usable DJI segments found"** | Make sure you selected the folder with both `.MP4` *and* `.SRT` files — both are required for each segment. |
| **"Extraction Error" popup** | Check `error_log.txt` next to `gui.py` for details. |
| **App doesn't start at all** | Check `error_log.txt`. Usually Python or PyQt6 is missing — run `pip install PyQt6`. |
| **Frames look blurry** | Lower the JPEG Quality number (try `1` for best quality). |
| **Too many / too few frames** | Adjust the Frame Interval in Advanced Settings. Shorter = more frames, longer = fewer. |

---

## How it works

1. Reads your video files and the GPS data from their `.SRT` sidecars.
2. Extracts evenly spaced still frames from each video.
3. Tags each frame with its GPS location.
4. Writes a `geo.txt` file WebODM can read.
5. Filters out takeoff/landing/transit frames using the altitude band.

---

## License

Released under the MIT License — see [LICENSE](LICENSE).

---

## Contributing

Issues and pull requests are welcome. If you hit a crash, attach the contents of `error_log.txt` (found next to `gui.py`) so it's easier to track down.
