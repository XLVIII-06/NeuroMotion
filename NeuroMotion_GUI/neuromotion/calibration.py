"""Camera calibration and plane mapping in undistorted image coordinates."""

from __future__ import annotations

import glob
import json
from pathlib import Path

import cv2
import numpy as np


class CalibrationError(ValueError):
    pass


def _world_chessboard(pattern: tuple[int, int], square_mm: float) -> np.ndarray:
    points = np.zeros((pattern[0] * pattern[1], 3), np.float32)
    points[:, :2] = np.mgrid[0 : pattern[0], 0 : pattern[1]].T.reshape(-1, 2)
    points *= square_mm
    return points


def _corners(frame: np.ndarray, pattern: tuple[int, int]) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    found, points = cv2.findChessboardCorners(gray, pattern)
    if not found:
        raise CalibrationError("Chessboard corners were not found")
    return cv2.cornerSubPix(
        gray, points, (11, 11), (-1, -1),
        (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001),
    )


def calibrate_intrinsics(image_glob: str, output_path: str | Path, *, pattern: tuple[int, int] = (11, 8), square_mm: float = 3.0) -> dict:
    images = sorted(glob.glob(image_glob))
    if len(images) < 3:
        raise CalibrationError("At least three chessboard images are required")
    object_points: list[np.ndarray] = []
    image_points: list[np.ndarray] = []
    image_size: tuple[int, int] | None = None
    for path in images:
        frame = cv2.imread(path)
        if frame is None:
            continue
        size = (frame.shape[1], frame.shape[0])
        if image_size is not None and size != image_size:
            raise CalibrationError("Calibration images have different resolutions")
        image_size = size
        try:
            points = _corners(frame, pattern)
        except CalibrationError:
            continue
        object_points.append(_world_chessboard(pattern, square_mm))
        image_points.append(points)
    if len(image_points) < 3 or image_size is None:
        raise CalibrationError("At least three images must have detectable chessboards")
    rms, matrix, distortion, _, _ = cv2.calibrateCamera(object_points, image_points, image_size, None, None)
    data = {
        "K": matrix.tolist(), "dist": distortion.tolist(), "image_size": list(image_size),
        "pattern_size": list(pattern), "square_size_mm": square_mm,
        "rms_reprojection_px": float(rms), "used_images": len(image_points),
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


class Undistorter:
    def __init__(self, intrinsics_path: str | Path, frame_size: tuple[int, int]):
        self.path = Path(intrinsics_path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if tuple(data.get("image_size", [])) != frame_size:
            raise CalibrationError("Intrinsics image_size does not match the video")
        self.rms_error_px = float(data.get("rms_reprojection_px", float("nan")))
        matrix = np.asarray(data["K"], dtype=np.float64)
        distortion = np.asarray(data["dist"], dtype=np.float64)
        if matrix.shape != (3, 3):
            raise CalibrationError("Intrinsics K must be 3x3")
        self.map1, self.map2 = cv2.initUndistortRectifyMap(
            matrix, distortion, None, matrix, frame_size, cv2.CV_16SC2
        )

    def apply(self, frame: np.ndarray) -> np.ndarray:
        return cv2.remap(frame, self.map1, self.map2, cv2.INTER_LINEAR)


class PlaneTransform:
    def __init__(self, homography_path: str | Path, frame_size: tuple[int, int]):
        self.path = Path(homography_path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if tuple(data.get("image_size", [])) != frame_size:
            raise CalibrationError("Homography image_size does not match the video")
        if data.get("coordinate_system") != "undistorted_full_frame":
            raise CalibrationError("Homography coordinate system must be undistorted_full_frame")
        self.matrix = np.asarray(data["H"], dtype=np.float64)
        if self.matrix.shape != (3, 3) or not np.isfinite(self.matrix).all():
            raise CalibrationError("Homography H must be a finite 3x3 matrix")
        self.rms_error_mm = float(data.get("rms_reprojection_mm", float("nan")))

    def apply(self, u: float, v: float) -> tuple[float, float]:
        point = cv2.perspectiveTransform(np.array([[[u, v]]], dtype=np.float64), self.matrix)[0, 0]
        return float(point[0]), float(point[1])


def calibrate_plane(
    video_path: str | Path,
    intrinsics_path: str | Path,
    output_path: str | Path,
    *,
    frame_index: int = 0,
    pattern: tuple[int, int] = (11, 8),
    square_mm: float = 3.0,
) -> dict:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise CalibrationError(f"Could not open video: {video_path}")
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
    finally:
        capture.release()
    if not ok or frame is None:
        raise CalibrationError(f"Could not read calibration frame {frame_index}")
    size = (frame.shape[1], frame.shape[0])
    undistorted = Undistorter(intrinsics_path, size).apply(frame)
    image_points = _corners(undistorted, pattern).reshape(-1, 2)
    world_points = _world_chessboard(pattern, square_mm)[:, :2]
    matrix, _ = cv2.findHomography(image_points, world_points, method=0)
    if matrix is None:
        raise CalibrationError("Could not compute plane homography")
    projected = cv2.perspectiveTransform(image_points.reshape(-1, 1, 2), matrix).reshape(-1, 2)
    rms = float(np.sqrt(np.mean(np.sum((projected - world_points) ** 2, axis=1))))
    data = {
        "H": matrix.tolist(), "image_size": list(size),
        "coordinate_system": "undistorted_full_frame",
        "pattern_size": list(pattern), "square_size_mm": square_mm,
        "rms_reprojection_mm": rms,
        "source_video": str(Path(video_path).resolve()), "source_frame": frame_index,
        "intrinsics_path": str(Path(intrinsics_path).resolve()),
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data
