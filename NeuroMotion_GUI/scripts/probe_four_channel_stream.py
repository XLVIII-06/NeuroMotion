"""Read-only dual-ADC transport check; never writes, resets or flashes a device."""
from pathlib import Path
import argparse
from collections import Counter
import json
import math
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.acquisition import SerialSource
from neuromotion.protocol_v2 import BOOT, META, SAMPLES, STATUS, SYNC_EDGE, V2Decoder
from neuromotion.session import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM5')
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 5:
        parser.error('Use at least five seconds')
    args.output.mkdir(parents=True, exist_ok=False)
    decoder = V2Decoder()
    counts = Counter()
    devices = [dict(records=0, valid=0, gaps=0, resets_or_duplicates=0, unexpected_time_intervals=0,
                    flags=Counter(), first=None, last=None) for _ in range(2)]
    metadata, hardware, edges, baseline = None, {}, 0, None
    byte_count = 0
    source = SerialSource(args.port)
    started = time.monotonic()
    try:
        with (args.output/'serial.bin').open('wb') as raw:
            while time.monotonic()-started < args.seconds:
                data = source.read(65536)
                raw.write(data)
                byte_count += len(data)
                packets = decoder.feed(data)
                if packets and baseline is None:
                    baseline = decoder.frame_errors
                for packet in packets:
                    counts[packet.kind] += 1
                    if packet.kind in (BOOT, META):
                        metadata = packet.value
                    elif packet.kind == STATUS:
                        hardware = packet.value
                    elif packet.kind == SYNC_EDGE:
                        edges += 1
                    elif packet.kind == SAMPLES:
                        for event in packet.value:
                            d = devices[event.device]
                            d['records'] += 1
                            d['valid'] += int(event.valid)
                            d['flags'][event.flags] += 1
                            last = d['last']
                            if last:
                                d['gaps'] += max(0, event.index-last[0]-1)
                                d['resets_or_duplicates'] += int(event.index <= last[0] or event.time_us <= last[1])
                                d['unexpected_time_intervals'] += int(not 900 <= event.time_us-last[1] <= 1100)
                            d['last'] = (event.index, event.time_us)
                            if d['first'] is None:
                                d['first'] = d['last']
    finally:
        source.close()
    elapsed = time.monotonic()-started
    for d in devices:
        first, last = d['first'], d['last']
        d['measured_rate_hz_mcu'] = None if not first or last[1] == first[1] else (last[0]-first[0])*1e6/(last[1]-first[1])
    errors = decoder.frame_errors-(baseline or 0)
    good = metadata and metadata.get('ready_mask') == 3 and edges >= 5 and hardware
    good = good and not errors and not decoder.invalid_records and not decoder.sequence_gaps
    good = good and all(d['records'] > args.seconds*800 and d['valid'] == d['records'] and not d['gaps']
                       and not d['resets_or_duplicates'] and not d['unexpected_time_intervals'] for d in devices)
    good = good and all(not value for key, value in hardware.items() if key != 'max_read_delay_us')
    report = dict(status='passed' if good else 'failed', evidence_kind='real_dual_adc_read_only_transport',
                  port=args.port, elapsed_s=elapsed, bytes=byte_count, bytes_per_second=byte_count/elapsed,
                  packet_counts=counts, devices=devices, metadata=metadata, hardware=hardware, ttl_edges=edges,
                  frame_errors_after_lock=errors, initial_resync_errors=baseline,
                  invalid_records=decoder.invalid_records, packet_gaps=decoder.sequence_gaps,
                  rate_note='Captured-event rates in nominal MCU seconds; missed physical captures can preserve contiguous native indices.',
                  firmware_downloaded=False, analog_timing_validated=False)
    write_json(args.output/'report.json', report)
    print(json.dumps(report, indent=2), flush=True)
    return 0 if good else 1


if __name__ == '__main__':
    raise SystemExit(main())
