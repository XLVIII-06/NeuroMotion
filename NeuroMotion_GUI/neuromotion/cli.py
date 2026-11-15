"""NeuroMotion command-line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from .calibration import calibrate_intrinsics, calibrate_plane
from .pipeline import RunConfig, run_pipeline
from .video import Rect, select_roi


def _run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="JSON run configuration")
    parser.add_argument("--emg", type=Path)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--led-roi", help="Raw video ROI: x,y,w,h")
    parser.add_argument("--marker-roi", help="Undistorted video ROI: x,y,w,h")
    parser.add_argument("--intrinsics", type=Path)
    parser.add_argument("--homography", type=Path)
    parser.add_argument("--markers", type=int)
    parser.add_argument("--sample-rate", type=float)
    parser.add_argument("--led-metric", choices=["p99", "mean"])
    parser.add_argument("--led-threshold", type=float)
    parser.add_argument("--marker-threshold", type=int)
    parser.add_argument("--sync-only", action="store_true")
    parser.add_argument("--video-analysis", type=Path)


def _value(args: argparse.Namespace, config: dict, key: str, argument: str | None = None, default=None):
    override = getattr(args, argument or key, None)
    return override if override is not None else config.get(key, default)


def _rect(value) -> Rect | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return Rect(**value)
    return Rect.parse(str(value))


def build_run_config(args: argparse.Namespace) -> RunConfig:
    values = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    if not isinstance(values, dict):
        raise ValueError("Config JSON must contain an object")
    emg = _value(args, values, "emg_csv", "emg")
    video = _value(args, values, "video_path", "video")
    output = _value(args, values, "output_dir", "output")
    led = _value(args, values, "led_roi")
    if args.led_roi is not None:
        led = args.led_roi
    cached = _value(args, values, "video_analysis_dir", "video_analysis")
    if led is None and cached:
        led = json.loads((Path(cached) / "video_analysis_report.json").read_text(encoding="utf-8"))["config"]["led_roi"]
    if any(item is None for item in (emg, video, output, led)):
        raise ValueError("Run needs EMG CSV, video, output directory and LED ROI")
    marker_roi = args.marker_roi if args.marker_roi is not None else values.get("marker_roi")
    intrinsics = _value(args, values, "intrinsics_path", "intrinsics")
    homography = _value(args, values, "homography_path", "homography")
    return RunConfig(
        emg_csv=Path(emg), video_path=Path(video), output_dir=Path(output), led_roi=_rect(led),
        sync_only=args.sync_only or bool(values.get("sync_only", False)),
        marker_roi=_rect(marker_roi), marker_count=int(_value(args, values, "marker_count", "markers", 1)),
        intrinsics_path=Path(intrinsics) if intrinsics else None,
        homography_path=Path(homography) if homography else None,
        nominal_rate_hz=float(_value(args, values, "nominal_rate_hz", "sample_rate", 1000.0)),
        led_metric=str(_value(args, values, "led_metric", default="p99")),
        led_threshold=_value(args, values, "led_threshold"),
        marker_threshold=int(_value(args, values, "marker_threshold", default=200)),
        led_threshold_strategy=str(values.get("led_threshold_strategy", "otsu_high_margin")),
        marker_min_area=float(values.get("marker_min_area", 5.0)),
        marker_max_area=float(values.get("marker_max_area", 6000.0)),
        marker_min_circularity=float(values.get("marker_min_circularity", 0.5)),
        marker_search_range_px=float(values.get("marker_search_range_px", 50.0)),
        marker_memory_frames=int(values.get("marker_memory_frames", 5)),
        max_emg_gap_samples=int(values.get("max_emg_gap_samples", 1)),
        max_motion_gap_s=float(values.get("max_motion_gap_s", 0.1)),
        max_intrinsics_rms_px=float(values.get("max_intrinsics_rms_px", 2.0)),
        max_homography_rms_mm=float(values.get("max_homography_rms_mm", 1.0)),
        allow_constant_fps_fallback=bool(values.get("allow_constant_fps_fallback", False)),
        video_analysis_dir=Path(cached) if cached else None,
        max_alignment_residual_frames=float(values.get("max_alignment_residual_frames", 1.0)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NeuroMotion offline EMG + video alignment")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Align EMG and video; optionally track markers")
    _run_args(run)
    commands.add_parser("gui", help="Open the Python desktop interface")
    record = commands.add_parser("record", help="Record live EMG or a clearly identified binary/simulated source")
    source = record.add_mutually_exclusive_group(required=True)
    source.add_argument("--port")
    source.add_argument("--replay", type=Path)
    source.add_argument("--simulate", action="store_true")
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--seconds", type=float, default=0, help="Wall time for serial, simulated duration for --simulate")
    record.add_argument("--speed", type=float, default=1)
    analyze = commands.add_parser("analyze-video", help="Standalone LED and/or marker analysis without EMG")
    _run_args(analyze)
    validate = commands.add_parser("validate", help="Run persistent software/real-sync acceptance suite")
    validate.add_argument("--output", type=Path, required=True)
    integration = commands.add_parser("validate-integration", help="Validate real parser -> acquisition -> video -> alignment -> replay")
    integration.add_argument("--output", type=Path, required=True)
    review = commands.add_parser("review", help="Prepare 30-frame human review images and annotation templates")
    review.add_argument("--result", type=Path, required=True)
    review.add_argument("--count", type=int, default=30)
    replay = commands.add_parser("index-replay", help="Build/reuse synchronized disk replay indexes")
    replay.add_argument("--result", type=Path, required=True)
    roi = commands.add_parser("select-roi", help="Choose an ROI on the video")
    roi.add_argument("--video", type=Path, required=True)
    roi.add_argument("--label", default="LED")
    roi.add_argument("--intrinsics", type=Path)
    intrinsics = commands.add_parser("calibrate-intrinsics", help="Calibrate camera intrinsics from chessboard images")
    intrinsics.add_argument("--images", required=True, help="Glob of chessboard images, e.g. images/*.jpg")
    intrinsics.add_argument("--output", type=Path, required=True)
    intrinsics.add_argument("--cols", type=int, default=11)
    intrinsics.add_argument("--rows", type=int, default=8)
    intrinsics.add_argument("--square-mm", type=float, default=3.0)
    plane = commands.add_parser("calibrate-plane", help="Calibrate pixel-to-mm homography on the movement plane")
    plane.add_argument("--video", type=Path, required=True)
    plane.add_argument("--intrinsics", type=Path, required=True)
    plane.add_argument("--output", type=Path, required=True)
    plane.add_argument("--frame", type=int, default=0)
    plane.add_argument("--cols", type=int, default=11)
    plane.add_argument("--rows", type=int, default=8)
    plane.add_argument("--square-mm", type=float, default=3.0)
    args = parser.parse_args(argv)
    if args.command == "run":
        result = run_pipeline(build_run_config(args), progress=print)
        print(json.dumps({"status": result["status"], "alignment": result["alignment"]}, indent=2))
    elif args.command == "gui":
        from .gui import launch
        launch()
    elif args.command == "record":
        from .acquisition import AcquisitionController, BinaryReplaySource, SimulatedSource
        source = BinaryReplaySource(args.replay, args.speed) if args.replay else SimulatedSource(args.seconds or 1800, args.speed) if args.simulate else None
        controller = AcquisitionController()
        controller.connect(port=args.port, source=source)
        try:
            controller.start_recording(args.output)
            started = time.monotonic()
            while controller.thread.is_alive():
                controller.thread.join(timeout=1)
                print(f"Saved {controller.snapshot(include_plot=False)['recorded_samples']} samples", flush=True)
                if source is None and args.seconds and time.monotonic() - started >= args.seconds:
                    controller.stop_recording()
                    break
        except KeyboardInterrupt:
            if controller.thread.is_alive():
                controller.stop_recording()
        finally:
            controller.disconnect()
        controller.wait()
    elif args.command == "analyze-video":
        from .video_analysis import VideoAnalysisConfig, analyze_video
        values = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
        video = _value(args, values, "video_path", "video")
        output = _value(args, values, "output_dir", "output")
        if not video or not output:
            raise ValueError("Video analysis needs --video and --output")
        intrinsics = _value(args, values, "intrinsics_path", "intrinsics")
        homography = _value(args, values, "homography_path", "homography")
        result = analyze_video(VideoAnalysisConfig(Path(video), Path(output), _rect(_value(args, values, "led_roi")),
                                                   _rect(_value(args, values, "marker_roi")),
                                                   int(_value(args, values, "marker_count", "markers", 5)),
                                                   Path(intrinsics) if intrinsics else None, Path(homography) if homography else None,
                                                   str(_value(args, values, "led_metric", default="p99")),
                                                   _value(args, values, "led_threshold"), int(_value(args, values, "marker_threshold", default=200)),
                                                   allow_constant_fps_fallback=bool(values.get("allow_constant_fps_fallback", False))))
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "validate":
        from .validation import run_validation_suite
        print(json.dumps(run_validation_suite(args.output), indent=2, ensure_ascii=False))
    elif args.command == "validate-integration":
        from .integration_validation import run_integration_validation
        print(json.dumps(run_integration_validation(args.output), indent=2, ensure_ascii=False))
    elif args.command == "review":
        from .review import prepare_review
        print(json.dumps(prepare_review(args.result, args.count), indent=2, ensure_ascii=False))
    elif args.command == "index-replay":
        from .replay import build_replay_index
        print(json.dumps(build_replay_index(args.result, progress=print), indent=2, ensure_ascii=False))
    elif args.command == "select-roi":
        chosen = select_roi(args.video, args.label, args.intrinsics)
        print(f"{chosen.x},{chosen.y},{chosen.w},{chosen.h}")
    elif args.command == "calibrate-intrinsics":
        print(json.dumps(calibrate_intrinsics(args.images, args.output, pattern=(args.cols, args.rows), square_mm=args.square_mm), indent=2))
    elif args.command == "calibrate-plane":
        print(json.dumps(calibrate_plane(args.video, args.intrinsics, args.output, frame_index=args.frame, pattern=(args.cols, args.rows), square_mm=args.square_mm), indent=2))
    return 0
