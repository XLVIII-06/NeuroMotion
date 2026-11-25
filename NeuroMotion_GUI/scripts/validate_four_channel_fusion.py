"""Retain the known-truth four-channel/video/calibration/replay test artifacts."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve()
    if out.exists():p.error('Choose a new output directory')
    out.parent.mkdir(parents=True,exist_ok=True);log=out.parent/(out.name+'_tests.log')
    root=Path(__file__).resolve().parents[1]
    with log.open('w',encoding='utf-8') as f:
        r=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_four_channel_integration.py','-v'],
                         cwd=root,env={**os.environ,'NEUROMOTION_FOUR_FIXTURE':str(out)},stdout=f,stderr=subprocess.STDOUT)
    report={'status':'passed' if r.returncode==0 else 'failed','evidence_kind':'synthetic_four_channel_full_fusion',
            'log':str(log),'result_dir':str(out/'result'),'hardware_validated':False}
    if out.exists():(out/'validation_report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
    return r.returncode


if __name__=='__main__':raise SystemExit(main())
