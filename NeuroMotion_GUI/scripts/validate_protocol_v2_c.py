"""Compile the actual MCU codec as a freestanding host DLL and compare bytes."""
from pathlib import Path
import argparse
import ctypes as c
import json
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from neuromotion.protocol_v2 import NativeSample, SAMPLES, encode_packet


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--clang',type=Path,required=True);p.add_argument('--lld',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
    core=Path(__file__).resolve().parents[2]/'NeuroMotion_MCU/ads1292_f446re/Core'
    (out/'string.h').write_text('#include <stddef.h>\nvoid *memcpy(void *,const void *,size_t);\n')
    (out/'shim.c').write_text('#include <stddef.h>\nvoid *memcpy(void *a,const void *b,size_t n){unsigned char *d=a;const unsigned char *s=b;while(n--)*d++=*s++;return a;}\n')
    objs=[]
    for source in (core/'Src/nm_protocol_v2.c',out/'shim.c'):
        obj=out/(source.stem+'.obj');objs.append(obj)
        subprocess.run([str(a.clang),'-ffreestanding','-fno-builtin','-c',str(source),'-I'+str(core/'Inc'),'-I'+str(out),'-o',str(obj)],check=True,capture_output=True)
    dll=out/'codec.dll'
    subprocess.run([str(a.lld),'-flavor','link','/dll','/noentry','/nodefaultlib','/machine:x64','/out:'+str(dll),
                    '/export:NM_CRC32','/export:NM_V2_Record','/export:NM_V2_Frame',*map(str,objs)],check=True,capture_output=True)
    lib=c.CDLL(str(dll))
    lib.NM_CRC32.argtypes=[c.c_void_p,c.c_size_t];lib.NM_CRC32.restype=c.c_uint32
    lib.NM_V2_Record.argtypes=[c.c_void_p,c.c_uint8,c.c_uint32,c.c_uint32,c.c_uint64,c.c_int32,c.c_int32,c.c_uint32]
    lib.NM_V2_Frame.argtypes=[c.c_void_p,c.c_uint8,c.c_uint32,c.c_void_p,c.c_uint16];lib.NM_V2_Frame.restype=c.c_size_t
    assert lib.NM_CRC32(b'123456789',9)==0xCBF43926
    count=0
    for seq in (0,1,0xFFFFFFFF):
        for t in (0,2**32+1,2**48+123):
            records=[];payload=c.create_string_buffer(224)
            for i in range(8):
                sample=NativeSample(i%2,400+i,t+i,-8388608+i,8388607-i,dropped=i)
                records.append(sample)
                lib.NM_V2_Record(c.byref(payload,28*i),sample.device|sample.flags,sample.status,sample.index,sample.time_us,sample.code1,sample.code2,sample.dropped)
            frame=c.create_string_buffer(1040)
            length=lib.NM_V2_Frame(frame,SAMPLES,seq,payload,224)
            assert frame.raw[:length]==encode_packet(SAMPLES,records,seq)
            count+=1
    report={'status':'passed','actual_mcu_codec_compiled_and_executed':True,'vectors':count,'native_records_checked':count*8,'crc_check':'CBF43926'}
    (out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))


if __name__=='__main__':main()
