"""Streaming video LED decoding and planar reflective-marker tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from .calibration import PlaneTransform, Undistorter
from .storage import CHUNK_ROWS, RecordBuffer, close_mapping


LED_DTYPE = np.dtype([("frame_index", "i8"), ("time_s", "f8"), ("mean", "f8"), ("max", "f8"), ("score", "f8"), ("state", "i1")])
TRACK_DTYPE = np.dtype([("frame_index", "i8"), ("particle_id", "i4"), ("u_px", "f8"), ("v_px", "f8"), ("X_mm", "f8"), ("Y_mm", "f8"), ("detected", "?"), ("quality", "i1")])
QUALITY_NAMES = ("missing", "observed", "reacquired_unverified")


class VideoInputError(ValueError):
    pass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def parse(cls, value: str) -> "Rect":
        try:
            parts = [int(part.strip()) for part in value.split(",")]
            if len(parts) != 4:
                raise ValueError
            return cls(*parts)
        except ValueError as exc:
            raise VideoInputError("ROI must be x,y,w,h") from exc

    def check(self, width: int, height: int) -> None:
        if self.x < 0 or self.y < 0 or self.w <= 0 or self.h <= 0 or self.x + self.w > width or self.y + self.h > height:
            raise VideoInputError(f"ROI {self} is outside {width}x{height} video")

    def crop(self, frame: np.ndarray) -> np.ndarray:
        return frame[self.y : self.y + self.h, self.x : self.x + self.w]


@dataclass(frozen=True)
class VideoTrace:
    frame_index: np.ndarray
    time_s: np.ndarray
    led_mean: np.ndarray
    led_max: np.ndarray
    led_score: np.ndarray
    led_state: np.ndarray
    threshold: float
    metric: str
    timestamp_source: str
    width: int
    height: int
    fps_reported: float
    records: np.ndarray | None = field(default=None, repr=False)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame({
            "frame_index": self.frame_index, "video_time_s": self.time_s,
            "roi_mean": self.led_mean, "roi_max": self.led_max,
            "led_score": self.led_score, "led_state": self.led_state,
        })

    def write_csv(self, path: Path) -> None:
        for start in range(0, len(self.frame_index), CHUNK_ROWS):
            stop = start + CHUNK_ROWS
            pd.DataFrame({"frame_index": self.frame_index[start:stop], "video_time_s": self.time_s[start:stop],
                          "roi_mean": self.led_mean[start:stop], "roi_max": self.led_max[start:stop],
                          "led_score": self.led_score[start:stop], "led_state": self.led_state[start:stop]}).to_csv(
                              path, mode="w" if start == 0 else "a", header=start == 0, index=False)

    def close(self) -> None:
        close_mapping(self.records)


@dataclass
class TrackingData:
    records: np.ndarray
    marker_count: int

    def rows(self):
        for record in self.records:
            yield {name: record[name].item() for name in self.records.dtype.names if name != "quality"} | {"quality_flag": QUALITY_NAMES[int(record["quality"])]}

    def write_csv(self, path: Path) -> None:
        import csv
        names = [name for name in self.records.dtype.names if name != "quality"] + ["quality_flag"]
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=names)
            writer.writeheader()
            for row in self.rows():
                writer.writerow({key: "" if isinstance(value, float) and not np.isfinite(value) else value for key, value in row.items()})

    def coverage(self) -> dict[str, float]:
        return {str(marker): float(np.count_nonzero(self.records[marker::self.marker_count]["detected"]) / (len(self.records) // self.marker_count)) for marker in range(self.marker_count)}

    def close(self) -> None:
        close_mapping(self.records)


def _open_video(path: str | Path) -> cv2.VideoCapture:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise VideoInputError(f"Could not open video: {path}")
    return capture


def _remove_short_runs(states: np.ndarray, maximum: int) -> np.ndarray:
    result = states.copy()
    if maximum < 1 or len(states) < 3:
        return result
    boundaries = np.r_[0, np.flatnonzero(states[1:] != states[:-1]) + 1, len(states)]
    for first, last, next_last in zip(boundaries[:-2], boundaries[1:-1], boundaries[2:]):
        if first == 0 or last - first > maximum:
            continue
        if states[first - 1] == states[last]:
            result[first:last] = states[first - 1]
    return result


def scan_video_led(
    path: str | Path,
    led_roi: Rect | None,
    *,
    threshold: float | None = None,
    threshold_strategy: str = "otsu_high_margin",
    metric: str = "p99",
    remove_glitches_up_to_frames: int = 2,
    allow_fps_fallback: bool = False,
    progress: Callable[[str], None] | None = None,
    storage_dir: Path | None = None,
    frame_consumer: Callable[[int, np.ndarray], None] | None = None,
) -> VideoTrace:
    capture = _open_video(path)
    buffer = RecordBuffer(LED_DTYPE, storage_dir / "led.bin" if storage_dir else None)
    count = 0
    try:
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count_reported = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if led_roi is not None:
            led_roi.check(width, height)
        histogram = np.zeros(256, dtype=np.int64)
        minimum, maximum = float("inf"), -float("inf")
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame_consumer is not None:
                frame_consumer(count, frame)
            gray = cv2.cvtColor(led_roi.crop(frame), cv2.COLOR_BGR2GRAY) if led_roi is not None else np.zeros((1, 1), dtype=np.uint8)
            mean = float(np.mean(gray))
            if metric == "p99":
                score = float(np.percentile(gray, 99))
            elif metric == "mean":
                score = mean
            else:
                raise VideoInputError("LED metric must be p99 or mean")
            buffer.append((count, float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0, mean, float(np.max(gray)), score, 0))
            histogram[int(np.clip(score, 0, 255))] += 1
            minimum, maximum = min(minimum, score), max(maximum, score)
            count += 1
            if progress is not None and count % 300 == 0:
                progress(f"LED: {count}/{frame_count_reported or '?'} 帧")
    except BaseException:
        buffer.close()
        raise
    finally:
        capture.release()
    if count == 0:
        buffer.close()
        raise VideoInputError("Video contains no decodable frames")
    records = buffer.finish()
    time_array = records["time_s"]
    pts_valid = True
    for start in range(0, count, CHUNK_ROWS):
        block = time_array[max(0, start - 1):start + CHUNK_ROWS]
        pts_valid &= bool(np.isfinite(block).all() and np.all(np.diff(block) > 0))
    if pts_valid:
        origin = float(time_array[0])
        for start in range(0, count, CHUNK_ROWS):
            time_array[start:start + CHUNK_ROWS] -= origin
        timestamp_source = "opencv_pts"
    elif allow_fps_fallback and fps > 0:
        for start in range(0, count, CHUNK_ROWS):
            time_array[start:start + CHUNK_ROWS] = np.arange(start, min(count, start + CHUNK_ROWS)) / fps
        timestamp_source = "assumed_constant_fps"
    else:
        close_mapping(records)
        raise VideoInputError("Video frame timestamps are invalid; constant-FPS fallback needs explicit confirmation")
    if led_roi is None:
        threshold = 0.0
    if threshold is None:
        if maximum - minimum < 8:
            close_mapping(records)
            raise VideoInputError("LED ROI has too little brightness contrast")
        weights = np.cumsum(histogram).astype(float)
        sums = np.cumsum(histogram * np.arange(256))
        denominator = weights * (count - weights)
        between = np.divide((sums[-1] * weights - sums * count) ** 2, denominator, out=np.zeros(256), where=denominator > 0)
        otsu = float(np.argmax(between))
        if threshold_strategy == "otsu_high_margin":
            high = float(np.searchsorted(weights, 0.99 * count))
            threshold = (otsu + high) / 2
        elif threshold_strategy == "otsu":
            threshold = float(otsu)
        else:
            close_mapping(records)
            raise VideoInputError("threshold_strategy must be otsu or otsu_high_margin")
    for start in range(0, count, CHUNK_ROWS):
        records["state"][start:start + CHUNK_ROWS] = (records["score"][start:start + CHUNK_ROWS] >= threshold) if led_roi is not None else -1
    states = records["state"]
    # Keep only run boundaries in each block; do not copy the full state array.
    run_start, current, previous = 0, int(states[0]), -1
    for start in range(0, count, CHUNK_ROWS):
        block = states[max(0, start - 1):start + CHUNK_ROWS]
        changes = np.flatnonzero(block[1:] != block[:-1]) + max(0, start - 1) + 1
        for index in changes:
            next_state = int(states[index])
            if run_start > 0 and index - run_start <= remove_glitches_up_to_frames and previous == next_state:
                states[run_start:index] = previous
            previous, current, run_start = current, next_state, int(index)
    return VideoTrace(
        frame_index=records["frame_index"], time_s=time_array,
        led_mean=records["mean"], led_max=records["max"], led_score=records["score"], led_state=states,
        threshold=float(threshold), metric=metric, timestamp_source=timestamp_source,
        width=width, height=height, fps_reported=fps,
        records=records,
    )


def select_roi(path: str | Path, label: str, intrinsics_path: str | Path | None = None) -> Rect:
    capture = _open_video(path)
    try:
        ok, frame = capture.read()
    finally:
        capture.release()
    if not ok:
        raise VideoInputError("Could not read first video frame for ROI selection")
    height, width = frame.shape[:2]
    if intrinsics_path is not None:
        frame = Undistorter(intrinsics_path, (width, height)).apply(frame)
    scale = min(1.0, 1280 / width, 800 / height)
    preview = frame if scale == 1 else cv2.resize(frame, (round(width * scale), round(height * scale)))
    window = f"Select {label} ROI (Enter to accept)"
    try:
        x, y, w, h = cv2.selectROI(window, preview, showCrosshair=True, fromCenter=False)
    finally:
        cv2.destroyWindow(window)
    if w <= 0 or h <= 0:
        raise VideoInputError("ROI selection was cancelled")
    roi = Rect(round(x / scale), round(y / scale), round(w / scale), round(h / scale))
    roi.check(width, height)
    return roi


def _detect_markers(
    frame: np.ndarray,
    roi: Rect,
    *,
    threshold: int,
    min_area: float,
    max_area: float,
    min_circularity: float,
    candidate_limit: int,
) -> list[tuple[float, float, float]]:
    gray = cv2.cvtColor(roi.crop(frame), cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(blurred, threshold, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found: list[tuple[float, float, float]] = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if not min_area <= area <= max_area:
            continue
        perimeter = float(cv2.arcLength(contour, True))
        circularity = 4 * np.pi * area / perimeter**2 if perimeter else 0
        if circularity < min_circularity:
            continue
        moments = cv2.moments(contour)
        if moments["m00"] == 0:
            continue
        x = float(moments["m10"] / moments["m00"] + roi.x)
        y = float(moments["m01"] / moments["m00"] + roi.y)
        mask = np.zeros_like(gray, dtype=np.uint8)
        cv2.drawContours(mask, [contour], -1, 255, -1)
        peak = float(np.max(gray[mask > 0]))
        bx, by, bw, bh = cv2.boundingRect(contour)
        ring = gray[max(0, by - 12) : min(gray.shape[0], by + bh + 12), max(0, bx - 12) : min(gray.shape[1], bx + bw + 12)]
        contrast = peak - float(np.median(ring))
        found.append((x, y, peak + 2 * max(0, contrast)))
    found.sort(key=lambda item: item[2], reverse=True)
    return found[:candidate_limit]


def track_video_markers(
    path: str | Path,
    trace: VideoTrace,
    marker_roi: Rect,
    *,
    marker_count: int,
    intrinsics_path: str | Path,
    homography_path: str | Path,
    threshold: int = 200,
    min_area: float = 5.0,
    max_area: float = 6000.0,
    min_circularity: float = 0.5,
    search_range_px: float = 50.0,
    memory_frames: int = 5,
    max_intrinsics_rms_px: float = 2.0,
    max_homography_rms_mm: float = 1.0,
    progress: Callable[[str], None] | None = None,
    storage_dir: Path | None = None,
) -> TrackingData:
    from .motion import MarkerTracker
    tracker = MarkerTracker((trace.width, trace.height), marker_roi, marker_count,
                            intrinsics_path, homography_path, threshold, min_area, max_area,
                            min_circularity, search_range_px, memory_frames,
                            max_intrinsics_rms_px, max_homography_rms_mm, storage_dir)
    capture = _open_video(path)
    index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            tracker.process(index, frame)
            index += 1
            if progress is not None and index % 300 == 0:
                progress(f'Markers: {index}/{len(trace.frame_index)} frames')
        if index != len(trace.frame_index):
            raise VideoInputError('Video frame count changed between LED and marker passes')
        return tracker.finish()
    except BaseException:
        tracker.abort()
        raise
    finally:
        capture.release()
