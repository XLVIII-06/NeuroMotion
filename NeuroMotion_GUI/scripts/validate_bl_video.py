"""Real BL/video diagnostic, explicitly without pretending a missing MCU recording exists."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd

from neuromotion.bl422 import load_bl422
from neuromotion.session import file_info, write_json
from neuromotion.stimulus_sync import TTLTrace, analog_ttl, fit_bl_clock
from neuromotion.timeline import iter_edges
from neuromotion.video import Rect, scan_video_led


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bl', type=Path, required=True)
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--led-roi', type=Rect.parse, required=True)
    parser.add_argument('--led-metric', choices=('mean', 'p99'), default='p99')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    trace = scan_video_led(args.video, args.led_roi, metric=args.led_metric, storage_dir=args.output, progress=print)
    try:
        frame_interval = float(np.median(np.diff(trace.time_s)))
        edges = list(iter_edges(trace.led_state, trace.time_s))
        master = TTLTrace(np.array([e.position for e in edges]), np.array([int(e.rising) for e in edges]))
        with load_bl422(args.bl) as recording:
            source, voltage = analog_ttl(recording)
            clock = fit_bl_clock(source, master, rms_limit_s=frame_interval, max_limit_s=frame_interval)
            trace.write_csv(args.output/'video_ttl.csv')
            common = clock.map([e['bl_time_s'] for e in recording.events])
            pd.DataFrame([{**e, 'video_time_s': t, 'diagnostic_only': True}
                          for e, t in zip(recording.events, common)]).to_csv(args.output/'events_in_video.csv', index=False)
            write_json(args.output/'bl_video_validation.json', {
                'status': 'passed_bl_video_only', 'three_source_validated': False,
                'absolute_stimulus_latency_validated': False, 'frame_interval_s': frame_interval,
                'bl_source': recording.metadata['source'], 'video_source': file_info(args.video),
                'led_roi': vars(args.led_roi), 'voltage': voltage, 'clock': clock.report})
    finally:
        trace.close()


if __name__ == '__main__':
    main()
