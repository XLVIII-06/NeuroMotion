"""Read B-only native samples without serial writes, resets or flashing."""
from pathlib import Path
import argparse
import csv
import json
import math
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.acquisition import SerialSource
from neuromotion.protocol_v2 import BOOT, META, SAMPLES, STATUS, V2Decoder


class ProbeDecoder(V2Decoder):
    """Locate the first valid frame exactly; retain all errors after that frame.

    Opening an active UART can start inside a packet. Bytewise decoding is
    limited to initial acquisition so later errors in the same read cannot
    accidentally be included in an initial-error baseline.
    """
    def __init__(self):
        super().__init__()
        self.locked = False
        self.initial_resync_errors = 0

    def feed(self, data):
        if self.locked:
            return super().feed(data)
        for i in range(len(data)):
            packets = super().feed(data[i:i+1])
            if packets:
                self.locked = True
                self.initial_resync_errors = self.frame_errors
                return packets + super().feed(data[i+1:])
        return []


def initialization_ok(meta):
    if not isinstance(meta, dict):
        return False
    if not (meta.get('firmware') == 'NeuroMotion-B-only-probe' and
            meta.get('diagnostic_mode') == 'B_only' and meta.get('ready_mask') == 2 and
            meta.get('id_b') in (0x53, 0x73) and meta.get('error_b') == 0):
        return False
    try:
        regs = bytes.fromhex(meta['registers_b'])
        return (len(regs) == 12 and regs[0] == meta['id_b'] and regs[1] == 3 and
                regs[4] == regs[5] == regs[6] == 0 and bool(regs[8] & 0x40))
    except (KeyError, ValueError, TypeError):
        return False


def summarize(meta, samples, hardware, seconds, decoder):
    initial_errors = getattr(decoder, 'initial_resync_errors', 0)
    stable_errors = decoder.frame_errors - initial_errors
    gaps = bad_time = wrong_device = invalid = 0
    last = None
    for s in samples:
        wrong_device += s.device != 1
        invalid += not s.valid
        if last:
            gaps += abs(s.index - last.index - 1)
            bad_time += not 900 <= s.time_us - last.time_us <= 1100
        last = s
    rate = None
    if len(samples) > 1 and samples[-1].time_us > samples[0].time_us:
        rate = (len(samples)-1)*1e6/(samples[-1].time_us-samples[0].time_us)
    fields = ['capture_overrun_b', 'dropped_b', 'spi_errors_b', 'late_b', 'status_errors_b',
              'record_dropped', 'tx_errors']
    status_ok = isinstance(hardware, dict) and all(hardware.get(k) == 0 for k in fields)
    passed = (initialization_ok(meta) and status_ok and len(samples) >= seconds*800 and
              rate is not None and 950 <= rate <= 1050 and not any([gaps, bad_time, wrong_device, invalid,
                  stable_errors, decoder.invalid_records, decoder.sequence_gaps]))
    return {'status': 'passed' if passed else 'failed', 'metadata': meta, 'hardware': hardware,
            'native_B_records': len(samples), 'rate_hz_in_nominal_mcu_seconds': rate,
            'index_discontinuity': gaps, 'bad_time_intervals': bad_time,
            'wrong_device_records': wrong_device, 'invalid_samples': invalid,
            'frame_errors': decoder.frame_errors, 'invalid_records': decoder.invalid_records,
            'initial_resync_errors': initial_errors, 'frame_errors_after_lock': stable_errors,
            'framing_note': 'Initial discard events are not lost-frame counts; all post-lock errors remain failures.',
            'packet_gaps': decoder.sequence_gaps, 'analog_accuracy_validated': False,
            'scope': 'B-only digital communication and continuous native sampling; floating analog inputs',
            'firmware_downloaded_by_this_script': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM5')
    parser.add_argument('--seconds', type=float, default=15)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 5:
        parser.error('Use at least five seconds')
    args.output.mkdir(parents=True, exist_ok=False)
    decoder = ProbeDecoder()
    samples, meta, hardware, error = [], None, None, ''
    source = SerialSource(args.port)
    start = time.monotonic()
    try:
        with (args.output/'serial.bin').open('wb') as raw:
            while time.monotonic()-start < args.seconds:
                data = source.read(65536)
                raw.write(data)
                for packet in decoder.feed(data):
                    if packet.kind in (BOOT, META):
                        meta = packet.value
                        if not initialization_ok(meta):
                            raise RuntimeError('B initialization failed or unexpected firmware; inspect metadata')
                    elif packet.kind == STATUS:
                        hardware = packet.value
                    elif packet.kind == SAMPLES:
                        samples.extend(packet.value)
    except Exception as exc:
        error = str(exc)
    finally:
        source.close()
    report = summarize(meta, samples, hardware, args.seconds, decoder)
    report.update({'elapsed_s': time.monotonic()-start, 'error': error, 'port_released': True})
    if error:
        report['status'] = 'failed'
    with (args.output/'native_B.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['index', 'time_us', 'code1', 'code2', 'status', 'flags'])
        writer.writerows((s.index, s.time_us, s.code1, s.code2, s.status, s.flags) for s in samples)
    (args.output/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
