"""Static alignment plot for review without loading full video frames."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .signals import SignalSession
from .timeline import Alignment
from .video import VideoTrace


def save_alignment_plot(path: str | Path, trace: VideoTrace, session: SignalSession, alignment: Alignment) -> None:
    step = max(1, len(session.sample_index) // 10000)
    emg_video_time = (session.sample_index[::step] - alignment.intercept_samples) / alignment.slope_samples_per_second
    video_step = max(1, len(trace.time_s) // 10000)
    figure, axes = plt.subplots(3, 1, figsize=(12, 8), constrained_layout=True)
    axes[0].step(trace.time_s[::video_step], trace.led_state[::video_step], where="post", label="LED video", lw=1.2)
    axes[0].step(emg_video_time, session.sync[::step] + 1.3, where="post", label="MCU sync + 1.3", lw=0.9)
    axes[0].axvline(alignment.video_start_s, color="green", ls="--", label="start")
    if alignment.video_end_s is not None:
        axes[0].axvline(alignment.video_end_s, color="red", ls="--", label="end")
    axes[0].set(title="TTL alignment across complete recording", ylabel="state (offset for visibility)")
    axes[0].legend(loc="upper right", ncol=4)
    first, last = np.searchsorted(trace.time_s, [max(0, alignment.video_start_s - 0.5), alignment.video_start_s + 6])
    emg_first, emg_last = np.searchsorted(emg_video_time, [max(0, alignment.video_start_s - 0.5), alignment.video_start_s + 6])
    axes[1].step(trace.time_s[first:last], trace.led_state[first:last], where="post", label="LED video", lw=1.2)
    axes[1].step(emg_video_time[emg_first:emg_last], session.sync[::step][emg_first:emg_last], where="post", label="MCU sync", lw=0.9, alpha=0.7)
    axes[1].set(xlim=(max(0, alignment.video_start_s - 0.5), alignment.video_start_s + 6),
                title="Start Barker code and first stable cycles", ylabel="state")
    axes[1].legend(loc="upper right")
    axes[2].plot(trace.time_s[::video_step], trace.led_score[::video_step], lw=0.8, label=f"LED {trace.metric} score")
    axes[2].axhline(trace.threshold, color="red", ls="--", label="threshold")
    axes[2].set(xlabel="Video time (s)", ylabel="brightness score", title="LED ROI contrast")
    axes[2].legend(loc="upper right")
    figure.savefig(path, dpi=150)
    plt.close(figure)
