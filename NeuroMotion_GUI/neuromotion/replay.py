"""Disk-indexed synchronized replay; seeking reads windows rather than CSV files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
import pandas as pd

from .calibration import Undistorter
from .session import file_info, write_json
from .storage import RecordBuffer, close_mapping
from .video import TRACK_DTYPE, QUALITY_NAMES


EMG_DTYPE = np.dtype([("sample_index", "i8"), ("common_time_s", "f8"), ("primary1", "f8"), ("primary2", "f8"),
                      ("raw1", "f8"), ("raw2", "f8"), ("filtered1", "f8"), ("filtered2", "f8"), ("sync", "i1"), ("revision", "i8")])
EMG4_DTYPE = np.dtype(EMG_DTYPE.descr + [("primary3","f8"),("primary4","f8"),("raw3","f8"),("raw4","f8"),
                                      ("filtered3","f8"),("filtered4","f8")]
                     + [(f"quality{i}","u1") for i in range(1,5)] + [(f"filter_quality{i}","u1") for i in range(1,5)])
FRAME_DTYPE = np.dtype([("frame_index", "i8"), ("video_time_s", "f8"), ("common_time_s", "f8"), ("led_state", "i1")])


def _stamp(path: Path):
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _fingerprint(report):
    return hashlib.sha256(json.dumps({key: report.get(key) for key in ("alignment", "input_files", "calibration", "status")}, sort_keys=True).encode()).hexdigest()


def _flush_array(buffer: RecordBuffer, output: Path):
    array = buffer.finish()
    try:
        np.save(output, array)
        return len(array)
    finally:
        close_mapping(array)
        if buffer.path:
            buffer.path.unlink(missing_ok=True)


def build_replay_index(result_dir: str | Path, progress=lambda _: None, cancel_event=None) -> dict:
    root = Path(result_dir).resolve()
    if (root/'result_manifest.json').is_file():
        from .result_store import build_derived_index
        return build_derived_index(root, progress, cancel_event)
    report = json.loads((root / "alignment_report.json").read_text(encoding="utf-8"))
    if report["status"] not in ("sync_validated", "full_fusion_generated"):
        raise ValueError("Replay needs a successful alignment result")
    index = root / "replay_index"
    manifest_path = index / "manifest.json"
    if manifest_path.is_file():
        cached = json.loads(manifest_path.read_text(encoding="utf-8"))
        if cached.get("schema_version") == 4 and cached["fingerprint"] == _fingerprint(report) and all(_stamp(Path(item["path"])) == item for item in cached["sources"]):
            return cached
    index.mkdir(exist_ok=True)
    data_dir = index / ("data_" + uuid4().hex)
    data_dir.mkdir()
    sources = []
    for key in ("emg_csv", "video", *(["sync_edges"] if "sync_edges" in report["input_files"] else [])):
        path = Path(report["input_files"][key]["path"])
        progress(f"校验回放来源：{path.name}")
        if file_info(path)["sha256"] != report["input_files"][key]["sha256"]:
            raise ValueError(f"Replay source changed after alignment: {path}")
        sources.append(_stamp(path))
    if "calibration" in report:
        for info in report["calibration"].values():
            path = Path(info["path"])
            if file_info(path)["sha256"] != info["sha256"]:
                raise ValueError("Replay calibration changed after alignment")
            sources.append(_stamp(path))
    session = next(item for item in report["emg_sessions"] if item["session_number"] == report["alignment"]["session_number"])
    a = report["alignment"]["slope_samples_per_second"]
    fitted_start = report["alignment"]["fitted_start_sample"]
    channel_count = session.get("metadata",{}).get("channel_count",2)
    buffer = RecordBuffer(EMG4_DTYPE if channel_count == 4 else EMG_DTYPE, data_dir / "emg.bin")
    offset = 0
    raw_available = False
    filtered_available = False
    reader = pd.read_csv(report["input_files"]["emg_csv"]["path"], encoding="utf-8-sig", chunksize=8192)
    try:
        progress("建立 EMG 随机读取索引")
        for block in reader:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Replay indexing cancelled")
            start, stop = max(0, session["source_row_start"] - offset), min(len(block), session["source_row_end"] - offset)
            offset += len(block)
            if stop <= start:
                continue
            block = block.iloc[start:stop]
            if channel_count == 4:
                from .signals_v2 import QUALITY_CODES
                ids=block.sample_index.to_numpy(dtype=np.int64)
                values=block[[f"ch{i}_aligned_uv" for i in range(1,5)]].to_numpy(dtype=float)
                filtered=block[[f"ch{i}_filtered_uv" for i in range(1,5)]].to_numpy(dtype=float)
                common=block.common_time_s.to_numpy(dtype=float)
                quality=block[[f"quality_ch{i}" for i in range(1,5)]].apply(lambda a:a.map(QUALITY_CODES)).to_numpy(dtype=np.uint8)
                fq=block[[f"filter_quality_ch{i}" for i in range(1,5)]].apply(lambda a:a.map(QUALITY_CODES)).to_numpy(dtype=np.uint8)
                filtered_available=True
                for j in range(len(block)):
                    buffer.append((ids[j],common[j],values[j,0],values[j,1],np.nan,np.nan,filtered[j,0],filtered[j,1],
                                   int(block.sync.iloc[j]),int(block.filter_revision.iloc[j]),values[j,2],values[j,3],np.nan,np.nan,
                                   filtered[j,2],filtered[j,3],*quality[j],*fq[j]))
                if offset >= session["source_row_end"]:break
                continue
            index_key = "sample_index" if "sample_index" in block else "sampleIndex"
            raw_available = {"ch1_raw_uv", "ch2_raw_uv"}.issubset(block.columns)
            recorded = ("ch1_uv", "ch2_uv") if "ch1_uv" in block else ("ch1", "ch2")
            filtered_available = all(key in block for key in recorded)
            keys = ("ch1_raw_uv", "ch2_raw_uv") if raw_available else recorded
            ids = block[index_key].to_numpy(dtype=np.int64)
            one = block[keys[0]].to_numpy(dtype=float)
            two = block[keys[1]].to_numpy(dtype=float)
            filtered1 = block[recorded[0]].to_numpy(dtype=float) if filtered_available else np.full(len(block), np.nan)
            filtered2 = block[recorded[1]].to_numpy(dtype=float) if filtered_available else np.full(len(block), np.nan)
            revisions = block["filter_revision"].to_numpy(dtype=np.int64) if "filter_revision" in block else np.full(len(block), -1)
            sync = block["sync"].to_numpy(dtype=np.int8)
            for i in range(len(block)):
                buffer.append((ids[i], (ids[i] - fitted_start) / a, one[i], two[i], one[i] if raw_available else np.nan,
                               two[i] if raw_available else np.nan, filtered1[i], filtered2[i], sync[i], revisions[i]))
            if offset >= session["source_row_end"]:
                break
        emg_count = _flush_array(buffer, data_dir / "emg.npy")
    except BaseException:
        buffer.close()
        raise
    finally:
        reader.close()
    buffer = RecordBuffer(FRAME_DTYPE, data_dir / "frames.bin")
    frame_file = root / "frame_alignment.csv"
    try:
        for block in pd.read_csv(frame_file, chunksize=8192):
            for row in block.itertuples(index=False):
                buffer.append((row.frame_index, row.video_time_s, row.common_time_s, row.led_state))
        frames_count = _flush_array(buffer, data_dir / "frames.npy")
    except BaseException:
        buffer.close()
        raise
    sources.append(_stamp(frame_file))
    # searchsorted copies a strided structured field. Separate contiguous time
    # columns keep each seek logarithmic and avoid reading the whole EMG index.
    for name, count in (("emg", emg_count), ("frames", frames_count)):
        source = np.load(data_dir / f"{name}.npy", mmap_mode="r")
        times = np.lib.format.open_memmap(data_dir / f"{name}_time.npy", mode="w+", dtype="f8", shape=(count,))
        try:
            for start in range(0, count, 8192):
                times[start:start + 8192] = source["common_time_s"][start:start + 8192]
        finally:
            close_mapping(times)
            close_mapping(source)
    marker_count = 0
    track_file = root / "tracking.csv"
    if track_file.is_file():
        progress("建立逐帧五点位置索引")
        buffer = RecordBuffer(TRACK_DTYPE, data_dir / "tracking.bin")
        row_count = 0
        try:
            for block in pd.read_csv(track_file, chunksize=8192):
                marker_count = max(marker_count, int(block.particle_id.max()) + 1)
                for row in block.itertuples(index=False):
                    buffer.append((row.frame_index, row.particle_id, row.u_px, row.v_px, row.X_mm, row.Y_mm,
                                   row.detected, QUALITY_NAMES.index(row.quality_flag)))
                    row_count += 1
            _flush_array(buffer, data_dir / "tracking.npy")
        except BaseException:
            buffer.close()
            raise
        if row_count != marker_count * frames_count:
            raise ValueError("Tracking table does not contain one row per marker/frame")
        sources.append(_stamp(track_file))
    manifest = {"schema_version": 4, "fingerprint": _fingerprint(report), "data_dir": data_dir.name, "channel_count":channel_count,
                "sources": sources, "emg_count": emg_count, "frame_count": frames_count,
                "marker_count": marker_count, "raw_available": raw_available, "filtered_available": filtered_available}
    write_json(manifest_path, manifest)
    return manifest


class ReplayEngine:
    def __init__(self, result_dir: str | Path, progress=lambda _: None, cancel_event=None):
        self.root = Path(result_dir).resolve()
        if (self.root/'result_manifest.json').is_file():
            from .result_store import replay_report
            self.report = replay_report(self.root)
        else:
            self.report = json.loads((self.root / "alignment_report.json").read_text(encoding="utf-8"))
        self.manifest = build_replay_index(self.root, progress, cancel_event)
        data = self.root / "replay_index" / self.manifest["data_dir"]
        self.emg = np.load(data / "emg.npy", mmap_mode="r")
        self.frames = np.load(data / "frames.npy", mmap_mode="r")
        self.emg_times = np.load(data / "emg_time.npy", mmap_mode="r")
        self.frame_times = np.load(data / "frames_time.npy", mmap_mode="r")
        self.tracking = np.load(data / "tracking.npy", mmap_mode="r") if self.manifest["marker_count"] else None
        times = self.frame_times
        first = max(float(times[0]), float(self.emg_times[0]))
        last = min(float(times[-1]), float(self.emg_times[-1]))
        if self.manifest.get('common_coverage_s'):
            first = max(first, self.manifest['common_coverage_s'][0])
            last = min(last, self.manifest['common_coverage_s'][1])
        self.first_frame = int(np.searchsorted(times, first, side="left"))
        self.last_frame = int(np.searchsorted(times, last, side="right")) - 1
        if self.last_frame < self.first_frame:
            self.close()
            raise ValueError("No replay overlap between EMG and video")
        self.start = float(times[self.first_frame])
        self.end = float(times[self.last_frame])
        self.capture = cv2.VideoCapture(self.report["input_files"]["video"]["path"])
        if not self.capture.isOpened():
            self.close()
            raise ValueError("Replay video could not be opened")
        self.undistorter = None
        if self.tracking is not None and self.report.get('calibration', {}).get('intrinsics'):
            self.undistorter = Undistorter(self.report["calibration"]["intrinsics"]["path"], (self.report["video"]["width"], self.report["video"]["height"]))
        from .result_store import read_replay_events
        self.events = read_replay_events(self.root)
        self.current_frame = -2
        self.cached_image = None

    def seek(self, common_time_s: float, window_s: float = 5.0) -> dict:
        if not np.isfinite(common_time_s) or not np.isfinite(window_s) or not 0 < window_s <= 60:
            raise ValueError("Replay time/window is invalid")
        t = float(np.clip(common_time_s, self.start, self.end))
        times = self.frame_times
        right = int(np.searchsorted(times, t))
        candidates = [int(np.clip(i, self.first_frame, self.last_frame)) for i in (right - 1, right)]
        frame_index = min(candidates, key=lambda i: abs(times[i] - t))
        return self.seek_frame(frame_index, window_s)

    def seek_frame(self, frame_index: int, window_s: float = 5.0) -> dict:
        index = int(np.clip(frame_index, self.first_frame, self.last_frame))
        frame = self.frames[index]
        t = float(frame["common_time_s"])
        positions = []
        if self.tracking is not None:
            count = self.manifest["marker_count"]
            for row in self.tracking[index * count:(index + 1) * count]:
                if int(row["frame_index"]) != index:
                    raise ValueError("Replay tracking order does not match video frames")
                positions.append({key: row[key].item() for key in TRACK_DTYPE.names if key != "quality"} | {"quality_flag": QUALITY_NAMES[int(row["quality"])]})
        if index != self.current_frame:
            if index != self.current_frame + 1:
                self.capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, image = self.capture.read()
            if not ok:
                raise ValueError(f"Cannot decode video frame {index}")
            if self.undistorter:
                image = self.undistorter.apply(image)
            for point in positions:
                if point["detected"]:
                    color = (0, 200, 0) if point["quality_flag"] == "observed" else (0, 160, 255)
                    location = round(point["u_px"]), round(point["v_px"])
                    cv2.circle(image, location, 7, color, 2)
                    label = str(point["particle_id"]) + ("?" if point["quality_flag"] != "observed" else "")
                    cv2.putText(image, label, (location[0] + 9, location[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
            self.cached_image, self.current_frame = image, index
        emg_times = self.emg_times
        start, stop = np.searchsorted(emg_times, [t - window_s / 2, t + window_s / 2])
        window = self.emg[start:stop]
        # Keep extrema on all available analog branches while limiting rendering cost.
        if len(window) > 5000:
            fields=tuple(f"{branch}{i}" for branch in ("primary","filtered") for i in range(1,self.manifest.get("channel_count",2)+1))
            bins=min(400,max(1,4800//(2*len(fields))))
            chunk = max(1, len(window) // bins)
            selected = {0, len(window) - 1}
            for field in fields:
                edges=np.flatnonzero(np.diff(np.isfinite(window[field])))+1
                selected.update(edges.tolist());selected.update((edges-1).tolist())
            for first in range(0, len(window), chunk):
                for field in fields:
                    values = window[field][first:first + chunk]
                    if np.isfinite(values).any():
                        selected.update((first + int(np.nanargmin(values)), first + int(np.nanargmax(values))))
            window = window[sorted(selected)]
        events = [e for e in self.events if e['common_time_s'] is not None and abs(e['common_time_s']-t) <= window_s/2]
        return {"frame_index": index, "video_time_s": float(frame["video_time_s"]), "common_time_s": t,
                "emg_sample_float": self.report["alignment"]["slope_samples_per_second"] * float(frame["video_time_s"]) + self.report["alignment"]["intercept_samples"],
                "led_state": int(frame["led_state"]), "positions": positions, "image": self.cached_image, "emg_window": window.copy(),
                "channel_count": self.manifest.get("channel_count",2), "value_type": "software_aligned_resampled" if self.manifest.get("channel_count",2)==4 else "legacy",
                'events': events}

    def close(self):
        if hasattr(self, "capture"):
            self.capture.release()
        for name in ("emg", "frames", "tracking", "emg_times", "frame_times"):
            close_mapping(getattr(self, name, None))
