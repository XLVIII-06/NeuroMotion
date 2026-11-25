"""Independent LED/marker analysis, shared decoding and reusable disk results."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np
import pandas as pd

from .motion import MarkerTracker
from .session import file_info, write_json
from .video import Rect, TrackingData, VideoTrace, _open_video, scan_video_led


@dataclass(frozen=True)
class VideoAnalysisConfig:
    video_path: Path
    output_dir: Path
    led_roi: Rect | None = None
    marker_roi: Rect | None = None
    marker_count: int = 5
    intrinsics_path: Path | None = None
    homography_path: Path | None = None
    led_metric: str = "p99"
    led_threshold: float | None = None
    marker_threshold: int = 200
    marker_search_range_px: float = 50
    marker_memory_frames: int = 5
    allow_constant_fps_fallback: bool = False


def analyze_video(config: VideoAnalysisConfig, progress=print) -> dict:
    if not config.video_path.is_file() or (config.led_roi is None and config.marker_roi is None):
        raise ValueError("Provide a video and at least one LED/marker ROI")
    output = config.output_dir
    if output.exists() and any(output.iterdir()):
        raise ValueError("Video analysis output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    cache = output / "cache"
    cache.mkdir()
    tracker = trace = tracking = None
    report = {"schema_version": 2, "status": "running", "source_video": file_info(config.video_path),
              "config": asdict(config), "led_extracted": config.led_roi is not None, "markers_extracted": config.marker_roi is not None}
    report["config"] = {key: str(value) if isinstance(value, Path) else value for key, value in report["config"].items()}
    with TemporaryDirectory(prefix=".work_", dir=output) as workspace:
        try:
            capture = _open_video(config.video_path)
            size = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            capture.release()
            if config.marker_roi:
                tracker = MarkerTracker(size, config.marker_roi, config.marker_count, config.intrinsics_path,
                                        config.homography_path, threshold=config.marker_threshold,
                                        search_range_px=config.marker_search_range_px, memory_frames=config.marker_memory_frames,
                                        storage_dir=Path(workspace))
            progress("逐帧分析视频：共享 LED 与反光点解码")
            trace = scan_video_led(config.video_path, config.led_roi, threshold=config.led_threshold,
                                   metric=config.led_metric, allow_fps_fallback=config.allow_constant_fps_fallback,
                                   storage_dir=Path(workspace), frame_consumer=tracker.process if tracker else None, progress=progress)
            np.save(cache / "video_trace.npy", trace.records)
            if config.led_roi:
                trace.write_csv(output / "video_ttl.csv")
            else:
                for start in range(0, len(trace.time_s), 4096):
                    pd.DataFrame({"frame_index": trace.frame_index[start:start + 4096], "video_time_s": trace.time_s[start:start + 4096]}).to_csv(output / "frame_timing.csv", mode="w" if start == 0 else "a", header=start == 0, index=False)
            report["video"] = {"width": trace.width, "height": trace.height, "fps_reported": trace.fps_reported,
                               "frame_count": len(trace.frame_index), "timestamp_source": trace.timestamp_source,
                               "led_metric": trace.metric, "led_threshold": trace.threshold if config.led_roi else None}
            if tracker:
                tracking = tracker.finish()
                tracking.write_csv(output / "tracking.csv")
                np.save(cache / "tracking.npy", tracking.records)
                report["marker_count"] = config.marker_count
                report["coordinate_system"] = tracker.coordinate_system
                report["millimeter_coordinates"] = tracker.plane is not None
                report["marker_coverage"] = tracking.coverage()
                report["calibration"] = {"intrinsics": file_info(config.intrinsics_path) if config.intrinsics_path else None,
                                         "homography": file_info(config.homography_path) if config.homography_path else None}
            report["status"] = "video_analysis_complete"
            report["full_fusion_validated"] = False
            write_json(output / "video_analysis_report.json", report)
            progress(f"视频分析完成：{output}")
            return report
        except Exception as exc:
            report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            write_json(output / "video_analysis_report.json", report)
            raise
        finally:
            if tracking:
                tracking.close()
            elif tracker:
                tracker.abort()
            if trace:
                trace.close()


def load_video_analysis(directory: Path, video_info: dict, require_markers: bool, *, require_metric: bool = True):
    report = json.loads((directory / "video_analysis_report.json").read_text(encoding="utf-8"))
    if report["status"] != "video_analysis_complete" or not report["led_extracted"]:
        raise ValueError("Selected video analysis has no completed LED extraction")
    if report["source_video"]["sha256"] != video_info["sha256"]:
        raise ValueError("Video analysis belongs to a different source video")
    if require_markers and (not report["markers_extracted"] or (require_metric and not report.get("millimeter_coordinates"))):
        raise ValueError("Full fusion needs completed pixel/mm marker analysis")
    data = np.load(directory / "cache/video_trace.npy", mmap_mode="r")
    meta = report["video"]
    trace = VideoTrace(data["frame_index"], data["time_s"], data["mean"], data["max"], data["score"], data["state"],
                       meta["led_threshold"], meta["led_metric"], meta["timestamp_source"], meta["width"], meta["height"], meta["fps_reported"], data)
    tracking = None
    if require_markers:
        tracking = TrackingData(np.load(directory / "cache/tracking.npy", mmap_mode="r"), report["marker_count"])
    return trace, tracking, report
