"""Native dual ADC packets -> acquisition -> calibrated video -> four-channel replay."""
from dataclasses import replace
import csv
import gc
import json
import math
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from neuromotion.acquisition import AcquisitionController,BinaryReplaySource
from neuromotion.filters import FilterSettings
from neuromotion.pipeline import run_pipeline
from neuromotion.protocol_v2 import BOOT,SAMPLES,SYNC_EDGE,NativeSample,SyncEdge,encode_packet
from neuromotion.replay import ReplayEngine
from neuromotion.signals import load_signal_sessions
from neuromotion.simulated_v2 import simulation_metadata
from neuromotion.validation import make_synthetic_inputs,ttl_state


def make_wire(path):
    events=[];last=0;edge_id=0
    for t in np.arange(.5,15,.5):
        state=ttl_state(float(t))
        if state!=last:
            events.append(SyncEdge(edge_id,round(t*1e6),state));edge_id+=1;last=state
    scale=2420000/(8388608*6)
    for d in (0,1):
        for n in range(14496):
            if d and 5000<=n<5010:continue
            t=504000+d*270+n*1000/(1+d*50e-6)
            a=round(70*math.sin(2*math.pi*(73+10*d)*t/1e6)/scale)
            b=round(45*math.cos(2*math.pi*(41+20*d)*t/1e6)/scale)
            events.append(NativeSample(d,n,round(t),a,b))
    events.sort(key=lambda e:e.time_us)
    meta=simulation_metadata();meta['ttl_start_us']=500000
    seq=0;pending=[]
    with path.open('wb') as f:
        f.write(encode_packet(BOOT,meta,seq));seq+=1
        for event in events:
            if isinstance(event,SyncEdge):
                if pending:f.write(encode_packet(SAMPLES,pending,seq));seq+=1;pending=[]
                f.write(encode_packet(SYNC_EDGE,event,seq));seq+=1
            else:
                pending.append(event)
                if len(pending)==8:f.write(encode_packet(SAMPLES,pending,seq));seq+=1;pending=[]
        if pending:f.write(encode_packet(SAMPLES,pending,seq))


class FourIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        persistent=os.environ.get('NEUROMOTION_FOUR_FIXTURE')
        if persistent:
            cls.temp=None;cls.root=Path(persistent);cls.root.mkdir(parents=True,exist_ok=False)
        else:
            cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        config=make_synthetic_inputs(cls.root/'fixture',30,1000)
        binary=cls.root/'native.bin';make_wire(binary)
        c=AcquisitionController(FilterSettings(high_pass_hz=20,low_pass_hz=200))
        c.connect(source=BinaryReplaySource(binary,speed=0));c.start_recording(cls.root/'session');c.wait()
        cls.emg=cls.root/'session/acquisition/emg.csv'
        cls.config=replace(config,emg_csv=cls.emg,output_dir=cls.root/'result')
        cls.report=run_pipeline(cls.config)

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        if cls.temp:cls.temp.cleanup()

    def test_four_channel_fusion_uses_mcu_time_and_preserves_individual_gaps(self):
        sample=pd.read_csv(self.config.output_dir/'aligned_by_emg_sample.csv')
        self.assertIn('ch4_aligned_uv',sample)
        self.assertIn('ch4_filtered_uv',sample)
        self.assertIn('emg_storage',self.report['processing'])
        self.assertEqual(self.report['processing']['emg_storage'],'disk_backed_chunked')
        self.assertTrue(np.allclose(sample.common_time_s,(sample.mcu_time_us-500000)/1e6))
        gap=sample[(sample.common_time_s>5.01)&(sample.common_time_s<5.03)]
        self.assertTrue(gap.ch1_aligned_uv.notna().all())
        self.assertTrue(gap.ch3_aligned_uv.isna().any())
        self.assertTrue((gap.quality_ch3=='native_gap').any())
        frames=pd.read_csv(self.config.output_dir/'aligned_by_frame.csv')
        self.assertIn('emg_ch4_aligned_uv_at_frame',frames)
        self.assertIn('emg_ch4_filtered_value_type',frames)
        self.assertLess(self.report['alignment']['max_residual_s'],1/30)

    def test_replay_has_all_four_channels_and_missing_sections(self):
        engine=ReplayEngine(self.config.output_dir)
        try:
            self.assertEqual(engine.manifest['channel_count'],4)
            snap=engine.seek(5.02)
            self.assertEqual(len(snap['positions']),5)
            self.assertIn('primary4',snap['emg_window'].dtype.names)
            self.assertIn('quality3',snap['emg_window'].dtype.names)
            self.assertTrue(np.isnan(snap['emg_window']['primary3']).any())
        finally:engine.close()

    def test_loader_time_anchor_and_native_value_semantics(self):
        sessions=load_signal_sessions(self.emg)
        try:
            s=sessions[0]
            self.assertEqual(s.channel_count,4)
            self.assertFalse(s.raw_available)
            self.assertEqual(s.metadata['ttl_start_us'],500000)
            self.assertTrue(s.sample_index.flags.c_contiguous)
            self.assertTrue(np.isnan(s.aligned_channels_uv).any())
        finally:s.storage_owner.close()


if __name__=='__main__':unittest.main()
