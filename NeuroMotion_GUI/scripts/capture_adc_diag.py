"""Capture real ADC via the production recorder, with slow bounded SWD reads."""
from pathlib import Path
import argparse
from datetime import datetime
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.acquisition import AcquisitionController
from neuromotion.session import write_json
from scripts.read_mcu_diag import read_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hex", type=Path, required=True)
    parser.add_argument("--port", default="COM5")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--variant", default="D_ADC")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    armed = args.output / "reset_confirmed.json"
    controller = AcquisitionController()
    snapshots, failure, begin = [], None, time.monotonic()
    running_since = changed_at = None
    previous_samples = None
    verified = False
    try:
        controller.connect(port=args.port)
        controller.start_recording(args.output / "session")
        print(f"LISTENING {args.port}; reset MCU then confirm; arm file: {armed}", flush=True)
        while time.monotonic() - begin < args.seconds + 240:
            if not armed.exists():
                time.sleep(.1)
                continue
            now = time.monotonic()
            info, raw = read_snapshot(args.hex, verify=not verified)
            verified = True
            state = controller.snapshot(include_plot=False)
            info.update(elapsed_s=now - begin, received_samples=state["samples"],
                        recorded_samples=state["recorded_samples"], host_frame_errors=state["frame_errors"],
                        host_index_gaps=state["index_gaps"], recorder_error=state["error"])
            snapshots.append(info)
            (args.output / f"snapshot_{len(snapshots):03}.txt").write_text(raw, encoding="utf-8")
            write_json(args.output / "snapshots.json", {"snapshots": snapshots})
            d = info["snapshot"]
            print(json.dumps({"elapsed_s":round(now-begin,1),"stage":d["stage"],"tick":d["tick_ms"],
                              "irq":d["irq_enter"],"spi_ok":d["spi_ok"],"spi_error":d["spi_error"],
                              "samples":d["samples"],"uart_ok":d["tx_ok"],"uart_error":d["tx_error"],
                              "host_samples":state["samples"],"ads_error":d["ads_error"]}), flush=True)
            if d["mode"] != 2:
                failure = "unexpected diagnostic mode"
                break
            if d["stage"] == 5:
                failure = f"MCU initialization/stage error; ADS error={d['ads_error']} ID={d['device_id']}"
                break
            if state["error"]:
                failure = state["error"]
                break
            if d["stage"] == 3:
                if running_since is None:
                    running_since = changed_at = now
                if d["samples"] != previous_samples:
                    changed_at = now
                    previous_samples = d["samples"]
                elif now - changed_at >= 4:
                    failure = "sampling counter stalled for at least 4 seconds"
                    break
                if d["spi_error"] or d["tx_error"] or d["dropped"]:
                    failure = "SPI/UART errors or dropped samples observed"
                    break
                if now - running_since >= args.seconds:
                    break
            time.sleep(1.5)
        else:
            failure = "timed out waiting for reset/run"
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if controller.thread:
            controller.disconnect()
    final = controller.snapshot(include_plot=False)
    if not failure and (running_since is None or final["recorded_samples"] == 0):
        failure = "no sustained real ADC recording"
    report = {"status":"passed" if failure is None else "failed", "reason":failure,
              "created_at":datetime.now().astimezone().isoformat(), "firmware_mode":args.variant,
              "synthetic_uart_only":False,"samples_saved":final["recorded_samples"],
              "host_frame_errors":final["frame_errors"],"host_index_gaps":final["index_gaps"],
              "snapshots":snapshots,"port_released":True}
    write_json(args.output / "adc_validation.json", report)
    print(json.dumps({key:value for key,value in report.items() if key != "snapshots"}), flush=True)


if __name__ == "__main__":
    main()
