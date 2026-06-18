"""
Unit tests for core.py — synthetic fixtures, no real video needed.
Run with: python -m pytest test_core.py -v
"""

import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import core


# ---------------------------------------------------------------------------
# Synthetic SRT fixture
# ---------------------------------------------------------------------------

def make_srt_block(
    index: int,
    wall_clock: datetime,
    lat: float,
    lon: float,
    rel_alt: float,
    abs_alt: float,
) -> str:
    """Generate one DJI-style SRT subtitle block."""
    start_ms = (index - 1) * 16
    end_ms = index * 16
    start_tc = f"00:00:{start_ms // 1000:02d},{start_ms % 1000:03d}"
    end_tc = f"00:00:{end_ms // 1000:02d},{end_ms % 1000:03d}"
    clock_str = wall_clock.strftime("%Y-%m-%d %H:%M:%S.") + f"{wall_clock.microsecond // 1000:03d}"

    return (
        f"{index}\n"
        f"{start_tc} --> {end_tc}\n"
        f'<font size="28">FrameCnt: {index}, DiffTime: 16ms\n'
        f"{clock_str}\n"
        f"[iso: 250] [shutter: 1/2000.0] [fnum: 1.7] [ev: -0.3] "
        f"[color_md: hlg] [focal_len: 24.00] "
        f"[latitude: {lat:.6f}] [longitude: {lon:.6f}] "
        f"[rel_alt: {rel_alt:.3f} abs_alt: {abs_alt:.3f}] "
        f"[ct: 5400] </font>"
    )


def generate_srt(
    num_blocks: int,
    start_time: datetime,
    base_lat: float = 41.433028,
    base_lon: float = -75.484483,
    base_rel_alt: float = 12.4,
    base_abs_alt: float = 471.9,
    alt_pattern: list[float] | None = None,
) -> str:
    """Generate a complete synthetic SRT file."""
    blocks = []
    for i in range(num_blocks):
        wall = start_time + timedelta(milliseconds=i * 16)
        rel_alt = alt_pattern[i] if alt_pattern and i < len(alt_pattern) else base_rel_alt
        abs_alt = base_abs_alt + (rel_alt - base_rel_alt)
        # Slight lat drift to simulate movement
        lat = base_lat + i * 0.000001
        lon = base_lon + i * 0.000001
        blocks.append(make_srt_block(i + 1, wall, lat, lon, rel_alt, abs_alt))
    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Tests: SRT parser
# ---------------------------------------------------------------------------

class TestSrtParser:
    def test_parse_10_blocks(self, tmp_path: Path):
        srt_text = generate_srt(10, datetime(2026, 5, 25, 13, 21, 17))
        srt_file = tmp_path / "test.SRT"
        srt_file.write_text(srt_text, encoding="utf-8")

        fixes, total, skipped = core.parse_srt(srt_file)

        assert total == 10
        assert skipped == 0
        assert len(fixes) == 10
        assert fixes[0].lat == pytest.approx(41.433028, abs=1e-5)
        assert fixes[0].lon == pytest.approx(-75.484483, abs=1e-5)
        assert fixes[0].rel_alt == pytest.approx(12.4, abs=0.1)

    def test_parse_with_malformed_block(self, tmp_path: Path):
        srt_text = generate_srt(5, datetime(2026, 5, 25, 13, 0, 0))
        # Inject a malformed block
        srt_text += "\n\n6\n00:00:00,096 --> 00:00:00,112\n<font>GARBAGE</font>\n"

        srt_file = tmp_path / "test.SRT"
        srt_file.write_text(srt_text, encoding="utf-8")

        fixes, total, skipped = core.parse_srt(srt_file)

        assert len(fixes) == 5
        assert skipped == 1

    def test_wall_clock_parsed(self, tmp_path: Path):
        t0 = datetime(2026, 5, 25, 13, 21, 17, 742000)
        srt_text = generate_srt(1, t0)
        srt_file = tmp_path / "test.SRT"
        srt_file.write_text(srt_text, encoding="utf-8")

        fixes, _, _ = core.parse_srt(srt_file)
        assert fixes[0].wall_clock.year == 2026
        assert fixes[0].wall_clock.month == 5
        assert fixes[0].wall_clock.second == 17


# ---------------------------------------------------------------------------
# Tests: flight grouping
# ---------------------------------------------------------------------------

def _make_segment(
    basename: str,
    seg_index: int,
    start: datetime,
    duration_s: float,
    lat: float = 41.433,
    lon: float = -75.484,
) -> core.Segment:
    """Helper to build a Segment with minimal fields for grouping tests."""
    end = start + timedelta(seconds=duration_s)
    return core.Segment(
        basename=basename,
        mp4_path=Path(f"/fake/{basename}.MP4"),
        srt_path=Path(f"/fake/{basename}.SRT"),
        seg_index=seg_index,
        start_time=start,
        end_time=end,
        duration_s=duration_s,
        fps=59.94,
        fixes=[
            core.GpsFix(0, start, lat, lon, 12.4, 471.9),
            core.GpsFix(1, end, lat + 0.0001, lon + 0.0001, 12.4, 471.9),
        ],
    )


