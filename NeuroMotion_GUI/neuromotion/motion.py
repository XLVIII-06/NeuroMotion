"""Stateful planar marker tracker shared by standalone and integrated video jobs."""

from pathlib import Path
import numpy as np
from scipy.optimize import linear_sum_assignment

from .calibration import PlaneTransform, Undistorter
from .storage import RecordBuffer
from .video import Rect, TRACK_DTYPE, TrackingData, VideoInputError, _detect_markers


class MarkerTracker:
    def __init__(self, frame_size: tuple[int, int], roi: Rect, marker_count: int = 5,
                 intrinsics_path=None, homography_path=None, threshold=200, min_area=5.0, max_area=6000.0,
                 min_circularity=0.5, search_range_px=50.0, memory_frames=5,
                 max_intrinsics_rms_px=2.0, max_homography_rms_mm=1.0, storage_dir: Path | None = None):
        if marker_count < 1 or not 0 <= threshold <= 255 or min_area <= 0 or max_area < min_area or search_range_px <= 0 or memory_frames < 0:
            raise VideoInputError("Invalid marker tracking parameters")
        roi.check(*frame_size)
        if homography_path and not intrinsics_path:
            raise VideoInputError("Homography requires the corresponding camera intrinsics")
        self.undistorter = Undistorter(intrinsics_path, frame_size) if intrinsics_path else None
        self.plane = PlaneTransform(homography_path, frame_size) if homography_path else None
        if self.undistorter and (not np.isfinite(self.undistorter.rms_error_px) or self.undistorter.rms_error_px > max_intrinsics_rms_px):
            raise VideoInputError(f"Intrinsics RMS error is missing or exceeds {max_intrinsics_rms_px} px")
        if self.plane and (not np.isfinite(self.plane.rms_error_mm) or self.plane.rms_error_mm > max_homography_rms_mm):
            raise VideoInputError(f"Homography RMS error is missing or exceeds {max_homography_rms_mm} mm")
        self.coordinate_system = "undistorted_full_frame" if self.undistorter else "raw_frame"
        self.roi, self.count = roi, marker_count
        self.threshold, self.min_area, self.max_area, self.circularity = threshold, min_area, max_area, min_circularity
        self.search, self.memory = search_range_px, memory_frames
        self.positions = [None] * marker_count
        self.last_seen = [-1] * marker_count
        self.uncertain = [False] * marker_count
        self.buffer = RecordBuffer(TRACK_DTYPE, storage_dir / "tracking.bin" if storage_dir else None)
        self.frames = 0

    def process(self, frame_index: int, raw_frame: np.ndarray):
        if frame_index != self.frames:
            raise VideoInputError("Marker frames are out of order")
        frame = self.undistorter.apply(raw_frame) if self.undistorter else raw_frame
        found = _detect_markers(frame, self.roi, threshold=self.threshold, min_area=self.min_area,
                                max_area=self.max_area, min_circularity=self.circularity,
                                candidate_limit=max(2 * self.count, self.count + 3))
        assigned = {}
        active = [i for i in range(self.count) if self.positions[i] is not None and frame_index - self.last_seen[i] <= self.memory + 1]
        if active and found:
            costs = np.full((len(active), len(found)), 1e9)
            for row, marker in enumerate(active):
                for candidate, (x, y, _) in enumerate(found):
                    distance = float(np.hypot(x - self.positions[marker][0], y - self.positions[marker][1]))
                    if distance <= self.search:
                        costs[row, candidate] = distance
            rows, columns = linear_sum_assignment(costs)
            for row, column in zip(rows, columns):
                if costs[row, column] < 1e9:
                    assigned[active[row]] = int(column)
        used = set(assigned.values())
        free = [i for i in range(self.count) if i not in assigned and (self.positions[i] is None or frame_index - self.last_seen[i] > self.memory + 1)]
        remaining = sorted((i for i in range(len(found)) if i not in used), key=lambda i: found[i][0])
        for marker, candidate in zip(free, remaining):
            assigned[marker] = candidate
        for marker in range(self.count):
            if marker not in assigned:
                self.buffer.append((frame_index, marker, np.nan, np.nan, np.nan, np.nan, False, 0))
                continue
            x, y, _ = found[assigned[marker]]
            self.uncertain[marker] |= self.positions[marker] is not None and frame_index - self.last_seen[marker] > self.memory + 1
            X, Y = self.plane.apply(x, y) if self.plane else (np.nan, np.nan)
            self.buffer.append((frame_index, marker, x, y, X, Y, True, 2 if self.uncertain[marker] else 1))
            self.positions[marker], self.last_seen[marker] = (x, y), frame_index
        self.frames += 1

    def finish(self) -> TrackingData:
        return TrackingData(self.buffer.finish(), self.count)

    def abort(self):
        self.buffer.close()
