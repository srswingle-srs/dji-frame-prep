"""
DJI Frame Prep -- GUI
PyQt6 single-window application. Entry point.
All logic delegated to core.py.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from PyQt6.QtCore import (
        QThread, pyqtSignal, pyqtSlot, Qt, QUrl, QSettings,
    )
    from PyQt6.QtGui import QDesktopServices, QShortcut, QKeySequence
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QSplitter, QPushButton, QLabel, QTreeWidget, QTreeWidgetItem,
        QFileDialog, QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox,
        QProgressBar, QTextEdit, QGroupBox, QMessageBox, QLineEdit,
        QToolBox, QSizePolicy, QDialog, QDialogButtonBox,
    )
except ImportError:
    print(f"\nERROR: PyQt6 is not installed for this Python ({sys.executable}).")
    print(f"Fix: run this command:\n  \"{sys.executable}\" -m pip install PyQt6\n")
    input("Press Enter to close...")
    sys.exit(1)

import core

# ---------------------------------------------------------------------------
# Unit conversion
# ---------------------------------------------------------------------------

M_PER_FT = 0.3048
FT_PER_M = 1.0 / M_PER_FT


def m_to_ft(m: float) -> float:
    return m * FT_PER_M


def ft_to_m(ft: float) -> float:
    return ft * M_PER_FT


# ---------------------------------------------------------------------------
# First-run walkthrough dialog
# ---------------------------------------------------------------------------

class WalkthroughDialog(QDialog):
    """4-step first-run guide shown once on first launch."""

    STEPS = [
        (
            "Step 1: Select Your Drone Folder",
            "Click 'Select DJI Folder' and pick the folder where your\n"
            "DJI drone saved its video (.MP4) and subtitle (.SRT) files.\n\n"
            "The tool will automatically find and match all your flight clips.",
        ),
        (
            "Step 2: Review Your Flights",
            "The tool groups your clips into flights and shows them in a tree.\n"
            "Each clip shows its duration, GPS data point count, and status.\n\n"
            "Uncheck any clips you want to skip (junk clips are auto-excluded).",
        ),
        (
            "Step 3: Check Your Settings",
            "The tool auto-detects your flight height and sets good defaults.\n\n"
            "Flight Pattern: Crosshatch (two-pass) or Lawnmower (single-pass)\n"
            "Altitude Filter: Removes frames from takeoff/landing/transit\n"
            "Frame Interval: How often to grab a frame (2 sec is a good start)",
        ),
        (
            "Step 4: Click Run",
            "Hit 'Preview' first to see how many frames you'll get.\n"
            "Then hit 'Run' to extract frames and generate the geo.txt file.\n\n"
            "When done, use 'Open Output Folder' to find your frames,\n"
            "then drag that folder into WebODM to start processing.",
        ),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Quick Start Guide")
        self.setMinimumWidth(450)
        self._step = 0

        layout = QVBoxLayout(self)

        self._title = QLabel()
        self._title.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self._title)

        self._body = QLabel()
        self._body.setWordWrap(True)
        self._body.setStyleSheet("font-size: 12px; margin: 10px 0;")
        layout.addWidget(self._body)

        self._step_label = QLabel()
        self._step_label.setStyleSheet("color: gray;")
        layout.addWidget(self._step_label)

        btn_layout = QHBoxLayout()
        self._btn_back = QPushButton("Back")
        self._btn_back.clicked.connect(self._go_back)
        btn_layout.addWidget(self._btn_back)

        self._dont_show = QCheckBox("Don't show this again")
        self._dont_show.setChecked(True)
        btn_layout.addWidget(self._dont_show)

        self._btn_next = QPushButton("Next")
        self._btn_next.clicked.connect(self._go_next)
        btn_layout.addWidget(self._btn_next)
        layout.addLayout(btn_layout)

        self._show_step()

    def _show_step(self):
        title, body = self.STEPS[self._step]
        self._title.setText(title)
        self._body.setText(body)
        self._step_label.setText(f"{self._step + 1} of {len(self.STEPS)}")
        self._btn_back.setEnabled(self._step > 0)
        self._btn_next.setText("Got it!" if self._step == len(self.STEPS) - 1 else "Next")

    def _go_back(self):
        if self._step > 0:
            self._step -= 1
            self._show_step()

    def _go_next(self):
        if self._step < len(self.STEPS) - 1:
            self._step += 1
            self._show_step()
        else:
            self.accept()

    def suppress_future(self) -> bool:
        return self._dont_show.isChecked()


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class ExtractionWorker(QThread):
    """Runs the extraction pipeline on a background thread."""
    progress = pyqtSignal(str)
    group_done = pyqtSignal(object)  # GroupResult
    all_done = pyqtSignal(list)      # list[GroupResult]
    error = pyqtSignal(str)

    def __init__(
        self,
        groups: list[core.FlightGroup],
        output_base: Path,
        source_folder: Path,
        interval_s: float,
        jpeg_quality: int,
        filter_enabled: bool,
        mapping_height: float,
        band: float,
        use_abs: bool,
        pattern: str,
    ):
        super().__init__()
        self.groups = groups
        self.output_base = output_base
        self.source_folder = source_folder
        self.interval_s = interval_s
        self.jpeg_quality = jpeg_quality
        self.filter_enabled = filter_enabled
        self.mapping_height = mapping_height
        self.band = band
        self.use_abs = use_abs
        self.pattern = pattern
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _check_cancel(self) -> bool:
        return self._cancelled

    def _progress(self, msg: str):
        self.progress.emit(msg)

    def run(self):
        import traceback
        results: list[core.GroupResult] = []

        try:
            self._progress(f"Worker started. Output: {self.output_base}")
            self._progress(f"Groups to process: {len(self.groups)}, interval={self.interval_s}s")

            for group in self.groups:
                if self._cancelled:
                    break

                self._progress(f"Processing flight {group.group_id} ({len(group.segments)} segments)...")
                sub_groups = core.split_group_on_exclusions(group)
                self._progress(f"Split into {len(sub_groups)} sub-group(s)")

                for si, sg in enumerate(sub_groups):
                    if self._cancelled:
                        break
                    self._progress(f"Running extraction for sub-group {si + 1}/{len(sub_groups)}...")
                    result = core.run_pipeline_for_group(
                        sg, si, self.output_base,
                        self.interval_s, self.jpeg_quality,
                        self.filter_enabled, self.mapping_height, self.band,
                        self.use_abs,
                        progress_cb=self._progress,
                        cancel_check=self._check_cancel,
                    )
                    results.append(result)
                    self.group_done.emit(result)

            # Write manifest for successful groups
            if not self._cancelled:
                total_ext = sum(r.frames_extracted for r in results if r.status == "success")
                total_kept = sum(r.frames_kept for r in results if r.status == "success")
                total_rej = sum(r.frames_rejected for r in results if r.status == "success")
                alt_stats = core.compute_altitude_stats(self.groups, self.use_abs)

                settings = {
                    "pattern": self.pattern,
                    "interval_s": self.interval_s,
                    "jpeg_quality": self.jpeg_quality,
                    "filter_enabled": self.filter_enabled,
                    "mapping_height": self.mapping_height,
                    "altitude_band": self.band,
                    "altitude_source": "abs_alt" if self.use_abs else "rel_alt",
                }

                try:
                    core.write_manifest(
                        self.output_base, self.source_folder, self.groups,
                        settings, total_ext, total_kept, total_rej, alt_stats,
                    )
                except Exception as e:
                    self._progress(f"Warning: could not write manifest: {e}")

        except Exception as e:
            tb = traceback.format_exc()
            self._progress(f"ERROR: {e}\n{tb}")
            self.error.emit(f"{e}\n{tb}")

        self.all_done.emit(results)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DJI Frame Prep")
        self.setMinimumSize(900, 650)

        self._groups: list[core.FlightGroup] = []
        self._source_folder: Optional[Path] = None
        self._worker: Optional[ExtractionWorker] = None
        self._ffmpeg_ok = False
        self._ffprobe_ok = False
        self._use_imperial = False
        self._settings = QSettings("DJI_Frame_Prep", "DJI_Frame_Prep")

        self._build_ui()
        self._check_ffmpeg()
        self._maybe_show_walkthrough()

    # -----------------------------------------------------------------------
    # Unit helpers
    # -----------------------------------------------------------------------

    def _unit_label(self) -> str:
        return "ft" if self._use_imperial else "m"

    def _display_alt(self, meters: float) -> float:
        return m_to_ft(meters) if self._use_imperial else meters

    def _storage_alt(self, display_val: float) -> float:
        return ft_to_m(display_val) if self._use_imperial else display_val

    def _update_unit_labels(self):
        u = self._unit_label()
        self._height_label.setText(f"Height ({u}):")
        self._band_label.setText(f"Altitude Band (+/- {u}):")

        # Re-display altitude stats if we have groups
        if self._groups:
            stats = core.compute_altitude_stats(self._groups, self._use_abs())
            if stats:
                self._show_alt_stats(stats)

    def _show_alt_stats(self, stats: core.AltitudeStats):
        u = self._unit_label()
        self._alt_stats_label.setText(
            f"Min: {self._display_alt(stats.min_alt):.1f}  "
            f"Max: {self._display_alt(stats.max_alt):.1f}  "
            f"Mean: {self._display_alt(stats.mean_alt):.1f}  "
            f"Median: {self._display_alt(stats.median_alt):.1f} {u}"
        )

    # -----------------------------------------------------------------------
    # First-run walkthrough
    # -----------------------------------------------------------------------

    def _maybe_show_walkthrough(self):
        if self._settings.value("walkthrough_done", False, type=bool):
            return
        dlg = WalkthroughDialog(self)
        dlg.exec()
        if dlg.suppress_future():
            self._settings.setValue("walkthrough_done", True)

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # --- Left panel: folder + scan results ---
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)

        # Folder picker
        folder_row = QHBoxLayout()
        self._folder_label = QLineEdit()
        self._folder_label.setReadOnly(True)
        self._folder_label.setPlaceholderText("No folder selected")
        self._folder_label.setToolTip(
            "The folder where your drone saved its flight videos.\n"
            "Should contain .MP4 video files and matching .SRT GPS files."
        )
        btn_folder = QPushButton("Select DJI Folder")
        btn_folder.setToolTip("Pick the folder containing your drone flight videos")
        btn_folder.clicked.connect(self._on_select_folder)
        folder_row.addWidget(self._folder_label, 1)
        folder_row.addWidget(btn_folder)
        left_layout.addLayout(folder_row)

        # Info summary
        self._info_label = QLabel("")
        self._info_label.setWordWrap(True)
        left_layout.addWidget(self._info_label)

        # Scan results tree
        self._tree = QTreeWidget()
        self._tree.setToolTip(
            "Your detected flights and video clips.\n"
            "Uncheck any clip to skip it.\n"
            "Short junk clips (under 5 seconds) are auto-skipped."
        )
        self._tree.setHeaderLabels(["Segment / Flight", "Duration", "GPS Points", "Status"])
        self._tree.setColumnWidth(0, 280)
        self._tree.setColumnWidth(1, 70)
        self._tree.setColumnWidth(2, 90)
        self._tree.itemChanged.connect(self._on_tree_item_changed)
        left_layout.addWidget(self._tree, 1)

        splitter.addWidget(left)

        # --- Right panel: settings + run ---
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 4, 4, 4)

        # Basic settings
        basic_box = QGroupBox("Settings")
        basic_layout = QVBoxLayout(basic_box)

        # Pattern selector
        pat_row = QHBoxLayout()
        pat_row.addWidget(QLabel("Flight Pattern:"))
        self._pattern_combo = QComboBox()
        self._pattern_combo.setToolTip(
            "How was this flight flown?\n\n"
            "Crosshatch: Two passes over the area in a grid pattern.\n"
            "  Grabs a frame every 2 seconds (overlapping coverage).\n\n"
            "Lawnmower: Single-direction passes in rows.\n"
            "  Grabs a frame every 1.2 seconds (needs more frames)."
        )
        self._pattern_combo.addItems(["Crosshatch", "Lawnmower"])
        self._pattern_combo.currentTextChanged.connect(self._on_pattern_changed)
        pat_row.addWidget(self._pattern_combo)
        basic_layout.addLayout(pat_row)

        # Output folder
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output Folder:"))
        self._output_edit = QLineEdit()
        self._output_edit.setToolTip(
            "Where your extracted frames and GPS file will be saved.\n"
            "Defaults to a 'frames_out' folder next to your videos."
        )
        btn_output = QPushButton("Browse")
        btn_output.setToolTip("Choose a different save location")
        btn_output.clicked.connect(self._on_browse_output)
        out_row.addWidget(self._output_edit, 1)
        out_row.addWidget(btn_output)
        basic_layout.addLayout(out_row)

        # Mapping height with stats
        alt_box = QGroupBox("Mapping Height")
        alt_layout = QVBoxLayout(alt_box)

        height_row = QHBoxLayout()
        self._height_label = QLabel("Height (m):")
        height_row.addWidget(self._height_label)
        self._mapping_height = QDoubleSpinBox()
        self._mapping_height.setRange(0, 500)
        self._mapping_height.setDecimals(1)
        self._mapping_height.setValue(12.0)
        self._mapping_height.setToolTip(
            "The height the drone flew at during its mapping passes.\n"
            "Auto-detected from your GPS data.\n"
            "Adjust if you know the planned flight height was different."
        )
        height_row.addWidget(self._mapping_height)
        self._filter_check = QCheckBox("Altitude filter")
        self._filter_check.setChecked(True)
        self._filter_check.setToolTip(
            "Removes frames taken during takeoff, landing, and transit.\n"
            "Only keeps frames near the mapping height.\n"
            "Rejected frames are moved to a 'rejected' folder, not deleted."
        )
        height_row.addWidget(self._filter_check)
        alt_layout.addLayout(height_row)

        # Metric / Imperial toggle
        unit_row = QHBoxLayout()
        self._unit_toggle = QCheckBox("Show in feet")
        self._unit_toggle.setToolTip(
            "Switch height display between meters and feet.\n"
            "All output files always use meters (WebODM standard)."
        )
        self._unit_toggle.toggled.connect(self._on_unit_toggle)
        unit_row.addWidget(self._unit_toggle)
        alt_layout.addLayout(unit_row)

        self._alt_stats_label = QLabel("Min / Max / Mean: --")
        self._alt_stats_label.setStyleSheet("color: gray; font-size: 11px;")
        alt_layout.addWidget(self._alt_stats_label)
        basic_layout.addWidget(alt_box)

        right_layout.addWidget(basic_box)

        # Advanced settings (collapsed)
        self._adv_toolbox = QToolBox()
        adv_widget = QWidget()
        adv_layout = QVBoxLayout(adv_widget)

        # Frame interval
        int_row = QHBoxLayout()
        int_row.addWidget(QLabel("Frame Interval (sec):"))
        self._interval_spin = QDoubleSpinBox()
        self._interval_spin.setRange(0.1, 30.0)
        self._interval_spin.setDecimals(1)
        self._interval_spin.setSingleStep(0.1)
        self._interval_spin.setValue(core.DEFAULT_INTERVALS["Crosshatch"])
        self._interval_spin.setToolTip(
            "How many seconds between each extracted frame.\n\n"
            "Smaller number = more frames, better coverage, bigger output.\n"
            "Larger number = fewer frames, faster processing in WebODM.\n\n"
            "Auto-set when you pick a flight pattern, but you can change it."
        )
        int_row.addWidget(self._interval_spin)
        adv_layout.addLayout(int_row)

        # Altitude band
        band_row = QHBoxLayout()
        self._band_label = QLabel("Altitude Band (+/- m):")
        band_row.addWidget(self._band_label)
        self._band_spin = QDoubleSpinBox()
        self._band_spin.setRange(0.5, 100.0)
        self._band_spin.setDecimals(1)
        self._band_spin.setValue(5.0)
        self._band_spin.setToolTip(
            "How far above or below the mapping height to keep.\n\n"
            "Example: if height = 40 and band = 16,\n"
            "  only frames between 24 and 56 are kept.\n\n"
            "Frames outside this range are moved to a 'rejected' folder."
        )
        band_row.addWidget(self._band_spin)
        adv_layout.addLayout(band_row)

        # Altitude source
        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Altitude Source:"))
        self._alt_abs_check = QCheckBox("Use height above sea level")
        self._alt_abs_check.setToolTip(
            "Which height measurement to use:\n\n"
            "OFF (default): Height above takeoff point.\n"
            "  Best for WebODM in most cases.\n\n"
            "ON: Height above sea level.\n"
            "  Only use if your workflow specifically requires it."
        )
        src_row.addWidget(self._alt_abs_check)
        adv_layout.addLayout(src_row)

        # JPEG quality
        q_row = QHBoxLayout()
        q_row.addWidget(QLabel("Image Quality (1-31):"))
        self._quality_spin = QSpinBox()
        self._quality_spin.setRange(1, 31)
        self._quality_spin.setValue(2)
        self._quality_spin.setToolTip(
            "Quality of the extracted frame images.\n\n"
            "2 = very good (recommended -- best for 3D reconstruction)\n"
            "5 = good (smaller files, still works well)\n"
            "10+ = low quality (not recommended for mapping)\n"
            "1 = maximum quality (overkill, huge files)"
        )
        q_row.addWidget(self._quality_spin)
        adv_layout.addLayout(q_row)

        self._adv_toolbox.addItem(adv_widget, "Advanced Settings")
        right_layout.addWidget(self._adv_toolbox)

        # Preview / Run / Cancel buttons
        btn_layout = QHBoxLayout()
        self._btn_preview = QPushButton("Preview")
        self._btn_preview.setToolTip(
            "See how many frames you'll get and estimated file size\n"
            "before running the full extraction. No files are created."
        )
        self._btn_preview.clicked.connect(self._on_preview)
        self._btn_preview.setEnabled(False)
        btn_layout.addWidget(self._btn_preview)

        self._btn_run = QPushButton("Run")
        self._btn_run.setToolTip(
            "Start extracting frames from your video.\n"
            "This may take several minutes for long flights."
        )
        self._btn_run.clicked.connect(self._on_run)
        self._btn_run.setEnabled(False)
        btn_layout.addWidget(self._btn_run)

        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setToolTip("Stop the current extraction. Frames already saved are kept.")
        self._btn_cancel.clicked.connect(self._on_cancel)
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.setVisible(False)
        btn_layout.addWidget(self._btn_cancel)
        right_layout.addLayout(btn_layout)

        # Enter shortcut for Run
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Return), self)
        shortcut.activated.connect(self._on_enter_pressed)

        # Progress
        self._progress_bar = QProgressBar()
        self._progress_bar.setVisible(False)
        right_layout.addWidget(self._progress_bar)

        # Log / warnings area
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(200)
        self._log.setToolTip(
            "Status messages and warnings appear here.\n"
            "Check this area if something doesn't look right."
        )
        right_layout.addWidget(self._log, 1)

        # Post-run buttons
        post_row = QHBoxLayout()
        self._btn_open_folder = QPushButton("Open Output Folder")
        self._btn_open_folder.setToolTip("Open the folder with your extracted frames")
        self._btn_open_folder.clicked.connect(self._on_open_folder)
        self._btn_open_folder.setVisible(False)
        post_row.addWidget(self._btn_open_folder)

        self._btn_copy_path = QPushButton("Copy Output Path")
        self._btn_copy_path.setToolTip("Copy the folder path so you can paste it into WebODM")
        self._btn_copy_path.clicked.connect(self._on_copy_path)
        self._btn_copy_path.setVisible(False)
        post_row.addWidget(self._btn_copy_path)
        right_layout.addLayout(post_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

    # -----------------------------------------------------------------------
    # ffmpeg check
    # -----------------------------------------------------------------------

    def _check_ffmpeg(self):
        self._ffmpeg_ok, self._ffprobe_ok = core.check_ffmpeg()
        if not self._ffmpeg_ok or not self._ffprobe_ok:
            missing = []
            if not self._ffmpeg_ok:
                missing.append("ffmpeg")
            if not self._ffprobe_ok:
                missing.append("ffprobe")
            QMessageBox.critical(
                self, "Missing Software",
                f"Could not find: {', '.join(missing)}\n\n"
                "These are needed to extract frames from your drone videos.\n\n"
                "To fix: close this app, run the run.bat file instead --\n"
                "it will install everything automatically.",
            )
            self._log_msg(f"ERROR: {', '.join(missing)} not found. Run button disabled.")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _log_msg(self, msg: str):
        self._log.append(msg)

    def _has_selected_segments(self) -> bool:
        for g in self._groups:
            for seg in g.segments:
                if seg.included:
                    return True
        return False

    def _update_run_button(self):
        enabled = (
            self._ffmpeg_ok and self._ffprobe_ok
            and self._has_selected_segments()
            and self._worker is None
        )
        self._btn_run.setEnabled(enabled)
        self._btn_preview.setEnabled(self._has_selected_segments())

    def _use_abs(self) -> bool:
        return self._alt_abs_check.isChecked()

    # -----------------------------------------------------------------------
    # Unit toggle
    # -----------------------------------------------------------------------

    @pyqtSlot(bool)
    def _on_unit_toggle(self, checked: bool):
        # Convert current values before switching
        old_height_m = self._storage_alt(self._mapping_height.value())
        old_band_m = self._storage_alt(self._band_spin.value())

        self._use_imperial = checked
        self._update_unit_labels()

        # Update spin box ranges and values in new units
        if self._use_imperial:
            self._mapping_height.setRange(0, 1640)  # ~500m
            self._band_spin.setRange(1.0, 330.0)    # ~100m
        else:
            self._mapping_height.setRange(0, 500)
            self._band_spin.setRange(0.5, 100.0)

        self._mapping_height.setValue(round(self._display_alt(old_height_m), 1))
        self._band_spin.setValue(round(self._display_alt(old_band_m), 1))

    # -----------------------------------------------------------------------
    # Folder selection & scanning
    # -----------------------------------------------------------------------

    @pyqtSlot()
    def _on_select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select DJI Video Folder")
        if not folder:
            return
        self._source_folder = Path(folder)
        self._folder_label.setText(folder)
        self._output_edit.setText(str(self._source_folder / "frames_out"))
        self._scan_folder()

    def _scan_folder(self):
        self._log.clear()
        self._tree.clear()
        self._groups = []
        self._log_msg(f"Scanning {self._source_folder}...")

        try:
            segments, warnings = core.scan_folder(self._source_folder)
        except Exception as e:
            self._log_msg(f"ERROR: Could not scan folder: {e}")
            QMessageBox.warning(
                self, "Scan Failed",
                f"Could not read the folder:\n{e}\n\n"
                "Make sure the folder exists and contains DJI video files.",
            )
            return

        for w in warnings:
            self._log_msg(f"WARNING: {w}")

        if not segments:
            self._info_label.setText("No usable drone clips found in this folder.")
            self._log_msg(
                "No DJI video+GPS file pairs found.\n"
                "Make sure the folder has both .MP4 and matching .SRT files\n"
                "with DJI naming format (e.g. DJI_20260525133456_0005_D.MP4)."
            )
            self._update_run_button()
            return

        self._groups = core.group_flights(segments)
        self._populate_tree()
        self._log_msg(f"Found {len(segments)} clips in {len(self._groups)} flight(s).")

        # Altitude stats
        stats = core.compute_altitude_stats(self._groups, self._use_abs())
        if stats:
            self._mapping_height.setValue(round(self._display_alt(stats.median_alt), 1))
            self._show_alt_stats(stats)

        # Summary
        total_segs = sum(len(g.segments) for g in self._groups)
        usable = sum(1 for g in self._groups for s in g.segments if s.included)
        junk = total_segs - usable
        info = f"{len(self._groups)} flight(s), {usable} usable clip(s)"
        if junk:
            info += f", {junk} short clip(s) auto-skipped"
        self._info_label.setText(info)

        for g in self._groups:
            for w in g.warnings:
                self._log_msg(f"WARNING (Flight {g.group_id}): {w}")

        self._update_run_button()

    def _populate_tree(self):
        self._tree.blockSignals(True)
        self._tree.clear()

        for g in self._groups:
            total_dur = sum(s.duration_s for s in g.segments)
            group_item = QTreeWidgetItem([
                f"Flight {g.group_id}",
                f"{total_dur:.0f}s",
                "",
                f"{len(g.segments)} clips",
            ])
            group_item.setFlags(group_item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)

            for seg in g.segments:
                seg_item = QTreeWidgetItem([
                    seg.basename,
                    f"{seg.duration_s:.0f}s",
                    f"{len(seg.fixes)}",
                    ", ".join(seg.warnings) if seg.warnings else "OK",
                ])
                seg_item.setFlags(seg_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                seg_item.setCheckState(
                    0,
                    Qt.CheckState.Checked if seg.included else Qt.CheckState.Unchecked,
                )
                seg_item.setData(0, Qt.ItemDataRole.UserRole, seg)
                group_item.addChild(seg_item)

            self._tree.addTopLevelItem(group_item)
            group_item.setExpanded(True)

        self._tree.blockSignals(False)

    # -----------------------------------------------------------------------
    # Tree checkbox changes
    # -----------------------------------------------------------------------

    @pyqtSlot(QTreeWidgetItem, int)
    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int):
        seg = item.data(0, Qt.ItemDataRole.UserRole)
        if seg is None:
            return
        seg.included = item.checkState(0) == Qt.CheckState.Checked
        self._update_run_button()

    # -----------------------------------------------------------------------
    # Pattern selector
    # -----------------------------------------------------------------------

    @pyqtSlot(str)
    def _on_pattern_changed(self, pattern: str):
        if pattern in core.DEFAULT_INTERVALS:
            self._interval_spin.setValue(core.DEFAULT_INTERVALS[pattern])

    # -----------------------------------------------------------------------
    # Output folder browse
    # -----------------------------------------------------------------------

    @pyqtSlot()
    def _on_browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self._output_edit.setText(folder)

    # -----------------------------------------------------------------------
    # Preview
    # -----------------------------------------------------------------------

    @pyqtSlot()
    def _on_preview(self):
        if not self._groups:
            return

        interval = self._interval_spin.value()
        filter_on = self._filter_check.isChecked()
        height = self._storage_alt(self._mapping_height.value())
        band = self._storage_alt(self._band_spin.value())
        use_abs = self._use_abs()

        preview = core.preview_extraction(
            self._groups, interval, filter_on, height, band, use_abs,
        )

        u = self._unit_label()
        lines = [
            "--- Preview ---",
            f"Estimated frames: {preview.total_frames}",
            f"Estimated size: {preview.estimated_size_mb:.0f} MB",
        ]
        for basename, count in preview.per_segment:
            lines.append(f"  {basename}: ~{count} frames")

        if filter_on:
            lines.append(f"After altitude filter: ~{preview.kept_after_filter} kept, "
                         f"~{preview.rejected_by_filter} removed")

        if preview.total_frames > 2000:
            lines.append("NOTE: Over 2000 frames may slow down WebODM. Try a longer interval.")

        self._log_msg("\n".join(lines))

    # -----------------------------------------------------------------------
    # Enter key shortcut
    # -----------------------------------------------------------------------

    @pyqtSlot()
    def _on_enter_pressed(self):
        if self._btn_run.isEnabled():
            self._on_run()

    # -----------------------------------------------------------------------
    # Run
    # -----------------------------------------------------------------------

    @pyqtSlot()
    def _on_run(self):
        if not self._groups or self._worker is not None:
            return

        output_path = Path(self._output_edit.text().strip())
        if not output_path.parent.exists():
            QMessageBox.warning(
                self, "Bad Save Location",
                "The folder you chose doesn't exist.\n"
                "Pick a different output folder.",
            )
            return

        # Check if output exists and is not empty
        if output_path.exists() and any(output_path.iterdir()):
            reply = QMessageBox.question(
                self, "Folder Not Empty",
                f"'{output_path.name}' already has files in it.\n"
                "Replace them with new frames?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        output_path.mkdir(parents=True, exist_ok=True)

        interval = self._interval_spin.value()
        quality = self._quality_spin.value()
        filter_on = self._filter_check.isChecked()
        height = self._storage_alt(self._mapping_height.value())
        band = self._storage_alt(self._band_spin.value())
        use_abs = self._use_abs()
        pattern = self._pattern_combo.currentText()

        # Disk space check
        preview = core.preview_extraction(
            self._groups, interval, filter_on, height, band, use_abs,
        )
        space_warning = core.check_disk_space(output_path, preview.estimated_size_mb)
        if space_warning:
            reply = QMessageBox.warning(
                self, "Low Disk Space",
                f"You might not have enough room:\n{space_warning}\n\nContinue anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Record pattern note
        try:
            (output_path / "flight_pattern.txt").write_text(
                f"Flight pattern: {pattern}\n", encoding="utf-8"
            )
        except OSError:
            pass

        # UI state
        self._btn_run.setEnabled(False)
        self._btn_cancel.setVisible(True)
        self._btn_cancel.setEnabled(True)
        self._btn_preview.setEnabled(False)
        self._btn_open_folder.setVisible(False)
        self._btn_copy_path.setVisible(False)
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)  # indeterminate
        self._log_msg("--- Starting extraction ---")
        self._log_msg(f"Saving to: {output_path}")
        u = self._unit_label()
        disp_h = self._mapping_height.value()
        disp_b = self._band_spin.value()
        self._log_msg(f"Settings: interval={interval}s, quality={quality}, "
                       f"filter={'ON' if filter_on else 'OFF'}, "
                       f"height={disp_h:.1f}{u}, band=+/-{disp_b:.1f}{u}")

        self._worker = ExtractionWorker(
            self._groups, output_path, self._source_folder,
            interval, quality, filter_on, height, band, use_abs, pattern,
        )
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.group_done.connect(self._on_group_done)
        self._worker.all_done.connect(self._on_all_done)
        self._worker.error.connect(self._on_worker_error)
        self._worker.start()

    # -----------------------------------------------------------------------
    # Cancel
    # -----------------------------------------------------------------------

    @pyqtSlot()
    def _on_cancel(self):
        if self._worker:
            self._log_msg("Cancelling... (waiting for current frame to finish)")
            self._worker.cancel()

    # -----------------------------------------------------------------------
    # Worker signals
    # -----------------------------------------------------------------------

    @pyqtSlot(str)
    def _on_worker_progress(self, msg: str):
        self._log_msg(msg)
        # Also write to log file for debugging
        try:
            log_path = Path(__file__).parent / "error_log.txt"
            with open(str(log_path), "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        except OSError:
            pass

    @pyqtSlot(str)
    def _on_worker_error(self, msg: str):
        self._log_msg(f"ERROR: {msg}")
        QMessageBox.critical(
            self, "Extraction Error",
            f"Something went wrong during extraction:\n\n{msg}\n\n"
            "Check the log area for details.",
        )

    @pyqtSlot(object)
    def _on_group_done(self, result: core.GroupResult):
        status_str = result.status.upper()
        part = f" part {result.sub_group_index + 1}" if result.sub_group_index > 0 else ""
        self._log_msg(
            f"Flight {result.group_id}{part}: {status_str}"
            + (f" -- {result.error}" if result.error else "")
            + f" | {result.frames_extracted} extracted"
            + f", {result.frames_kept} kept, {result.frames_rejected} filtered out"
        )
        for w in result.warnings:
            self._log_msg(f"  WARNING: {w}")

    @pyqtSlot(list)
    def _on_all_done(self, results: list[core.GroupResult]):
        self._worker = None
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.setVisible(False)
        self._progress_bar.setVisible(False)
        self._update_run_button()

        success = [r for r in results if r.status == "success"]
        failed = [r for r in results if r.status == "failed"]
        cancelled = [r for r in results if r.status == "cancelled"]

        total_kept = sum(r.frames_kept for r in success)
        total_rej = sum(r.frames_rejected for r in success)

        summary = ["--- Run Complete ---"]
        summary.append(f"{len(success)} succeeded, {len(failed)} failed, {len(cancelled)} cancelled")
        if success:
            summary.append(f"Frames ready for WebODM: {total_kept} ({total_rej} filtered out)")
            if success[0].geo_txt_path:
                summary.append(f"Output: {success[0].geo_txt_path.parent}")
                summary.append("Drag this folder into WebODM to start processing.")

        for line in summary:
            self._log_msg(line)

        if failed:
            fail_msgs = []
            for r in failed:
                fail_msgs.append(f"Flight {r.group_id}: {r.error}")
            QMessageBox.warning(
                self, "Some Flights Failed",
                "The following flights had errors:\n\n" + "\n".join(fail_msgs),
            )

        # Show post-run buttons
        if success:
            self._btn_open_folder.setVisible(True)
            self._btn_copy_path.setVisible(True)

    # -----------------------------------------------------------------------
    # Post-run actions
    # -----------------------------------------------------------------------

    @pyqtSlot()
    def _on_open_folder(self):
        path = self._output_edit.text().strip()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @pyqtSlot()
    def _on_copy_path(self):
        path = self._output_edit.text().strip()
        if path:
            QApplication.clipboard().setText(path)
            self._log_msg("Output path copied to clipboard.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    import logging
    log_path = Path(__file__).parent / "error_log.txt"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # Also mirror stderr to the log file
    sys.stderr = open(str(log_path), "a", encoding="utf-8")

    try:
        main()
    except Exception as e:
        import traceback
        msg = traceback.format_exc()
        logging.critical(msg)
        print(f"\nFATAL ERROR: {e}\n")
        print(msg)
        print(f"Full log saved to: {log_path}")
        input("\nPress Enter to close...")