class TestFlightGrouping:
    def test_single_group(self):
        t0 = datetime(2026, 5, 25, 13, 0, 0)
        segs = [
            _make_segment("DJI_20260525130000_0005_D", 5, t0, 223),
            _make_segment("DJI_20260525130343_0006_D", 6, t0 + timedelta(seconds=226), 223),
            _make_segment("DJI_20260525130726_0007_D", 7, t0 + timedelta(seconds=449), 204),
        ]
        groups = core.group_flights(segs)
        assert len(groups) == 1
        assert len(groups[0].segments) == 3

    def test_two_groups_large_gap(self):
        t0 = datetime(2026, 5, 25, 13, 0, 0)
        segs = [
            _make_segment("DJI_20260525130000_0001_D", 1, t0, 200),
            _make_segment("DJI_20260525133500_0002_D", 2, t0 + timedelta(minutes=5), 200),
        ]
        groups = core.group_flights(segs)
        assert len(groups) == 2

    def test_junk_clip_excluded(self):
        t0 = datetime(2026, 5, 25, 13, 0, 0)
        seg = _make_segment("DJI_20260525130000_0001_D", 1, t0, 3.0)
        seg.included = False  # simulates junk detection
        segs = [seg]
        groups = core.group_flights(segs)
        assert len(groups) == 1
        assert not groups[0].segments[0].included

    def test_gps_jump_warning(self):
        t0 = datetime(2026, 5, 25, 13, 0, 0)
        seg1 = _make_segment("S1", 1, t0, 200, lat=41.433, lon=-75.484)
        # Place seg2 far away (different lat by ~0.01 = ~1.1 km)
        seg2 = _make_segment("S2", 2, t0 + timedelta(seconds=203), 200, lat=41.443, lon=-75.484)
        groups = core.group_flights([seg1, seg2])
        assert len(groups) == 1
        assert any("GPS jump" in w for w in groups[0].warnings)


# ---------------------------------------------------------------------------
# Tests: group splitting on middle exclusion
# ---------------------------------------------------------------------------

class TestGroupSplitting:
    def test_middle_exclusion_splits(self):
        t0 = datetime(2026, 5, 25, 13, 0, 0)
        segs = [
            _make_segment("S1", 1, t0, 200),
            _make_segment("S2", 2, t0 + timedelta(seconds=203), 200),
            _make_segment("S3", 3, t0 + timedelta(seconds=406), 200),
        ]
        segs[1].included = False  # exclude middle

        group = core.FlightGroup(group_id=1, segments=segs)
        sub = core.split_group_on_exclusions(group)

        assert len(sub) == 2
        assert sub[0].segments[0].basename == "S1"
        assert sub[1].segments[0].basename == "S3"

    def test_tail_exclusion_trims(self):
        t0 = datetime(2026, 5, 25, 13, 0, 0)
        segs = [
            _make_segment("S1", 1, t0, 200),
            _make_segment("S2", 2, t0 + timedelta(seconds=203), 200),
        ]
        segs[1].included = False

        group = core.FlightGroup(group_id=1, segments=segs)
        sub = core.split_group_on_exclusions(group)

        assert len(sub) == 1
        assert sub[0].segments[0].basename == "S1"


# ---------------------------------------------------------------------------
# Tests: altitude filtering
# ---------------------------------------------------------------------------

class TestAltitudeFilter:
    def _make_frames(self, alts: list[float]) -> list[tuple[str, core.GpsFix]]:
        return [
            (f"frame_{i:05d}.jpg", core.GpsFix(
                i, datetime(2026, 1, 1), 41.0, -75.0, alt, 470.0 + alt,
            ))
            for i, alt in enumerate(alts)
        ]

    def test_all_within_band(self):
        frames = self._make_frames([12.0, 12.5, 11.5, 13.0, 12.0])
        kept, rejected = core.filter_frames_by_altitude(frames, 12.0, 5.0)
        assert len(kept) == 5
        assert len(rejected) == 0

    def test_some_rejected(self):
        alts = [1.0, 5.0, 12.0, 12.5, 18.0, 12.0, 0.5]
        frames = self._make_frames(alts)
        kept, rejected = core.filter_frames_by_altitude(frames, 12.0, 5.0)
        # Within 12 +/- 5 = [7, 17]: 12.0, 12.5, 12.0 kept; 1.0, 5.0, 18.0, 0.5 rejected
        assert len(kept) == 3
        assert len(rejected) == 4

    def test_exact_boundary_kept(self):
        frames = self._make_frames([7.0, 17.0])  # exactly at boundary
        kept, rejected = core.filter_frames_by_altitude(frames, 12.0, 5.0)
        assert len(kept) == 2

    def test_abs_alt_mode(self):
        frames = self._make_frames([12.0])  # rel_alt=12, abs_alt=482
        kept, rejected = core.filter_frames_by_altitude(frames, 482.0, 5.0, use_abs=True)
        assert len(kept) == 1


