"""Prepare image evidence and blank annotations for the planned human review."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

from .calibration import Undistorter


def prepare_review(result_dir: str | Path, count: int = 30) -> dict:
    root = Path(result_dir).resolve()
    report = json.loads((root / "alignment_report.json").read_text(encoding="utf-8"))
    if report["status"] not in ("sync_validated", "full_fusion_generated"):
        raise ValueError("Only successful alignment results can be prepared for review")
    if count < 30:
        raise ValueError("The plan requires at least 30 review frames")
    target = root / "manual_review"
    if target.exists() and any(target.iterdir()):
        raise ValueError("Review directory is not empty; existing human annotations will be preserved")
    target.mkdir(parents=True, exist_ok=True)
    frames_dir = target / "frames"
    frames_dir.mkdir(exist_ok=True)
    frame_count = report["video"]["frame_count"]
    if frame_count < count:
        raise ValueError("Video has too few frames for the requested review")
    alignment = report["alignment"]
    # Spread review frames over the mapped EMG/video overlap.
    selected_session = next(item for item in report["emg_sessions"] if item["session_number"] == alignment["session_number"])
    first_t = (selected_session["sample_index_start"] - alignment["intercept_samples"]) / alignment["slope_samples_per_second"]
    last_t = (selected_session["sample_index_end"] - alignment["intercept_samples"]) / alignment["slope_samples_per_second"]
    eligible_first, eligible_last = None, None
    for block in pd.read_csv(root / "video_ttl.csv", chunksize=4096):
        eligible = block[(block.video_time_s >= first_t) & (block.video_time_s <= last_t)]
        if not eligible.empty:
            eligible_first = int(eligible.frame_index.iloc[0]) if eligible_first is None else eligible_first
            eligible_last = int(eligible.frame_index.iloc[-1])
    if eligible_first is None or eligible_last - eligible_first + 1 < count:
        raise ValueError("Aligned overlap has fewer than 30 frames")
    chosen = set(np.linspace(eligible_first, eligible_last, count, dtype=int).tolist())
    ttl = {}
    for block in pd.read_csv(root / "video_ttl.csv", chunksize=4096):
        for row in block[block.frame_index.isin(chosen)].to_dict("records"):
            ttl[int(row["frame_index"])] = row
    markers: dict[int, list[dict]] = {}
    if (root / "tracking.csv").is_file():
        for block in pd.read_csv(root / "tracking.csv", chunksize=4096):
            for row in block[block.frame_index.isin(chosen)].to_dict("records"):
                markers.setdefault(int(row["frame_index"]), []).append(row)
    video = Path(report["input_files"]["video"]["path"])
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise ValueError(f"Cannot open review video: {video}")
    width, height = report["video"]["width"], report["video"]["height"]
    undistorter = None
    if markers:
        undistorter = Undistorter(report["calibration"]["intrinsics"]["path"], (width, height))
    led = report["config"]["led_roi"]
    colors = [(255, 150, 0), (50, 220, 70), (255, 80, 200), (30, 220, 240), (220, 120, 100)]
    thumbs = []
    annotations = []
    index = 0
    try:
        while index <= max(chosen):
            ok, raw = capture.read()
            if not ok:
                raise ValueError(f"Cannot decode review frame {index}")
            if index not in chosen:
                index += 1
                continue
            frame = undistorter.apply(raw) if undistorter else raw.copy()
            row = ttl[index]
            time_s = float(row["video_time_s"])
            if not undistorter:
                cv2.rectangle(frame, (led["x"], led["y"]), (led["x"] + led["w"], led["y"] + led["h"]), (0, 200, 255), 2)
            for point in markers.get(index, []):
                if point["detected"]:
                    u, v = round(point["u_px"]), round(point["v_px"])
                    color = colors[int(point["particle_id"]) % len(colors)]
                    cv2.circle(frame, (u, v), 8, color, 1)
                    cv2.putText(frame, str(int(point["particle_id"])), (u + 9, v - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
            # Full-size review evidence retains coordinates; contact sheet is just an index.
            cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tofile(frames_dir / f"frame_{index:07d}.jpg")
            thumbnail = np.full((250, 340, 3), 245, dtype=np.uint8)
            scale = min(320 / width, 205 / height)
            small = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))))
            thumbnail[28:28 + small.shape[0], (340 - small.shape[1]) // 2:(340 - small.shape[1]) // 2 + small.shape[1]] = small
            cv2.putText(thumbnail, f"Frame {index} | t={time_s - alignment['video_start_s']:.3f}s | LED={int(row['led_state'])}", (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (30, 40, 60), 1)
            thumbs.append(thumbnail)
            for point in markers.get(index, [{"particle_id": ""}]):
                annotations.append({"frame_index": index, "video_time_s": time_s,
                                    "common_time_s": time_s - alignment["video_start_s"], "auto_led_state": int(row["led_state"]),
                                    **{key: point.get(key, "") for key in ("particle_id", "u_px", "v_px", "X_mm", "Y_mm", "detected", "quality_flag")},
                                    "reviewed_led_state": "", "id_ok": "", "position_ok": "", "manual_u_px": "", "manual_v_px": "", "notes": ""})
            index += 1
    finally:
        capture.release()
    columns = 5
    sheet = np.full(((len(thumbs) + columns - 1) // columns * 250, columns * 340, 3), 245, np.uint8)
    for i, thumb in enumerate(thumbs):
        sheet[(i // columns) * 250:(i // columns + 1) * 250, (i % columns) * 340:(i % columns + 1) * 340] = thumb
    cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tofile(target / "contact_sheet.jpg")
    with (target / "annotations.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(annotations[0]))
        writer.writeheader()
        writer.writerows(annotations)
    (target / "distance_checks.csv").write_text("frame_index,u1_px,v1_px,u2_px,v2_px,known_distance_mm,measured_distance_mm,notes\n", encoding="utf-8-sig")
    result = {"status": "pending_human_review", "frame_count": len(chosen), "annotation_rows": len(annotations),
              "coordinate_system": "undistorted_full_frame" if markers else "raw_frame",
              "full_fusion_validated": False, "source_video": str(video), "selected_frames": sorted(chosen)}
    (target / "review_status.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (target / "README.md").write_text(
        "# 人工抽查记录\n\n先看 contact_sheet.jpg，再逐张查看 frames/ 中的原尺寸图片。完整融合时图片坐标系为去畸变完整帧，与 tracking.csv 一致。LED 状态需回看原视频中的 LED ROI；去畸变图片不显示原始 LED ROI 框。\n\n"
        "在 annotations.csv 中填写 reviewed_led_state（0/1）、id_ok、position_ok（yes/no），必要时填写手工像素坐标和 notes。遮挡、错检、换 ID 均应记录，不能仅凭绿色检测标记判为通过。至少核查 30 个不同帧；五个点会生成 150 条待审行。\n\n"
        "在 distance_checks.csv 中记录同一平面上已知长度及软件换算的毫米距离。与棋盘格角点的重投影误差一起保存，并依据真实标注确定可接受的坐标误差。\n\n"
        "此目录只是待填写的质检包。生成图片和模板不会把真实全链路验收标记为通过。\n", encoding="utf-8")
    return result
