"""
DJI Frame Prep — Core Logic
All pipeline logic: SRT parsing, flight grouping, ffmpeg wrapper,
frame->GPS mapping, altitude filtering, geo.txt / manifest writing.
No Qt imports — pure stdlib + subprocess.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional


def _subprocess_hide_window() -> dict:
    """Return kwargs to hide console windows on Windows."""
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class GpsFix:
    frame_index: int        # 0-based block index within the segment
    wall_clock: datetime    # parsed from the SRT timestamp line
    lat: float
    lon: float
    rel_alt: float
    abs_alt: float


@dataclass
class Segment:
    basename: str           # e.g. "DJI_20260525133456_0005_D"
    mp4_path: Path
    srt_path: Path
    seg_index: int          # the NNNN number
    start_time: datetime    # first SRT fix wall-clock
    end_time: datetime      # last SRT fix wall-clock
    duration_s: float       # from ffprobe
    fps: float              # from ffprobe r_frame_rate
    fixes: list[GpsFix] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    included: bool = True   # user checkbox state


@dataclass
class FlightGroup:
    group_id: int
    segments: list[Segment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DJI_RE = re.compile(
    r"^(DJI_(\d{14})_(\d{4})_D)\.(MP4|SRT|LRF)$", re.IGNORECASE
)

SRT_RE = re.compile(
    r"latitude:\s*(-?\d+\.\d+).*?"
    r"longitude:\s*(-?\d+\.\d+).*?"
    r"rel_alt:\s*(-?\d+\.\d+)\s+abs_alt:\s*(-?\d+\.\d+)",
    re.DOTALL,
)

WALL_CLOCK_RE = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)")

FLIGHT_GAP_THRESHOLD_S = 10.0
JUNK_CLIP_THRESHOLD_S = 5.0
GPS_JUMP_THRESHOLD_M = 100.0

DEFAULT_INTERVALS = {
    "Crosshatch": 2.0,
    "Lawnmower": 1.2,
}

APPROX_FRAME_SIZE_MB = 2.0  # rough average for 4K JPEG q:v 2


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two GPS points."""
    R = 6_371_000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def check_ffmpeg() -> tuple[bool, bool]:
    """Return (ffmpeg_found, ffprobe_found)."""
    ff = shutil.which("ffmpeg") is not None
    fp = shutil.which("ffprobe") is not None
    return ff, fp


# ---------------------------------------------------------------------------
# SRT parsing
# ---------------------------------------------------------------------------

def parse_srt(srt_path: Path) -> tuple[list[GpsFix], int, int]:
    """
    Parse a DJI SRT file.
    Returns (fixes, total_blocks, skipped_blocks).
    """
    text = srt_path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\s*\n", text.strip())

    fixes: list[GpsFix] = []
    skipped = 0

    for idx, block in enumerate(blocks):
        gps_match = SRT_RE.search(block)
        clock_match = WALL_CLOCK_RE.search(block)
        if not gps_match or not clock_match:
            skipped += 1
            continue

        wall_clock = datetime.strptime(clock_match.group(1), "%Y-%m-%d %H:%M:%S.%f")
        fixes.append(GpsFix(
            frame_index=idx,
            wall_clock=wall_clock,
            lat=float(gps_match.group(1)),
            lon=float(gps_match.group(2)),
            rel_alt=float(gps_match.group(3)),
            abs_alt=float(gps_match.group(4)),
        ))

    return fixes, len(blocks), skipped


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------