# ---------------------------------------------------------------------------
# Tests: altitude stats
# ---------------------------------------------------------------------------

class TestAltitudeStats:
    def test_median_computation(self):
        t0 = datetime(2026, 5, 25, 13, 0, 0)
        seg = _make_segment("S1", 1, t0, 200)
        seg.fixes = [
            core.GpsFix(i, t0, 41.0, -75.0, alt, 470.0)
            for i, alt in enumerate([10.0, 12.0, 12.5, 13.0, 15.0])
        ]
        groups = [core.FlightGroup(group_id=1, segments=[seg])]
        stats = core.compute_altitude_stats(groups)
        assert stats is not None
        assert stats.median_alt == 12.5
        assert stats.min_alt == 10.0
        assert stats.max_alt == 15.0


# ---------------------------------------------------------------------------
# Tests: frame count estimation
# ---------------------------------------------------------------------------

class TestFrameCount:
    def test_nominal_count(self):
        # 223s at 1.5s interval: floor(223/1.5) + 1 = 148 + 1 = 149
        assert core.estimate_frame_count(223.0, 1.5) == 149

    def test_exact_boundary(self):
        # 6s at 2.0s: floor(6/2) + 1 = 3 + 1 = 4 frames (at t=0, 2, 4, 6)
        assert core.estimate_frame_count(6.0, 2.0) == 4

    def test_short_clip(self):
        # 3s at 2.0s: floor(3/2) + 1 = 1 + 1 = 2 frames (at t=0, 2)
        assert core.estimate_frame_count(3.0, 2.0) == 2


# ---------------------------------------------------------------------------
# Tests: haversine
# ---------------------------------------------------------------------------

class TestHaversine:
    def test_same_point(self):
        assert core.haversine_m(41.433, -75.484, 41.433, -75.484) == 0.0

    def test_known_distance(self):
        # ~111 km per degree of latitude
        d = core.haversine_m(0.0, 0.0, 1.0, 0.0)
        assert 110_000 < d < 112_000


# ---------------------------------------------------------------------------
# Tests: hovering detection
# ---------------------------------------------------------------------------

class TestHovering:
    def test_hovering_detected(self):
        fixes = [
            (f"f{i}.jpg", core.GpsFix(i, datetime(2026, 1, 1), 41.0, -75.0, 12.0, 470.0))
            for i in range(10)
        ]
        warnings = core.detect_hovering(fixes, threshold_m=0.5, min_run=5)
        assert len(warnings) >= 1
        assert "Near-stationary" in warnings[0]

    def test_no_hovering(self):
        fixes = [
            (f"f{i}.jpg", core.GpsFix(i, datetime(2026, 1, 1), 41.0 + i * 0.001, -75.0, 12.0, 470.0))
            for i in range(10)
        ]
        warnings = core.detect_hovering(fixes, threshold_m=0.5, min_run=5)
        assert len(warnings) == 0


# ---------------------------------------------------------------------------
# Tests: geo.txt writing
# ---------------------------------------------------------------------------

class TestGeoTxt:
    def test_format(self, tmp_path: Path):
        frames = [
            ("img_001.jpg", core.GpsFix(0, datetime(2026, 1, 1), 41.433028, -75.484483, 12.4, 471.9)),
            ("img_002.jpg", core.GpsFix(1, datetime(2026, 1, 1), 41.433029, -75.484484, 12.5, 472.0)),
        ]
        geo = core.write_geo_txt(tmp_path, frames)

        lines = geo.read_text().strip().split("\n")
        assert lines[0] == "EPSG:4326"
        assert lines[1].startswith("img_001.jpg")
        parts = lines[1].split()
        assert len(parts) == 4
        # lon first, lat second
        assert float(parts[1]) == pytest.approx(-75.484483, abs=1e-6)
        assert float(parts[2]) == pytest.approx(41.433028, abs=1e-6)
        assert float(parts[3]) == pytest.approx(12.4, abs=0.01)


# ---------------------------------------------------------------------------
# Tests: DJI filename regex
# ---------------------------------------------------------------------------

class TestDjiRegex:
    def test_matches_mp4(self):
        m = core.DJI_RE.match("DJI_20260525133456_0005_D.MP4")
        assert m is not None
        assert m.group(1) == "DJI_20260525133456_0005_D"
        assert m.group(3) == "0005"

    def test_matches_srt(self):
        m = core.DJI_RE.match("DJI_20260525133456_0005_D.SRT")
        assert m is not None

    def test_matches_lrf(self):
        m = core.DJI_RE.match("DJI_20260525133456_0005_D.LRF")
        assert m is not None

    def test_rejects_non_dji(self):
        assert core.DJI_RE.match("random_video.mp4") is None

    def test_case_insensitive(self):
        m = core.DJI_RE.match("DJI_20260525133456_0005_D.mp4")
        assert m is not None


# Need pytest for approx
import pytest
