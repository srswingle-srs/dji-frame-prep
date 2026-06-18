# Packaging & Release Guide

This is the maintainer's guide for cutting a public release of DJI Frame Prep. End users don't need any of this — they just download a release and run `run.bat`.

---

## Repo layout

A typical release ships these files. Keep the structure flat so `run.bat` and `gui.py` sit at the top level where users can find them.

```
dji-frame-prep/
├── run.bat              Entry point — installs deps, launches the GUI
├── gui.py               The PyQt6 application
├── requirements.txt     Python dependencies
├── README.md            User-facing docs
├── MANUAL.txt           Plain-text quick-start (shipped alongside the app)
├── LICENSE
├── CHANGELOG.md
└── .gitignore
```

> Note the troubleshooting docs reference `error_log.txt` "next to `gui.py`" — so `gui.py` must live at the root of the distributed folder, not in a subfolder.

---

## Before you tag a release

Run through this checklist:

- [ ] `run.bat` launches cleanly on a fresh Windows 10 and Windows 11 machine (or VM) with nothing pre-installed.
- [ ] The first-run dependency install works end to end, including the "restart and run again" path if it triggers.
- [ ] A real `.MP4` + `.SRT` pair extracts frames, geotags them, and writes a valid `geo.txt`.
- [ ] The resulting `group_flight1/` folder imports into WebODM and produces an orthomosaic.
- [ ] The altitude filter actually moves rejected frames to `rejected/` (and doesn't delete them).
- [ ] `manifest.json` is written and contains the run settings.
- [ ] `CHANGELOG.md` has an entry for this version.
- [ ] Version number is bumped everywhere it appears (see below).

---

## Versioning

Use [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **MAJOR** — breaking change (output format changes, WebODM import behavior changes).
- **MINOR** — new feature, backward compatible (new setting, new flight pattern).
- **PATCH** — bug fix, no new behavior.

Bump the version in:
- `CHANGELOG.md`
- the Git tag (`v1.2.0`)
- the window title / about box in `gui.py`, if present

---

## Cutting the release

1. **Finalize the changelog.** Move items out of `[Unreleased]` into a dated version section.
2. **Commit.** `git commit -am "Release v1.x.x"`
3. **Tag.** `git tag -a v1.x.x -m "v1.x.x"` then `git push --tags`
4. **Build the download zip.** Zip the repo contents (not the `.git` folder). Name it clearly:
   ```
   dji-frame-prep-v1.x.x.zip
   ```
   Confirm `run.bat` and `gui.py` are at the top level inside the zip, not nested in an extra folder.
5. **Create the GitHub Release.**
   - Go to **Releases → Draft a new release**.
   - Choose the tag you just pushed.
   - Title it `DJI Frame Prep v1.x.x`.
   - Paste the changelog entry for this version into the description.
   - Attach `dji-frame-prep-v1.x.x.zip` as a binary asset.
   - Publish.

End users download the zip from the Releases page, unzip, and double-click `run.bat`.

---

## Optional: standalone .exe

The default distribution relies on Python being present (or installed via `run.bat`). If you want a zero-Python download for non-technical users, you can bundle with PyInstaller:

```
pip install pyinstaller
pyinstaller --onefile --windowed --name "DJI Frame Prep" gui.py
```

Caveats:
- **ffmpeg is still a separate dependency.** PyInstaller bundles Python, not ffmpeg. Either ship an `ffmpeg.exe` next to the build and point the app at it, or keep the `winget install ffmpeg` instruction in your docs.
- Antivirus false positives are common with `--onefile` PyInstaller builds. Code signing reduces this; without it, expect some SmartScreen warnings.
- Test the bundled `.exe` on a clean machine — bundling frequently misses a hidden import.

If you go the `.exe` route, attach both the zip (source/script) and the `.exe` to the release so users can pick.

---

## Code signing (optional but recommended for public distribution)

Unsigned executables and scripts trigger Windows SmartScreen warnings that scare off non-technical users. An OV or IV code-signing certificate from a recognized CA removes most of these. This is optional and only relevant if you ship an `.exe`; the script-based `run.bat` distribution doesn't strictly need it, though SmartScreen may still warn on first run.
