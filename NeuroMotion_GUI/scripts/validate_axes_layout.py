"""Inspect actual Tk client geometry at four DPI-equivalent font scales.

This measures the widgets and text bounds; it is not a screenshot or a test
of changing Windows display settings. A successful aligned result can also
exercise the existing replay rendering path.
"""
from pathlib import Path
import argparse
import gc
import sys
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from neuromotion.axes import YAxisSettings
from neuromotion.axis_dialog import AxisSettingsDialog
from neuromotion.gui import NeuroMotionApp
from neuromotion.replay import ReplayEngine
from neuromotion.session import file_info, write_json


def check_traces(traces):
    descriptions = []
    for canvas in traces:
        labels = []
        for tag in ("title", "x_unit", "x_tick", "y_tick"):
            for item in canvas.find_withtag(tag):
                box = canvas.bbox(item)
                assert box[0] >= 0 and box[1] >= 0, (tag, box)
                assert box[2] <= canvas.winfo_width() and box[3] <= canvas.winfo_height(), (tag, box)
                labels.append({"kind": tag, "text": canvas.itemcget(item, "text"), "bbox": box})
        descriptions.append({"size": (canvas.winfo_width(), canvas.winfo_height()),
                             "plot_rect": canvas.plot_rect, "x_limits_s": canvas.current_x,
                             "labels": labels})
    assert len({trace.plot_rect[0] for trace in traces}) == 1
    assert len({trace.current_x for trace in traces}) == 1
    return descriptions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, help="Optional successful result to inspect in replay")
    parser.add_argument("--output", type=Path, default=Path("output/axes_20261105/layout.json"))
    args = parser.parse_args()
    records = []
    original_init = tk.Tk.__init__
    for scale in (1, 1.25, 1.5, 2):
        def init_at_scale(root, *positional, **keywords):
            original_init(root, *positional, **keywords)
            root.tk.call("tk", "scaling", 96 / 72 * scale)
            root.attributes("-alpha", 0)

        with patch.object(tk.Tk, "__init__", init_at_scale):
            app = NeuroMotionApp()
        try:
            app.geometry("1200x900")
            page = app.live_page
            t = np.arange(2000) / 1000
            values = np.column_stack((np.arange(len(t)), t, np.sin(t * 40) * 550000,
                                      np.cos(t * 50) * 650000, np.sin(t * 40), np.cos(t * 50),
                                      (t % 1 < .5).astype(int), np.zeros(len(t))))
            page.controller.history.extend(map(tuple, values))
            page.mode.set("原始")
            page.axes.channels = [YAxisSettings(unit="uV"), YAxisSettings(unit="uV")]
            app.update()
            page.refresh_plot()
            app.update_idletasks()
            entry = {"windows_scale_equivalent": scale, "tk_scaling": float(app.tk.call("tk", "scaling")),
                     "live": check_traces(page.traces)}
            assert all(trace.current_x == (0, 10) for trace in page.traces)
            dialog = AxisSettingsDialog(page)
            app.update_idletasks()
            entry["dialog_size"] = (dialog.winfo_width(), dialog.winfo_height())
            entry["screen_size"] = (app.winfo_screenwidth(), app.winfo_screenheight())
            assert dialog.winfo_width() <= app.winfo_screenwidth() - 30
            assert dialog.winfo_height() <= app.winfo_screenheight() - 60
            dialog.destroy()
            if args.result:
                app.replay_page.set_engine(ReplayEngine(args.result))
                app.update()
                app.replay_page.render(app.replay_page.engine.start + 2)
                app.update_idletasks()
                entry["replay"] = check_traces(app.replay_page.traces)
                assert any(trace.surface.find_withtag("waveform") for trace in app.replay_page.traces)
            records.append(entry)
        finally:
            app._close()
            del app, page, dialog
            gc.collect()
    root = Path(__file__).resolve().parents[1]
    evidence = {"status": "passed", "method": "actual Tk widget/font geometry; not Windows display-setting changes or screenshot review",
                "result": str(args.result.resolve()) if args.result else None, "scales": records,
                "sources": [file_info(root / "neuromotion" / filename) for filename in
                            ("axes.py", "axis_dialog.py", "plotting.py", "live_ui.py", "acquisition.py", "replay_ui.py")]}
    write_json(args.output, evidence)
    print(f"Four live/dialog/replay layouts passed: {args.output}")


if __name__ == "__main__":
    main()
