"""Offline NeuroMotion processing and explicit quality-gated exports."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

import numpy as np
import pandas as pd

from .signals import SignalSession, load_signal_sessions
from .timeline import Alignment, AlignmentError, fit_session_alignment, iter_edges, iter_matched_edges
from .video import Rect, VideoTrace, TrackingData, scan_video_led, track_video_markers
from .qc import save_alignment_plot


@dataclass(frozen=True)
class RunConfig:
    emg_csv: Path
    video_path: Path
    output_dir: Path
    led_roi: Rect
    sync_only: bool = False
    marker_roi: Rect | None = None
    marker_count: int = 1
    intrinsics_path: Path | None = None
    homography_path: Path | None = None
    nominal_rate_hz: float = 1000.0
    led_metric: str = "p99"
    led_threshold: float | None = None
    led_threshold_strategy: str = "otsu_high_margin"
    marker_threshold: int = 200
    marker_min_area: float = 5.0
    marker_max_area: float = 6000.0
    marker_min_circularity: float = 0.5
    marker_search_range_px: float = 50.0
    marker_memory_frames: int = 5
    max_emg_gap_samples: int = 1
    max_motion_gap_s: float = 0.1
    max_intrinsics_rms_px: float = 2.0
    max_homography_rms_mm: float = 1.0
    allow_constant_fps_fallback: bool = False
    video_analysis_dir: Path | None = None
    max_alignment_residual_frames: float = 1.0

    def validate(self) -> None:
        if not self.emg_csv.is_file() or not self.video_path.is_file():
            raise ValueError("EMG CSV and video must exist")
        if self.nominal_rate_hz <= 0 or self.max_motion_gap_s <= 0:
            raise ValueError("Sample rate and motion interpolation gap must be positive")
        if self.marker_count < 1 or self.max_emg_gap_samples < 1:
            raise ValueError("Marker count and maximum EMG gap must be positive")
        if self.max_intrinsics_rms_px <= 0 or self.max_homography_rms_mm <= 0:
            raise ValueError("Calibration quality thresholds must be positive")
        numeric = (self.nominal_rate_hz, self.max_motion_gap_s, self.max_intrinsics_rms_px,
                   self.max_homography_rms_mm, self.marker_min_area, self.marker_max_area,
                   self.marker_min_circularity, self.marker_search_range_px)
        if not np.isfinite(numeric).all():
            raise ValueError("Configuration values must be finite")
        if not 0 <= self.marker_threshold <= 255 or (self.led_threshold is not None and not (np.isfinite(self.led_threshold) and 0 <= self.led_threshold <= 255)):
            raise ValueError("Brightness thresholds must be between 0 and 255")
        if not 0 < self.marker_min_area <= self.marker_max_area or not 0 < self.marker_min_circularity <= 1 or self.marker_search_range_px <= 0 or self.marker_memory_frames < 0:
            raise ValueError("Marker detection/tracking parameters are invalid")
        if self.video_analysis_dir is not None and not (self.video_analysis_dir / "video_analysis_report.json").is_file():
            raise ValueError("Video analysis report does not exist")
        if not 0 < self.max_alignment_residual_frames <= 1:
            raise ValueError("Formal alignment residual limit must be within one frame")
        if not self.sync_only and self.video_analysis_dir is None:
            if self.marker_roi is None or self.intrinsics_path is None or self.homography_path is None:
                raise ValueError("Full fusion needs marker ROI, intrinsics and homography")
            if not self.intrinsics_path.is_file() or not self.homography_path.is_file():
                raise ValueError("Calibration files do not exist")


def _file_info(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path.resolve()), "size_bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _write_quality_details(output: Path, trace: VideoTrace, session: SignalSession, alignment: Alignment, rate: float) -> dict:
    stream, writer = _csv_writer(output / "ttl_edge_matches.csv", ["edge_number", "video_time_s", "emg_sample_index", "rising", "common_time_s", "residual_s"])
    with stream:
        writer.writerows(iter_matched_edges(trace.led_state, trace.time_s, session.sync, session.sample_index, alignment, rate))
    summary = {"emg_missing_index_samples": session.missing_index_samples, "video_gap_count": 0,
               "firmware_dropped_counter_max": int(session.dropped_samples.max()),
               "pc_missing_counter_max": int(session.pc_missing_samples.max())}
    stream, writer = _csv_writer(output / "data_gaps.csv", ["source", "index_before", "index_after", "gap", "unit"])
    minimum, maximum = float("inf"), 0.0
    with stream:
        for start in range(0, len(trace.time_s) - 1, 4096):
            differences = np.diff(trace.time_s[start:start + 4097])
            minimum, maximum = min(minimum, float(differences.min())), max(maximum, float(differences.max()))
            for index in np.flatnonzero(differences > 1.5 * alignment.frame_interval_s):
                writer.writerow({"source": "video", "index_before": start + int(index), "index_after": start + int(index) + 1, "gap": float(differences[index]), "unit": "seconds"})
                summary["video_gap_count"] += 1
        for start in range(0, len(session.sample_index) - 1, 4096):
            differences = np.diff(session.sample_index[start:start + 4097])
            for index in np.flatnonzero(differences > 1):
                writer.writerow({"source": "emg", "index_before": int(session.sample_index[start + index]), "index_after": int(session.sample_index[start + index + 1]), "gap": int(differences[index] - 1), "unit": "missing_samples"})
    summary.update({"min_video_interval_s": minimum, "max_video_interval_s": maximum})
    return summary


def _select_session(trace: VideoTrace, sessions: list[SignalSession], rate: float, max_residual_frames: float = 1.0) -> tuple[SignalSession, Alignment, dict[str, str]]:
    matches: list[tuple[SignalSession, Alignment]] = []
    rejected: dict[str, str] = {}
    for session in sessions:
        try:
            if session.metadata.get("recording_state", "complete") != "complete":
                raise AlignmentError("Recording is incomplete; inspect acquisition_report.json before synchronization")
            rates = session.metadata.get("sample_rate_hz", [])
            if rates and (len(rates) != 1 or abs(rates[0] - rate) > 0.01):
                raise AlignmentError("CSV sample_rate_hz metadata disagrees with configured nominal rate")
            alignment = fit_session_alignment(
                trace.led_state, trace.time_s, session.sync, session.sample_index,
                session_number=session.session_number, nominal_rate_hz=rate, max_residual_frames=max_residual_frames,
            )
            matches.append((session, alignment))
        except AlignmentError as exc:
            rejected[str(session.session_number)] = str(exc)
    if len(matches) != 1:
        raise AlignmentError(f"Expected one matching EMG session, found {len(matches)}; rejected={rejected}")
    return *matches[0], rejected


def _csv_writer(path: Path, fieldnames: list[str]):
    stream = path.open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    return stream, writer


def _number_or_none(value):
    return None if pd.isna(value) else float(value)


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Processing cancelled; original recording is preserved")


BRANCH_FRAME_FIELDS = ["emg_ch1_raw_uv_at_frame", "emg_ch2_raw_uv_at_frame", "emg_ch1_filtered_uv_at_frame", "emg_ch2_filtered_uv_at_frame", "filtered_value_type"]


def _frame_branches(session: SignalSession, sample_float: float, max_gap: int, raw1, raw2) -> dict:
    first = second = None
    kind = "not_available"
    if session.ch1_filtered_uv is not None:
        ids = session.sample_index
        right = int(np.searchsorted(ids, sample_float, side="left"))
        if right < len(ids) and abs(ids[right] - sample_float) < 1e-9:
            first, second = float(session.ch1_filtered_uv[right]), float(session.ch2_filtered_uv[right])
            kind = "observed"
        elif 0 < right < len(ids) and ids[right] - ids[right - 1] <= max_gap:
            if session.filter_revision is not None and session.filter_revision[right] != session.filter_revision[right - 1]:
                kind = "filter_revision_boundary"
            else:
                fraction = (sample_float - ids[right - 1]) / (ids[right] - ids[right - 1])
                first = float(session.ch1_filtered_uv[right - 1] + fraction * (session.ch1_filtered_uv[right] - session.ch1_filtered_uv[right - 1]))
                second = float(session.ch2_filtered_uv[right - 1] + fraction * (session.ch2_filtered_uv[right] - session.ch2_filtered_uv[right - 1]))
                kind = "interpolated"
        else:
            kind = "outside_or_gap"
    return {"emg_ch1_raw_uv_at_frame": raw1 if session.raw_available else None,
            "emg_ch2_raw_uv_at_frame": raw2 if session.raw_available else None,
            "emg_ch1_filtered_uv_at_frame": first, "emg_ch2_filtered_uv_at_frame": second, "filtered_value_type": kind}


def _interpolate_emg(session: SignalSession, sample_float: float, max_gap: int) -> tuple[int | None, int | None, float | None, float | None, str]:
    ids = session.sample_index
    index = int(np.searchsorted(ids, sample_float, side="left"))
    if index < len(ids) and abs(float(ids[index]) - sample_float) < 1e-9:
        return int(ids[index]), int(ids[index]), float(session.ch1_uv[index]), float(session.ch2_uv[index]), "observed"
    if index == 0 or index >= len(ids):
        return None, None, None, None, "outside_emg"
    before, after = int(ids[index - 1]), int(ids[index])
    if after - before > max_gap:
        return before, after, None, None, "emg_gap"
    fraction = (sample_float - before) / (after - before)
    ch1 = float(session.ch1_uv[index - 1] + fraction * (session.ch1_uv[index] - session.ch1_uv[index - 1]))
    ch2 = float(session.ch2_uv[index - 1] + fraction * (session.ch2_uv[index] - session.ch2_uv[index - 1]))
    return before, after, ch1, ch2, "interpolated"


def _write_frame_alignment(path: Path, trace: VideoTrace, session: SignalSession, alignment: Alignment, max_emg_gap: int, cancel_event=None) -> None:
    fields = ["frame_index", "video_time_s", "common_time_s", "led_state", "emg_sample_float", "emg_sample_before", "emg_sample_after", "emg_ch1_uv_at_frame", "emg_ch2_uv_at_frame", "emg_value_type"]
    stream, writer = _csv_writer(path, fields + BRANCH_FRAME_FIELDS)
    with stream:
        for i in range(len(trace.frame_index)):
            if i % 300 == 0:
                _check_cancel(cancel_event)
            time_s = float(trace.time_s[i])
            sample_float = float(alignment.sample_at_video_time(time_s))
            before, after, ch1, ch2, kind = _interpolate_emg(session, sample_float, max_emg_gap)
            writer.writerow({
                "frame_index": i, "video_time_s": time_s,
                "common_time_s": float(alignment.common_time_from_video(time_s)),
                "led_state": int(trace.led_state[i]), "emg_sample_float": sample_float,
                "emg_sample_before": before, "emg_sample_after": after,
                "emg_ch1_uv_at_frame": ch1, "emg_ch2_uv_at_frame": ch2,
                "emg_value_type": kind,
                **_frame_branches(session, sample_float, max_emg_gap, ch1, ch2),
            })


def _write_frame_fusion(path: Path, trace: VideoTrace, session: SignalSession, alignment: Alignment, tracking: TrackingData, max_emg_gap: int, cancel_event=None) -> None:
    fields = ["frame_index", "video_time_s", "common_time_s", "led_state", "emg_sample_float", "emg_sample_before", "emg_sample_after", "emg_ch1_uv_at_frame", "emg_ch2_uv_at_frame", "emg_value_type", "particle_id", "u_px", "v_px", "X_mm", "Y_mm", "detected", "quality_flag"]
    stream, writer = _csv_writer(path, fields + BRANCH_FRAME_FIELDS)
    marker_count = tracking.marker_count
    rows = tracking.rows()
    with stream:
        for frame_index in range(len(trace.frame_index)):
            if frame_index % 300 == 0:
                _check_cancel(cancel_event)
            time_s = float(trace.time_s[frame_index])
            sample_float = float(alignment.sample_at_video_time(time_s))
            before, after, ch1, ch2, emg_kind = _interpolate_emg(session, sample_float, max_emg_gap)
            for _ in range(marker_count):
                row = next(rows)
                if int(row["frame_index"]) != frame_index:
                    raise ValueError("Tracking rows are not ordered by frame")
                writer.writerow({
                    "frame_index": frame_index, "video_time_s": time_s,
                    "common_time_s": float(alignment.common_time_from_video(time_s)),
                    "led_state": int(trace.led_state[frame_index]), "emg_sample_float": sample_float,
                    "emg_sample_before": before, "emg_sample_after": after,
                    "emg_ch1_uv_at_frame": ch1, "emg_ch2_uv_at_frame": ch2,
                    "emg_value_type": emg_kind,
                    **_frame_branches(session, sample_float, max_emg_gap, ch1, ch2),
                    "particle_id": int(row["particle_id"]),
                    **{key: _number_or_none(row[key]) for key in ("u_px", "v_px", "X_mm", "Y_mm")},
                    "detected": bool(row["detected"]),
                    "quality_flag": row["quality_flag"] if emg_kind in ("observed", "interpolated") else f"{row['quality_flag']};{emg_kind}",
                })


def _write_sample_fusion(path: Path, session: SignalSession, alignment: Alignment, trace: VideoTrace, tracking: TrackingData, max_motion_gap_s: float, cancel_event=None) -> None:
    fields = ["sample_index", "common_time_s", "ch1_uv", "ch2_uv", "signal_kind", "sync", "dropped_samples", "pc_missing_samples", "particle_id", "u_px", "v_px", "X_mm", "Y_mm", "motion_value_type", "quality_flag"]
    stream, writer = _csv_writer(path, fields + ["ch1_raw_uv", "ch2_raw_uv", "ch1_filtered_uv", "ch2_filtered_uv", "filter_revision"])
    count = tracking.marker_count
    frame_count = len(trace.frame_index)
    before = [-1] * count
    after = [0] * count

    def next_observed(marker: int, frame: int) -> int:
        while frame < frame_count and not tracking.records[frame * count + marker]["detected"]:
            frame += 1
        return frame

    for marker in range(count):
        after[marker] = next_observed(marker, 0)
    with stream:
        for i, index in enumerate(session.sample_index):
            if i % 4096 == 0:
                _check_cancel(cancel_event)
            common_time = float(alignment.common_time_from_sample(float(index)))
            base = {
                "sample_index": int(index), "common_time_s": common_time,
                "ch1_uv": float(session.ch1_uv[i]), "ch2_uv": float(session.ch2_uv[i]),
                "signal_kind": session.signal_kind, "sync": int(session.sync[i]),
                "dropped_samples": int(session.dropped_samples[i]),
                "pc_missing_samples": int(session.pc_missing_samples[i]),
                "ch1_raw_uv": float(session.ch1_uv[i]) if session.raw_available else None,
                "ch2_raw_uv": float(session.ch2_uv[i]) if session.raw_available else None,
                "ch1_filtered_uv": float(session.ch1_filtered_uv[i]) if session.ch1_filtered_uv is not None else None,
                "ch2_filtered_uv": float(session.ch2_filtered_uv[i]) if session.ch2_filtered_uv is not None else None,
                "filter_revision": int(session.filter_revision[i]) if session.filter_revision is not None else None,
            }
            for particle_id in range(count):
                while after[particle_id] < frame_count and float(trace.time_s[after[particle_id]] - alignment.video_start_s) < common_time - 1e-9:
                    before[particle_id] = after[particle_id]
                    after[particle_id] = next_observed(particle_id, after[particle_id] + 1)
                first, last = before[particle_id], after[particle_id]
                position = {key: None for key in ("u_px", "v_px", "X_mm", "Y_mm")}
                kind, quality = "missing", "outside_motion" if first < 0 or last >= frame_count else "motion_gap"
                if last < frame_count:
                    row = tracking.records[last * count + particle_id]
                    last_time = float(trace.time_s[last] - alignment.video_start_s)
                    if abs(last_time - common_time) <= 1e-9:
                        position = {key: float(row[key]) for key in position}
                        kind = "observed"
                        quality = "observed" if row["quality"] == 1 else "reacquired_unverified"
                    elif first >= 0:
                        previous = tracking.records[first * count + particle_id]
                        first_time = float(trace.time_s[first] - alignment.video_start_s)
                        gap = last_time - first_time
                        if row["quality"] != 1 or previous["quality"] != 1:
                            quality = "reacquired_unverified"
                        elif gap <= max_motion_gap_s:
                            fraction = (common_time - first_time) / gap
                            position = {key: float(previous[key] + fraction * (row[key] - previous[key])) for key in position}
                            kind, quality = "interpolated", "interpolated"
                writer.writerow({**base, "particle_id": particle_id, **position,
                                 "motion_value_type": kind, "quality_flag": quality})


def run_pipeline(config: RunConfig, progress: Callable[[str], None] | None = None, cancel_event=None) -> dict:
    config.validate()
    output = config.output_dir
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    workspace = TemporaryDirectory(prefix=".work_", dir=output)
    trace = None
    tracking = None
    def announce(message):
        _check_cancel(cancel_event)
        if progress:
            progress(message)
    temporary_outputs: list[Path] = []
    report: dict = {"status": "running", "config": {
        **{key: str(value) if isinstance(value, Path) else value for key, value in asdict(config).items() if key not in ("led_roi", "marker_roi")},
        "led_roi": asdict(config.led_roi),
        "marker_roi": asdict(config.marker_roi) if config.marker_roi else None,
    }}
    try:
        announce("读取 EMG CSV 并分段")
        sessions = load_signal_sessions(config.emg_csv)
        report["input_files"] = {"emg_csv": _file_info(config.emg_csv), "video": _file_info(config.video_path)}
        report["emg_sessions"] = [{"session_number": s.session_number, "source_row_start": s.source_row_start, "source_row_end": s.source_row_end, "sample_index_start": int(s.sample_index[0]), "sample_index_end": int(s.sample_index[-1]), "missing_index_samples": s.missing_index_samples, "signal_kind": s.signal_kind, "metadata": s.metadata, "boundary_reason": s.boundary_reason} for s in sessions]
        announce("逐帧提取 LED 状态")
        cached_analysis = None
        if config.video_analysis_dir:
            from .video_analysis import load_video_analysis
            trace, tracking, cached_analysis = load_video_analysis(config.video_analysis_dir, report["input_files"]["video"], not config.sync_only)
            report["reused_video_analysis"] = str(config.video_analysis_dir.resolve())
            for key in ("led_roi", "marker_roi", "marker_count", "intrinsics_path", "homography_path"):
                report["config"][key] = cached_analysis["config"].get(key)
        else:
            trace = scan_video_led(
                config.video_path, config.led_roi, threshold=config.led_threshold,
                threshold_strategy=config.led_threshold_strategy, metric=config.led_metric,
                progress=announce, storage_dir=Path(workspace.name), allow_fps_fallback=config.allow_constant_fps_fallback,
            )
        trace.write_csv(output / "video_ttl.csv")
        report["video"] = {"frame_count": len(trace.frame_index), "width": trace.width, "height": trace.height, "fps_reported": trace.fps_reported, "timestamp_source": trace.timestamp_source, "led_metric": trace.metric, "led_threshold": trace.threshold, "led_edges": sum(1 for _ in iter_edges(trace.led_state, trace.time_s))}
        report["processing"] = {"video_metadata_storage": "disk_backed", "tracking_metadata_storage": "disk_backed", "metadata_chunk_rows": 4096, "decoded_frame_buffer": "current raw/undistorted frame only", "emg_storage": "in_memory"}
        report["timing_limits"] = {"absolute_emg_motion_latency_calibrated": False,
                                   "unmeasured_delays": ["ADS1292R digital filter group delay", "DRDY sync sampling phase", "LED response", "camera exposure and rolling shutter"]}
        report["warnings"] = []
        if trace.timestamp_source == "assumed_constant_fps":
            report["warnings"].append("Frame timestamps replaced by FPS only because the user explicitly enabled constant-FPS fallback")
        announce("识别起始编码并拟合 TTL 时间轴")
        session, alignment, rejected = _select_session(trace, sessions, config.nominal_rate_hz, config.max_alignment_residual_frames)
        report["alignment"] = alignment.as_dict()
        report["rejected_sessions"] = rejected
        report["data_quality"] = _write_quality_details(output, trace, session, alignment, config.nominal_rate_hz)
        if not alignment.end_marker_verified:
            report["warnings"].append("No end marker in either recording; ending boundary is unverified")
        _write_frame_alignment(output / "frame_alignment.csv", trace, session, alignment, config.max_emg_gap_samples, cancel_event)
        try:
            save_alignment_plot(output / "qc_alignment.png", trace, session, alignment)
        except Exception as exc:
            report["qc_plot_error"] = f"{type(exc).__name__}: {exc}"
        if config.sync_only:
            report["status"] = "sync_validated"
            report["full_fusion_validated"] = False
        else:
            announce("逐帧追踪反光点并换算平面坐标")
            if tracking is None:
                assert config.marker_roi is not None and config.intrinsics_path is not None and config.homography_path is not None
                tracking = track_video_markers(
                config.video_path, trace, config.marker_roi, marker_count=config.marker_count,
                intrinsics_path=config.intrinsics_path, homography_path=config.homography_path,
                threshold=config.marker_threshold, min_area=config.marker_min_area,
                max_area=config.marker_max_area, min_circularity=config.marker_min_circularity,
                search_range_px=config.marker_search_range_px, memory_frames=config.marker_memory_frames,
                max_intrinsics_rms_px=config.max_intrinsics_rms_px,
                max_homography_rms_mm=config.max_homography_rms_mm,
                progress=announce, storage_dir=Path(workspace.name),
                )
            if not bool(tracking.records["detected"].any()):
                raise ValueError("No reflective markers were detected in the marker ROI")
            tracking_temp = output / ".tracking.csv.tmp"
            frame_temp = output / ".aligned_by_frame.csv.tmp"
            sample_temp = output / ".aligned_by_emg_sample.csv.tmp"
            temporary_outputs.extend([tracking_temp, frame_temp, sample_temp])
            tracking.write_csv(tracking_temp)
            report["estimated_sample_export_rows"] = len(session.sample_index) * tracking.marker_count
            report["estimated_sample_export_bytes"] = report["estimated_sample_export_rows"] * 350
            announce(f"导出样本表：约 {report['estimated_sample_export_rows']} 行，预估 {report['estimated_sample_export_bytes'] / 1e9:.2f} GB")
            _write_frame_fusion(frame_temp, trace, session, alignment, tracking, config.max_emg_gap_samples, cancel_event)
            _write_sample_fusion(sample_temp, session, alignment, trace, tracking, config.max_motion_gap_s, cancel_event)
            _check_cancel(cancel_event)
            tracking_temp.replace(output / "tracking.csv")
            frame_temp.replace(output / "aligned_by_frame.csv")
            sample_temp.replace(output / "aligned_by_emg_sample.csv")
            report["calibration"] = cached_analysis["calibration"] if cached_analysis else {"intrinsics": _file_info(config.intrinsics_path), "homography": _file_info(config.homography_path)}
            report["marker_coverage"] = tracking.coverage()
            report["status"] = "full_fusion_generated"
            report["full_fusion_validated"] = False  # requires the planned manual 30-frame review
        (output / "alignment_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        if progress:
            progress(f"完成：{output}")
        return report
    except Exception as exc:
        for temporary in temporary_outputs:
            temporary.unlink(missing_ok=True)
        report["status"] = "cancelled" if isinstance(exc, InterruptedError) else "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        (output / "alignment_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        raise
    finally:
        if tracking is not None:
            tracking.close()
        if trace is not None:
            trace.close()
        workspace.cleanup()