def probe_video(mp4_path: Path) -> tuple[float, float]:
    """
    Get duration (seconds) and fps from a single ffprobe call.
    Returns (duration_s, fps).
    """
    cmd = [
        "ffprobe", "-v", "0",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-show_entries", "format=duration",
        "-of", "default=nw=1",
        str(mp4_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                            **_subprocess_hide_window())
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {mp4_path.name}: {result.stderr.strip()}")

    duration = 0.0
    fps = 0.0
    for line in result.stdout.strip().splitlines():
        if line.startswith("r_frame_rate="):
            val = line.split("=", 1)[1]
            frac = Fraction(val)
            fps = float(frac)
        elif line.startswith("duration="):
            duration = float(line.split("=", 1)[1])

    if fps <= 0:
        raise RuntimeError(f"Could not determine fps for {mp4_path.name}")
    if duration <= 0:
        raise RuntimeError(f"Could not determine duration for {mp4_path.name}")

    return duration, fps


# ---------------------------------------------------------------------------
# Folder scanning & segment building
# ---------------------------------------------------------------------------

def scan_folder(folder: Path) -> tuple[list[Segment], list[str]]:
    """
    Scan a folder for DJI MP4/SRT pairs.
    Returns (segments, warnings). Segments sorted by start_time.
    """
    warnings: list[str] = []

    # Gather all DJI files by basename
    by_base: dict[str, dict[str, Path]] = {}
    for f in folder.iterdir():
        if not f.is_file():
            continue
        m = DJI_RE.match(f.name)
        if not m:
            continue
        base = m.group(1)
        ext = m.group(4).upper()
        by_base.setdefault(base, {})[ext] = f

    segments: list[Segment] = []
    for base, exts in sorted(by_base.items()):
        mp4 = exts.get("MP4")
        srt = exts.get("SRT")
        if mp4 and not srt:
            warnings.append(f"{base}.MP4 has no matching .SRT — excluded.")
            continue
        if srt and not mp4:
            warnings.append(f"{base}.SRT has no matching .MP4 — excluded.")
            continue
        if not mp4 or not srt:
            continue

        # Parse the segment index from the basename
        m2 = DJI_RE.match(mp4.name)
        seg_index = int(m2.group(3)) if m2 else 0

        # Parse SRT
        fixes, total_blocks, skipped = parse_srt(srt)
        seg_warnings: list[str] = []
        if skipped > 0:
            seg_warnings.append(f"SRT: {skipped}/{total_blocks} blocks skipped (malformed).")

        if not fixes:
            warnings.append(f"{base}: SRT has no valid GPS fixes — excluded.")
            continue

        # Probe video
        try:
            duration_s, fps = probe_video(mp4)
        except RuntimeError as e:
            warnings.append(str(e))
            continue

        seg = Segment(
            basename=base,
            mp4_path=mp4,
            srt_path=srt,
            seg_index=seg_index,
            start_time=fixes[0].wall_clock,
            end_time=fixes[-1].wall_clock,
            duration_s=duration_s,
            fps=fps,
            fixes=fixes,
            warnings=seg_warnings,
        )

        # Flag junk clips
        if duration_s < JUNK_CLIP_THRESHOLD_S:
            seg.warnings.append(f"Junk clip ({duration_s:.1f}s < {JUNK_CLIP_THRESHOLD_S}s threshold).")
            seg.included = False

        segments.append(seg)

    segments.sort(key=lambda s: s.start_time)
    return segments, warnings


# ---------------------------------------------------------------------------
# Flight grouping
# ---------------------------------------------------------------------------

def group_flights(segments: list[Segment]) -> list[FlightGroup]:
    """
    Group sorted segments into continuous flight groups.
    Uses gap threshold and GPS continuity check.
    """
    if not segments:
        return []

    groups: list[FlightGroup] = []
    current = FlightGroup(group_id=1, segments=[segments[0]])

    for prev_seg, seg in zip(segments, segments[1:]):
        gap = (seg.start_time - prev_seg.end_time).total_seconds()
        if gap > FLIGHT_GAP_THRESHOLD_S:
            groups.append(current)
            current = FlightGroup(group_id=len(groups) + 1, segments=[seg])
        else:
            # GPS continuity check at the seam
            last_fix = prev_seg.fixes[-1]
            first_fix = seg.fixes[0]
            dist = haversine_m(last_fix.lat, last_fix.lon, first_fix.lat, first_fix.lon)
            if dist > GPS_JUMP_THRESHOLD_M:
                current.warnings.append(
                    f"GPS jump of {dist:.0f}m between {prev_seg.basename} and {seg.basename} "
                    f"(threshold {GPS_JUMP_THRESHOLD_M:.0f}m)."
                )
            current.segments.append(seg)

    groups.append(current)
    return groups


def split_group_on_exclusions(group: FlightGroup) -> list[FlightGroup]:
    """
    If a user un-checks a middle segment, split the group into sub-groups.
    Returns a list of FlightGroups containing only included segments.
    """
    sub_groups: list[FlightGroup] = []
    current_segs: list[Segment] = []

    for seg in group.segments:
        if seg.included:
            current_segs.append(seg)
        else:
            if current_segs:
                sub_groups.append(FlightGroup(
                    group_id=group.group_id,
                    segments=list(current_segs),
                    warnings=list(group.warnings),
                ))
                current_segs = []

    if current_segs:
        sub_groups.append(FlightGroup(
            group_id=group.group_id,
            segments=list(current_segs),
            warnings=list(group.warnings),
        ))

    # Renumber sub-groups if split occurred
    if len(sub_groups) > 1:
        for i, sg in enumerate(sub_groups):
            sg.group_id = group.group_id
            sg.warnings = list(group.warnings) + [
                f"Split into sub-group {i + 1}/{len(sub_groups)} due to excluded middle segment(s)."
            ]

    return sub_groups


# ---------------------------------------------------------------------------
# Altitude statistics & filtering
# ---------------------------------------------------------------------------

@dataclass
class AltitudeStats:
    min_alt: float
    max_alt: float
    mean_alt: float
    median_alt: float


def compute_altitude_stats(
    groups: list[FlightGroup],
    use_abs: bool = False,
) -> Optional[AltitudeStats]:
    """Compute altitude stats from ALL raw SRT fixes across included segments."""
    alts: list[float] = []
    for g in groups:
        for seg in g.segments:
            if not seg.included:
                continue
            for fix in seg.fixes:
                alts.append(fix.abs_alt if use_abs else fix.rel_alt)

    if not alts:
        return None

    alts_sorted = sorted(alts)
    n = len(alts_sorted)
    median = (alts_sorted[n // 2] if n % 2 == 1
              else (alts_sorted[n // 2 - 1] + alts_sorted[n // 2]) / 2)

    return AltitudeStats(
        min_alt=min(alts),
        max_alt=max(alts),
        mean_alt=sum(alts) / len(alts),
        median_alt=median,
    )


def filter_frames_by_altitude(
    frames: list[tuple[str, GpsFix]],
    mapping_height: float,
    band: float,
    use_abs: bool = False,
) -> tuple[list[tuple[str, GpsFix]], list[tuple[str, GpsFix]]]:
    """
    Partition frames into (kept, rejected) based on altitude band.
    Each frame is (filename, GpsFix).
    """
    kept: list[tuple[str, GpsFix]] = []
    rejected: list[tuple[str, GpsFix]] = []

    for fname, fix in frames:
        alt = fix.abs_alt if use_abs else fix.rel_alt
        if abs(alt - mapping_height) <= band:
            kept.append((fname, fix))
        else:
            rejected.append((fname, fix))

    return kept, rejected


# ---------------------------------------------------------------------------
# Hovering / near-stationary detection
# ---------------------------------------------------------------------------

def detect_hovering(
    frames: list[tuple[str, GpsFix]],
    threshold_m: float = 0.5,
    min_run: int = 5,
) -> list[str]:
    """
    Detect runs of consecutive frames with < threshold_m movement.
    Returns warning strings.
    """
    warnings: list[str] = []
    if len(frames) < 2:
        return warnings

    run_start = 0
    run_len = 1

    for i in range(1, len(frames)):
        dist = haversine_m(
            frames[i - 1][1].lat, frames[i - 1][1].lon,
            frames[i][1].lat, frames[i][1].lon,
        )
        if dist < threshold_m:
            run_len += 1
        else:
            if run_len >= min_run:
                warnings.append(
                    f"Near-stationary: frames {frames[run_start][0]} to "
                    f"{frames[run_start + run_len - 1][0]} ({run_len} frames, <{threshold_m}m movement). "
                    f"Hovering can degrade reconstruction."
                )
            run_start = i
            run_len = 1

    if run_len >= min_run:
        warnings.append(
            f"Near-stationary: frames {frames[run_start][0]} to "
            f"{frames[run_start + run_len - 1][0]} ({run_len} frames, <{threshold_m}m movement)."
        )

    return warnings


# ---------------------------------------------------------------------------
# Preview / estimation
# ---------------------------------------------------------------------------

@dataclass
class PreviewResult:
    total_frames: int
    estimated_size_mb: float
    per_segment: list[tuple[str, int]]  # (basename, frame_count)
    kept_after_filter: int
    rejected_by_filter: int


def estimate_frame_count(duration_s: float, interval_s: float) -> int:
    """Nominal frame count from ffmpeg fps=1/interval filter."""
    return int(math.floor(duration_s / interval_s)) + 1


def preview_extraction(
    groups: list[FlightGroup],
    interval_s: float,
    filter_enabled: bool,
    mapping_height: float,
    band: float,
    use_abs: bool = False,
) -> PreviewResult:
    """
    Estimate extraction results without running ffmpeg.
    Uses raw SRT fixes to estimate altitude filtering.
    """
    total = 0
    per_seg: list[tuple[str, int]] = []
    all_sampled_alts: list[float] = []

    for g in groups:
        for seg in g.segments:
            if not seg.included:
                continue
            count = estimate_frame_count(seg.duration_s, interval_s)
            total += count
            per_seg.append((seg.basename, count))

            # Sample the fixes at the extraction interval to estimate filtering
            for n in range(count):
                t = n * interval_s
                fix_idx = round(t * seg.fps)
                if fix_idx < len(seg.fixes):
                    fix = seg.fixes[fix_idx]
                    alt = fix.abs_alt if use_abs else fix.rel_alt
                    all_sampled_alts.append(alt)

    kept = total
    rejected = 0
    if filter_enabled and all_sampled_alts:
        kept = sum(1 for a in all_sampled_alts if abs(a - mapping_height) <= band)
        rejected = len(all_sampled_alts) - kept

    return PreviewResult(
        total_frames=total,
        estimated_size_mb=total * APPROX_FRAME_SIZE_MB,
        per_segment=per_seg,
        kept_after_filter=kept,
        rejected_by_filter=rejected,
    )


# ---------------------------------------------------------------------------
# Frame extraction (ffmpeg)
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    segment_basename: str
    frames: list[tuple[str, GpsFix]]  # (filename, fix)
    frame_count: int
    warnings: list[str]


def extract_frames_for_segment(
    seg: Segment,
    output_dir: Path,
    interval_s: float,
    jpeg_quality: int = 2,
    progress_cb: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> ExtractionResult:
    """
    Extract evenly-spaced frames from one segment via ffmpeg.
    Maps each frame to its GPS fix from the SRT.
    """
    warnings: list[str] = []
    prefix = seg.basename

    cmd = [
        "ffmpeg",
        "-i", str(seg.mp4_path),
        "-vf", f"fps=1/{interval_s}",
        "-q:v", str(jpeg_quality),
        "-loglevel", "error",
        str(output_dir / f"{prefix}_%05d.jpg"),
    ]

    if progress_cb:
        progress_cb(f"Extracting frames from {seg.basename}...")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **_subprocess_hide_window())

    # Wait for completion, checking for cancel
    while proc.poll() is None:
        if cancel_check and cancel_check():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise InterruptedError("Extraction cancelled by user.")
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass

    if proc.returncode != 0:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(
            f"ffmpeg failed on {seg.basename} (exit {proc.returncode}): {stderr.strip()}"
        )

    # Enumerate extracted frames and map to GPS
    frames: list[tuple[str, GpsFix]] = []
    n = 0
    while True:
        fname = f"{prefix}_{n + 1:05d}.jpg"
        fpath = output_dir / fname
        if not fpath.exists():
            break

        t = n * interval_s
        fix_idx = round(t * seg.fps)

        if fix_idx >= len(seg.fixes):
            # Frame beyond SRT coverage — remove it
            fpath.unlink()
            warnings.append(f"Removed {fname}: beyond SRT coverage (index {fix_idx} >= {len(seg.fixes)} fixes).")
            n += 1
            continue

        frames.append((fname, seg.fixes[fix_idx]))
        n += 1

    if progress_cb:
        progress_cb(f"{seg.basename}: extracted {len(frames)} geotagged frames.")

    return ExtractionResult(
        segment_basename=seg.basename,
        frames=frames,
        frame_count=len(frames),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# geo.txt writing
# ---------------------------------------------------------------------------

def write_geo_txt(
    output_dir: Path,
    frames: list[tuple[str, GpsFix]],
    use_abs: bool = False,
) -> Path:
    """Write WebODM geo.txt. Returns the path written."""
    geo_path = output_dir / "geo.txt"
    lines = ["EPSG:4326\n"]
    for fname, fix in frames:
        alt = fix.abs_alt if use_abs else fix.rel_alt
        lines.append(f"{fname} {fix.lon:.8f} {fix.lat:.8f} {alt:.3f}\n")
    geo_path.write_text("".join(lines), encoding="utf-8")
    return geo_path


# ---------------------------------------------------------------------------
# Manifest writing
# ---------------------------------------------------------------------------

def write_manifest(
    output_dir: Path,
    source_folder: Path,
    groups: list[FlightGroup],
    settings: dict,
    total_extracted: int,
    total_kept: int,
    total_rejected: int,
    alt_stats: Optional[AltitudeStats],
) -> Path:
    """Write manifest.json with run metadata."""
    manifest = {
        "timestamp": datetime.now().isoformat(),
        "source_folder": str(source_folder),
        "segments": [],
        "settings": settings,
        "results": {
            "total_extracted": total_extracted,
            "total_kept": total_kept,
            "total_rejected": total_rejected,
        },
    }

    if alt_stats:
        manifest["results"]["altitude_stats"] = {
            "min": round(alt_stats.min_alt, 3),
            "max": round(alt_stats.max_alt, 3),
            "mean": round(alt_stats.mean_alt, 3),
            "median": round(alt_stats.median_alt, 3),
        }

    for g in groups:
        for seg in g.segments:
            if seg.included:
                manifest["segments"].append({
                    "basename": seg.basename,
                    "duration_s": round(seg.duration_s, 2),
                    "fps": round(seg.fps, 2),
                    "fix_count": len(seg.fixes),
                    "group_id": g.group_id,
                })

    path = output_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Incomplete-run marker
# ---------------------------------------------------------------------------

def write_incomplete_marker(output_dir: Path) -> Path:
    p = output_dir / "INCOMPLETE_RUN"
    p.write_text(
        f"Run incomplete as of {datetime.now().isoformat()}.\n"
        "This folder does not contain a valid geo.txt.\n",
        encoding="utf-8",
    )
    return p


def remove_incomplete_marker(output_dir: Path) -> None:
    p = output_dir / "INCOMPLETE_RUN"
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Disk space check
# ---------------------------------------------------------------------------

def check_disk_space(output_dir: Path, estimated_mb: float) -> Optional[str]:
    """Return a warning string if insufficient space, else None."""
    try:
        stat = shutil.disk_usage(output_dir)
        free_mb = stat.free / (1024 * 1024)
        if free_mb < estimated_mb * 1.1:  # 10% margin
            return (
                f"Low disk space: {free_mb:.0f} MB free, "
                f"estimated need {estimated_mb:.0f} MB."
            )
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Full pipeline runner (called from GUI worker)
# ---------------------------------------------------------------------------

@dataclass
class GroupResult:
    group_id: int
    sub_group_index: int
    status: str = ""  # "success", "failed", "cancelled"
    error: str = ""
    frames_extracted: int = 0
    frames_kept: int = 0
    frames_rejected: int = 0
    geo_txt_path: Optional[Path] = None
    warnings: list[str] = field(default_factory=list)


def run_pipeline_for_group(
    group: FlightGroup,
    sub_group_index: int,
    output_base: Path,
    interval_s: float,
    jpeg_quality: int,
    filter_enabled: bool,
    mapping_height: float,
    band: float,
    use_abs: bool,
    progress_cb: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> GroupResult:
    """
    Run the full extraction pipeline for one (sub-)group.
    """
    result = GroupResult(
        group_id=group.group_id,
        sub_group_index=sub_group_index,
    )

    # Determine output dir
    suffix = f"_flight{group.group_id}"
    if sub_group_index > 0:
        suffix += f"_part{sub_group_index + 1}"
    output_dir = output_base / f"group{suffix}"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_incomplete_marker(output_dir)

    all_frames: list[tuple[str, GpsFix]] = []

    try:
        for seg in group.segments:
            if not seg.included:
                continue
            if cancel_check and cancel_check():
                result.status = "cancelled"
                result.error = "Cancelled by user."
                return result

            ext_result = extract_frames_for_segment(
                seg, output_dir, interval_s, jpeg_quality,
                progress_cb=progress_cb, cancel_check=cancel_check,
            )
            all_frames.extend(ext_result.frames)
            result.warnings.extend(ext_result.warnings)
            result.frames_extracted += ext_result.frame_count

    except InterruptedError:
        result.status = "cancelled"
        result.error = "Cancelled by user."
        return result
    except RuntimeError as e:
        result.status = "failed"
        result.error = str(e)
        return result

    # Altitude filtering
    if filter_enabled:
        kept, rejected = filter_frames_by_altitude(
            all_frames, mapping_height, band, use_abs
        )
        # Move rejected frames to rejected/ subfolder
        if rejected:
            rej_dir = output_dir / "rejected"
            rej_dir.mkdir(exist_ok=True)
            for fname, _ in rejected:
                src = output_dir / fname
                if src.exists():
                    shutil.move(str(src), str(rej_dir / fname))

        result.frames_kept = len(kept)
        result.frames_rejected = len(rejected)

        # Hovering detection on kept frames
        hover_warnings = detect_hovering(kept)
        result.warnings.extend(hover_warnings)

        final_frames = kept
    else:
        result.frames_kept = len(all_frames)
        result.frames_rejected = 0
        hover_warnings = detect_hovering(all_frames)
        result.warnings.extend(hover_warnings)
        final_frames = all_frames

    # Write geo.txt
    if progress_cb:
        progress_cb("Writing geo.txt...")
    geo_path = write_geo_txt(output_dir, final_frames, use_abs)
    result.geo_txt_path = geo_path

    # Remove incomplete marker on success
    remove_incomplete_marker(output_dir)

    result.status = "success"
    return result


# Future: parallel extraction (v2) — segments are independent,
# so 2-3 concurrent ffmpeg processes could halve extraction time.
# Deferred for failure-mode simplicity in v1.
