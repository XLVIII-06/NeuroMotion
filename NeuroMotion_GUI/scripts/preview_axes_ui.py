"""Open a labelled synthetic display fixture for manual layout inspection.

This does not connect hardware or write acquisition files. Close it normally.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from neuromotion.axes import YAxisSettings
from neuromotion.gui import NeuroMotionApp


app = NeuroMotionApp()
app.title("NeuroMotion · 坐标显示验证（模拟数据）")
app.geometry("1200x900")
page = app.live_page
times = np.arange(60000) / 1000
rows = np.column_stack((np.arange(len(times)), times, np.sin(times * 40) * 550000,
                        np.cos(times * 50) * 650000, np.sin(times * 40) * 70,
                        np.cos(times * 50) * 50, (times % 1 < .5).astype(int), np.zeros(len(times))))
page.controller.history.extend(map(tuple, rows))
page.mode.set("原始")
page.axes.channels = [YAxisSettings(unit="uV"), YAxisSettings(unit="uV")]
app.after(100, page.refresh_plot)
app.mainloop()
