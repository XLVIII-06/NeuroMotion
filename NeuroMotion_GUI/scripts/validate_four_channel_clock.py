"""Known-time, drifting independent ADC clocks; never counts as hardware evidence."""
from pathlib import Path
import argparse
import json
import sys
import time

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from neuromotion.clock_alignment import ClockAlignedStream
from neuromotion.protocol_v2 import NativeSample, SyncEdge


def truth(t):
    t=np.asarray(t)/1e6
    def first(x): return 100*np.sin(2*np.pi*73*x)+30*np.sin(2*np.pi*181*x)
    def second(x): return 70*np.sin(2*np.pi*97*x)
    return np.column_stack((first(t),second(t),first(t-.012),second(t-.025)))


def validate(seconds):
    stream=ClockAlignedStream()
    stream.feed([SyncEdge(0,0,1)])
    total=int(seconds*1000)
    scale=2420000/(8388608*6)
    sums=np.zeros(4); truth_sums=np.zeros(4); valid_count=rows=0
    max_timing_us=0.; source_offset=[4000.,7470.]
    slope=1e-4/seconds
    started=time.perf_counter()
    for start in range(0,total,500):
        ids=np.arange(start,min(start+500,total));u=ids/1000
        ts=[source_offset[0]+u*1e6,source_offset[1]+1e6*(u/(1+50e-6)+.5*slope*u*u)]
        events=[]
        for d in (0,1):
            codes=np.rint(truth(ts[d])[:,d*2:d*2+2]/scale).astype(int)
            events.extend(NativeSample(d,int(n),int(round(t)),int(c[0]),int(c[1])) for n,t,c in zip(ids,ts[d],codes))
        events.sort(key=lambda e:e.time_us)
        blocks=stream.feed(events)
        if start+500>=total: blocks+=stream.finish()
        for b in blocks:
            rows+=len(b.time_us)
            mask=np.isfinite(b.values).all(axis=1)
            if not mask.any():continue
            tt=b.time_us[mask]; expected=truth(tt); actual=b.values[mask]
            sums+=np.sum((actual-expected)**2,axis=0);truth_sums+=np.sum(expected**2,axis=0)
            valid_count+=len(tt)
            for d in (0,1):
                n_est,_=stream.clocks[d].positions(tt)
                sec=(tt-source_offset[d])/1e6
                if d:
                    base=1/(1+50e-6)
                    n_true=1000*(2*sec/(base+np.sqrt(base*base+2*slope*sec)))
                else:n_true=1000*sec
                max_timing_us=max(max_timing_us,float(np.max(abs(n_est-n_true)))*1000)
        stream.pop_model_events()
        if start and start%300000==0:print(f"Validated {start/1000:g}/{seconds:g} simulated seconds",flush=True)
    relative_rms=np.sqrt(sums/truth_sums)
    reference=stream.clocks[1].reference
    fixed_error_us=None
    if reference:
        n=total-1
        predicted=reference['reference_time_us']+(n-reference['reference_index'])*reference['period_us']
        u=n/1000;true=source_offset[1]+1e6*(u/(1+50e-6)+.5*slope*u*u)
        fixed_error_us=predicted-true
    report={'status':'passed' if max_timing_us<=1000 and max(relative_rms)<=.01 else 'failed',
            'evidence_kind':'synthetic_known_truth_only','seconds':seconds,'native_events':stream.native_count,
            'grid_rows':rows,'valid_rows_all_channels':valid_count,'max_mapping_error_us':max_timing_us,
            'relative_rms_error_by_channel':relative_rms.tolist(),'fixed_reference_final_error_us':fixed_error_us,
            'preserved_activation_delays_s':[0,0,.012,.025],'wall_time_s':time.perf_counter()-started,
            'clock_status':stream.status(),'hardware_validated':False}
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--seconds',type=float,default=1800);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();r=validate(a.seconds);a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(r,indent=2),encoding='utf-8');print(json.dumps(r,indent=2))
    raise SystemExit(0 if r['status']=='passed' else 1)
