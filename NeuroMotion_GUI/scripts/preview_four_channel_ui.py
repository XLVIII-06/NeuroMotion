"""Visible source GUI preview, clearly labelled synthetic and auto-finalized."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from neuromotion.gui import NeuroMotionApp
from neuromotion.simulated_v2 import SimulatedFourChannelSource
from neuromotion.replay import ReplayEngine


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--close-after',type=int,default=180);p.add_argument('--result',type=Path);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    app=NeuroMotionApp();app.title('NeuroMotion · 四通道模拟检查（非硬件）')
    page=app.live_page;page.kind.set('四通道模拟');page.controller.connect(source=SimulatedFourChannelSource(600,speed=1))
    page.controller.start_recording(a.output/'session')
    errors=[]
    app.report_callback_exception=lambda kind,value,tb:errors.append(f'{kind.__name__}: {value}')
    def finish():
        page.controller.disconnect()
        state=page.controller.snapshot(include_plot=False)
        (a.output/'desktop_report.json').write_text(json.dumps({'errors':errors,'channel_count':state['channel_count'],
            'received_native':state['samples'],'saved_aligned':state['recorded_samples'],'trace_count':len(page.traces)},indent=2),encoding='utf-8')
        app._close()
    if a.result:
        def replay():
            page.controller.disconnect()
            app.replay_page.set_engine(ReplayEngine(a.result))
        app.after(90000,replay)
    app.after(a.close_after*1000,finish)
    app.mainloop()


if __name__=='__main__':main()
