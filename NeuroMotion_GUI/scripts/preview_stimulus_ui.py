"""Temporary BL/result desktop preview; never opens a serial port or changes source data."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.bl_ui import preview_bl
from neuromotion.gui import NeuroMotionApp
from neuromotion.replay import ReplayEngine
from neuromotion.session import write_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bl', type=Path, required=True)
    p.add_argument('--result', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--close-after', type=int, default=180)
    a = p.parse_args()
    app = NeuroMotionApp(); app.title('NeuroMotion · BL 与事件回放界面检查（回放为合成数据）')
    app.geometry('1500x980')
    errors = []
    app.report_callback_exception = lambda kind, value, tb: errors.append(f'{kind.__name__}: {value}')
    app.replay_page.set_engine(ReplayEngine(a.result))
    app.bl_csv.set(str(a.bl))
    app.bl_page.show_summary(preview_bl(a.bl)['summary'])
    def finish():
        write_json(a.output, {'status': 'passed' if not errors else 'failed', 'errors': errors,
                   'pages': len(app.book.tabs()), 'bl_events': len(app.bl_page.events),
                   'replay_events': len(app.replay_page.engine.events), 'synthetic_replay': True})
        app._close()
    app.protocol('WM_DELETE_WINDOW', finish)
    app.after(a.close_after*1000, finish)
    app.mainloop()


if __name__ == '__main__':
    main()
