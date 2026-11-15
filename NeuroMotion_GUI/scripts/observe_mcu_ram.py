"""Observe the matched IRQ/polling RAM-only pair; no serial data expected."""
from pathlib import Path
import argparse
from datetime import datetime
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.read_mcu_diag import read_snapshot
from scripts.reset_mcu_diag import reset_diagnostic
from neuromotion.session import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hex", type=Path, required=True)
    parser.add_argument("--mode", type=int, choices=[4, 5, 6], required=True)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    trigger = parser.add_mutually_exclusive_group()
    trigger.add_argument("--hardware-reset", action="store_true", help="Verify image and pulse NRST before observing")
    trigger.add_argument("--observe-current", action="store_true", help="Observe an operator-confirmed power-up without issuing a reset")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    marker = args.output / "reset_confirmed.json"
    print(f"READY RAM-only mode {args.mode}; reset then confirm: {marker}", flush=True)
    begin = time.monotonic()
    snapshots, reason = [], None
    running_at = changed_at = previous = None
    counter = "irq_enter" if args.mode == 6 else "samples"
    verified = False
    try:
        if args.hardware_reset:
            reset_diagnostic(args.hex, args.output)
            write_json(marker, {"source": "verified ST-Link hardware reset", "at": datetime.now().astimezone().isoformat()})
        elif args.observe_current:
            write_json(marker, {"source": "operator-confirmed power-up; no reset issued", "at": datetime.now().astimezone().isoformat()})
        while time.monotonic() - begin < args.seconds + 240:
            if not marker.exists():
                time.sleep(.1)
                continue
            now = time.monotonic()
            report, raw = read_snapshot(args.hex, verify=not verified)
            verified = True
            report["elapsed_s"] = now - begin
            snapshots.append(report)
            write_json(args.output / "snapshots.json", {"snapshots": snapshots})
            (args.output / f"snapshot_{len(snapshots):03}.txt").write_text(raw, encoding="utf-8")
            d = report["snapshot"]
            print(json.dumps({"elapsed_s":round(now-begin,2),"mode":d["mode"],"stage":d["stage"],
                              "tick":d["tick_ms"],"heartbeat":d["heartbeat"],"irq_enter":d["irq_enter"],
                              "irq_exit":d["irq_exit"],"spi_ok":d["spi_ok"],"spi_error":d["spi_error"],
                              "samples":d["samples"],"drdy":d["drdy_level"],"poll_edges":d.get("gpio_reserved0",0),
                              "phase":d["last_phase"],"ads_error":d["ads_error"],
                              "valid_status":d.get("valid_status"),"invalid_status":d.get("invalid_status"),
                              "probe_reason":d.get("probe_reason"),"probe_stage":d.get("probe_stage")}),flush=True)
            if d["mode"] != args.mode:
                reason = "unexpected diagnostic mode"
                break
            if d["stage"] == 7:
                reason = f"fault register probe completed; trigger={d.get('probe_reason')}"
                break
            if d["stage"] == 5:
                reason = f"initialization failed; ADS error={d['ads_error']}; ID={d['device_id']}"
                break
            if d["stage"] == 3:
                if running_at is None:
                    running_at = changed_at = now
                if previous != d[counter]:
                    changed_at, previous = now, d[counter]
                elif now - changed_at >= 4:
                    reason = f"{counter} counter stalled for at least 4 seconds"
                    break
                if d["spi_error"] or d["dropped"]:
                    reason = "SPI errors or ring overflow observed"
                    break
                if d.get("invalid_status", 0):
                    reason = "ADC status violates fixed prefix/zero-bit format despite HAL_OK"
                    break
                if now - running_at >= args.seconds:
                    break
            time.sleep(1.5)
        else:
            reason = "timed out waiting for reset/run"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
    rate = None
    running = [report["snapshot"] for report in snapshots if report["snapshot"]["stage"] == 3]
    if len(running) >= 2:
        first, last = running[0], running[-1]
        elapsed_ms = last["tick_ms"] - first["tick_ms"]
        if elapsed_ms > 0:
            rate = (last[counter] - first[counter]) * 1000 / elapsed_ms
    if reason is None and (rate is None or not 990 <= rate <= 1010):
        reason = "observed counter rate outside planned 1000/s +/-1% check"
    result = {"status":"passed" if reason is None else "failed","reason":reason,
              "mode":args.mode,"created_at":datetime.now().astimezone().isoformat(),
              "rate_per_mcu_tick_s":rate,"observed_counter":counter,
              "test_kind":"DRDY edges without ADC reads" if args.mode == 6 else "RAM-only ADC reads",
              "real_adc_validated":False,
              "uart_output_intentionally_disabled":True,"snapshots":snapshots}
    write_json(args.output / "ram_validation.json",result)
    print(json.dumps({k:v for k,v in result.items() if k!="snapshots"}),flush=True)


if __name__ == "__main__":
    main()
