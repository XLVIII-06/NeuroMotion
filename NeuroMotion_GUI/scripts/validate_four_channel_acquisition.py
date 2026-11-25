"""Long real acquisition code path with a labelled synthetic v2 source.

Checks raw conservation, grid continuity, filter changes and bounded history.
Never marks dual-ADC hardware or analog synchronization as validated.
"""
from pathlib import Path
import argparse
import csv
import json
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from neuromotion.acquisition import AcquisitionController
from neuromotion.filters import FilterSettings
from neuromotion.protocol_v2 import SAMPLES,V2Decoder
from neuromotion.simulated_v2 import SimulatedFourChannelSource


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--seconds',type=float,default=1800);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    c=AcquisitionController();started=time.perf_counter()
    c.connect(source=SimulatedFourChannelSource(a.seconds,speed=0));c.start_recording(a.output/'session')
    changed=False;history_checks=0;max_history=0;last_report=0
    while c.thread.is_alive():
        state=c.snapshot(max_points=2000,plot_window_s=.05)
        history_checks+=1;max_history=max(max_history,len(c.history))
        if state['samples']>100000 and not changed:
            c.update_filters(FilterSettings(high_pass_hz=20,low_pass_hz=200,notch_50hz=True));changed=True
        if state['samples']-last_report>200000:
            last_report=state['samples'];print(f"Native events {last_report}, grid rows {state['recorded_samples']}",flush=True)
            frozen=c.snapshot(max_points=60000)['plot'].copy()
            assert len(frozen)<=60000
        c.thread.join(.1)
    c.wait()
    wall=time.perf_counter()-started
    folder=a.output/'session/acquisition';decoder=V2Decoder();native_count=0
    with (folder/'serial.bin').open('rb') as binary,(folder/'native_samples.csv').open(newline='') as csvfile:
        native=csv.DictReader(csvfile)
        while True:
            data=binary.read(65536)
            if not data:break
            for packet in decoder.feed(data):
                if packet.kind!=SAMPLES:continue
                for event in packet.value:
                    row=next(native)
                    actual=tuple(int(row[k]) for k in ('device','native_index','drdy_time_us','ch1_code','ch2_code','adc_status','flags','known_dropped'))
                    expected=(event.device,event.index,event.time_us,event.code1,event.code2,event.status,event.flags,event.dropped)
                    if actual!=expected:raise AssertionError((native_count,actual,expected))
                    native_count+=1
        assert next(native,None) is None
    count=0;previous=None;nan_counts=np.zeros(4,dtype=np.int64);revisions=set()
    columns=['sample_index','mcu_time_us','filter_revision']+[f'ch{i}_aligned_uv' for i in range(1,5)]
    for block in pd.read_csv(folder/'emg.csv',usecols=columns,chunksize=8192):
        ids=block.sample_index.to_numpy(dtype=np.int64);ticks=block.mcu_time_us.to_numpy(dtype=np.int64)
        assert (np.diff(ids)==1).all() and (np.diff(ticks)==1000).all()
        if previous is not None:assert (ids[0],ticks[0])==(previous[0]+1,previous[1]+1000)
        previous=(int(ids[-1]),int(ticks[-1]));count+=len(block)
        nan_counts+=np.isnan(block[[f'ch{i}_aligned_uv' for i in range(1,5)]].to_numpy()).sum(axis=0)
        revisions.update(block.filter_revision.unique())
    state=c.snapshot(include_plot=False)
    assert native_count==state['samples']==state['native_recorded_samples']
    assert count==state['recorded_samples'] and count>=a.seconds*1000-10
    assert not state['error'] and not state['index_gaps'] and not decoder.frame_errors and not decoder.sequence_gaps
    assert max_history<=60000 and max(nan_counts)<100
    if changed:assert len(revisions)>=2
    result={'status':'passed','evidence_kind':'synthetic_v2_through_production_acquisition','duration_s':a.seconds,
            'native_records':native_count,'aligned_rows':count,'binary_native_csv_exact':True,'nan_rows_by_channel':nan_counts.tolist(),
            'filter_revisions':sorted(int(r) for r in revisions),'snapshot_checks':history_checks,'history_max_rows':max_history,
            'frame_errors':decoder.frame_errors,'packet_gaps':decoder.sequence_gaps,'wall_time_s':wall,
            'speed_vs_realtime':a.seconds/wall,'hardware_validated':False}
    (a.output/'report.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result,indent=2))


if __name__=='__main__':main()
