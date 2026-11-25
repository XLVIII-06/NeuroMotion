"""Chunk-validated, disk-backed four-channel signal sessions."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

from .signals import SignalInputError, SignalSession
from .storage import close_mapping

QUALITY_NAMES = ("valid", "warming", "support_gap", "native_gap", "spi_error", "status_error", "read_late",
                 "capture_error", "source_stalled", "source_gap", "filter_warming", "despike_replaced", "invalid_native")
QUALITY_CODES = {name:i for i,name in enumerate(QUALITY_NAMES)}
DTYPE = np.dtype([("segment","i8"),("index","i8"),("mcu","i8"),("common","f8"),("values","f8",(4,)),
                  ("filtered","f8",(4,)),("sync","i1"),("sync_valid","i1"),("revision","i8"),
                  ("quality","u1",(4,)),("filter_quality","u1",(4,))])


class SignalStorage:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="neuromotion_signals_")
        self.root = Path(self.temp.name)
        self.arrays = []

    def map(self, path, dtype, count, mode="r+"):
        a=np.memmap(path, dtype=dtype, shape=(count,), mode=mode)
        a._signal_storage = self  # Views retain the owner through their base map.
        self.arrays.append(a)
        return a

    def close(self):
        for a in self.arrays:
            try: close_mapping(a)
            except (OSError,ValueError): pass
        try:self.temp.cleanup()
        except (OSError,AttributeError): pass

    def __del__(self):
        self.close()


def load_four_sessions(path):
    path=Path(path)
    owner=SignalStorage(); raw=owner.root/'signals.bin'
    previous=None; bounds=[0]; segment_ids=[]; total=0
    with raw.open('wb') as output:
        for chunk in pd.read_csv(path,encoding='utf-8-sig',chunksize=4096):
            required=['format_version','segment_id','sample_index','mcu_time_us','common_time_s','sync','sync_valid',
                      'filter_revision','sample_rate_hz']+[f'ch{i}_{branch}_uv' for branch in ('aligned','filtered') for i in range(1,5)]
            if not set(required).issubset(chunk):raise SignalInputError('Incomplete four-channel CSV schema')
            try:
                numbers=chunk[required].apply(pd.to_numeric,errors='raise')
            except (ValueError,TypeError) as exc:raise SignalInputError(f'Invalid four-channel number: {exc}') from exc
            if not (numbers.format_version.eq(2).all() and numbers.sample_rate_hz.eq(1000).all()):
                raise SignalInputError('Unsupported four-channel format/rate')
            block=np.empty(len(chunk),dtype=DTYPE)
            for field,column in [('segment','segment_id'),('index','sample_index'),('mcu','mcu_time_us'),('revision','filter_revision')]:
                values=numbers[column].to_numpy(dtype=float)
                if not np.all(np.isfinite(values)&(values>=0)&(values<=2**53-1)&(values==np.floor(values))):
                    raise SignalInputError(f'Invalid integer {column}')
                block[field]=values.astype(np.int64)
            for field,column in [('sync','sync'),('sync_valid','sync_valid')]:
                if not numbers[column].isin([0,1]).all():raise SignalInputError(f'Invalid {column}')
                block[field]=numbers[column]
            block['common']=numbers.common_time_s
            if np.isinf(block['common']).any():raise SignalInputError('Infinite public time')
            for ch in range(4):
                for field,branch in [('values','aligned'),('filtered','filtered')]:
                    a=numbers[f'ch{ch+1}_{branch}_uv'].to_numpy(dtype=float)
                    if np.isinf(a).any():raise SignalInputError('Infinite four-channel measurement')
                    block[field][:,ch]=a
                for field,column in [('quality',f'quality_ch{ch+1}'),('filter_quality',f'filter_quality_ch{ch+1}')]:
                    if column not in chunk:raise SignalInputError(f'Missing {column}')
                    codes=chunk[column].map(QUALITY_CODES)
                    if codes.isna().any():raise SignalInputError(f'Unknown {column}')
                    block[field][:,ch]=codes
                if np.any((block['quality'][:,ch]==QUALITY_CODES['valid'])&~np.isfinite(block['values'][:,ch])):
                    raise SignalInputError('Valid quality cannot describe a missing channel value')
            # Chunk boundaries use the same validation as the interior.
            joined=np.r_[previous,block] if previous is not None else block
            changes=joined['segment'][1:]!=joined['segment'][:-1]
            if np.any(~changes & ((np.diff(joined['index'])!=1)|(np.diff(joined['mcu'])!=1000))):
                raise SignalInputError('Four-channel grid must be continuous in each segment')
            local=np.flatnonzero(changes)+1
            for i in local:
                boundary=total+int(i)-(1 if previous is not None else 0)
                if boundary not in bounds:bounds.append(boundary)
            if total==0:segment_ids.append(int(block['segment'][0]))
            segment_ids.extend(int(joined['segment'][i]) for i in local)
            previous=block[-1:].copy();block.tofile(output);total+=len(block)
    if not total:raise SignalInputError('EMG CSV is empty')
    if len(segment_ids)!=len(set(segment_ids)):raise SignalInputError('A segment ID cannot reappear later in the CSV')
    bounds.append(total)
    records=owner.map(raw,DTYPE,total)
    indices=owner.map(owner.root/'indices.bin','i8',total,'w+')
    positions=owner.map(owner.root/'positions.bin','f8',total,'w+')
    for start in range(0,total,4096):
        indices[start:start+4096]=records['index'][start:start+4096]
        positions[start:start+4096]=records['index'][start:start+4096]
    report_path=path.parent/'acquisition_report.json'
    report=json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else {}
    edge_path=path.parent/'sync_edges.csv'
    edges=pd.read_csv(edge_path) if edge_path.is_file() else None
    sessions=[]
    for number,(start,end) in enumerate(zip(bounds,bounds[1:])):
        r=records[start:end]; ids=indices[start:end]; pos=positions[start:end]
        anchor=None; valid_ttl=True
        for k in range(0,len(r),4096):
            b=r[k:k+4096];finite=np.isfinite(b['common'])
            if finite.any():
                anchors=b['mcu'][finite]-b['common'][finite]*1e6
                if anchor is None:anchor=float(anchors[0])
                if np.max(abs(anchors-anchor))>2:raise SignalInputError('Common time disagrees with the MCU clock')
            valid_ttl &= bool(b['sync_valid'].all())
        origin=int(r['mcu'][0])-int(ids[0])*1000
        edge_count=0
        if edges is not None:
            selected=edges[edges.segment_id==int(r['segment'][0])]
            if len(selected):
                et=selected.mcu_time_us.to_numpy(dtype=np.int64);levels=selected.level.to_numpy(dtype=np.int8)
                if np.any(np.diff(et)<=0) or not np.isin(levels,[0,1]).all():raise SignalInputError('Invalid TTL edge ledger')
                ei=selected.edge_index.to_numpy(dtype=np.int64)
                if np.any(np.diff(ei)!=1):valid_ttl=False
                where=np.searchsorted(r['mcu'],et)
                for j,index in enumerate(where):
                    if index==0 or index>=len(r):continue
                    if r['sync'][index]!=levels[j] or r['sync'][index-1]==levels[j]:
                        raise SignalInputError('Grid TTL disagrees with captured edge ledger')
                    pos[index]=(float(et[j])-origin)/1000
                    edge_count+=1
        meta={'sample_rate_hz':[1000.],'channel_count':4,'segment_id':int(r['segment'][0]),'value_type':'software_aligned_resampled',
              'storage':'disk_backed_chunked','recording_state':report.get('state','complete'),
              'recording_source':report.get('source',{}),'grid_origin_us':origin,'ttl_start_us':anchor,
              'sync_valid':valid_ttl,'hardware_edge_count':edge_count,'sync_edges_path':str(edge_path.resolve())}
        if anchor is not None:meta['master_start_sample']=(anchor-origin)/1000
        zeros=np.broadcast_to(np.zeros(1,dtype=np.int64),(len(r),))
        sessions.append(SignalSession(number,start,end,ids,r['values'][:,0],r['values'][:,1],r['sync'],zeros,zeros,
            'aligned_resampled',meta,'file_start' if number==0 else 'mcu_reset',r['filtered'][:,0],r['filtered'][:,1],
            r['revision'],r['values'],r['filtered'],r['quality'],r['filter_quality'],r['mcu'],r['common'],pos,owner))
    return sessions
