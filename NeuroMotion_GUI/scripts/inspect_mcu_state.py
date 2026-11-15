"""Read live STM32 state through ST-Link HotPlug, without halt/reset/flash.

The flashed HEX must match before RAM symbols from its MAP are interpreted.
All ST-Link commands here are reads or file comparison. No core-run, reset,
erase, program, option-byte or memory-write commands are used.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.session import file_info, write_json


SYMBOLS = ("last_error", "device_id", "sampling_enabled", "sample_index",
           "stream_count", "dropped_samples", "ads_regs", "uwTick")


def symbols_from_map(path):
    result = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"\s*(\w+)\s+(0x[0-9a-fA-F]+)\s+Data\s+(\d+)\s", line)
        if match and match[1] in SYMBOLS:
            result[match[1]] = (int(match[2], 16), int(match[3]))
    missing = set(SYMBOLS) - result.keys()
    if missing:
        raise ValueError(f"MAP lacks required symbols: {sorted(missing)}")
    return result


def run_cli(path, commands):
    # ST-LINK CLI 3.6 crashed on a long sequence of repeated read operations.
    # Give each invocation exactly one documented operation; never batch them.
    valid = (commands == ["-SCore"] or
             (len(commands) == 2 and commands[0] == "-CmpFile") or
             (len(commands) == 3 and commands[0] in ("-r8", "-r16", "-r32")))
    if not valid:
        raise ValueError("ST-Link inspection accepts one read/compare operation per process")
    process = subprocess.run([str(path), "-c", "ID=0", "SWD", "HOTPLUG", *commands],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    text = process.stdout.decode("utf-8", errors="replace")
    text = re.sub(r"(ST-LINK SN:\s*)\S+", r"\1[redacted]", text)
    if process.returncode or "Connection mode: HotPlug" not in text or "Device family: STM32F446xx" not in text:
        raise RuntimeError("Expected one accessible STM32F446 in HotPlug mode:\n" + text[-1800:])
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hex", type=Path, required=True)
    parser.add_argument("--map", type=Path)
    parser.add_argument("--cli", type=Path, default=Path(r"D:\Program Files (x86)\STMicroelectronics\STM32 ST-LINK Utility\ST-LINK Utility\ST-LINK_CLI.exe"))
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output path to retain previous observations")
    map_path = args.map or args.hex.with_suffix(".map")
    symbols = symbols_from_map(map_path)
    image = file_info(args.hex)
    map_info = file_info(map_path)
    comparison = run_cli(args.cli, ["-CmpFile", str(args.hex.resolve())])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".compare.txt").write_text(comparison, encoding="utf-8")
    if "No difference found." not in comparison:
        raise RuntimeError("Flashed image differs from HEX; refusing to interpret MAP RAM addresses")
    observations = [run_cli(args.cli, ["-SCore"])]
    for address, size in sorted(set(symbols.values())):
        observations.append(run_cli(args.cli, ["-r8", hex(address), hex(size)]))
    observation = "\n".join(observations)
    args.output.with_suffix(".read.txt").write_text(observation, encoding="utf-8")
    memory = {}
    for line in observation.splitlines():
        match = re.match(r"\s*(0x[0-9a-fA-F]+)\s*:\s*(.*)", line)
        if match:
            start = int(match[1], 16)
            for offset, token in enumerate(re.findall(r"\b[0-9a-fA-F]{2}\b", match[2])):
                memory[start + offset] = int(token, 16)
    values = {}
    for name, (address, size) in symbols.items():
        raw = bytes(memory[address + offset] for offset in range(size))
        values[name] = list(raw) if name == "ads_regs" else int.from_bytes(raw, "little")
    if file_info(args.hex)["sha256"] != image["sha256"] or file_info(map_path)["sha256"] != map_info["sha256"]:
        raise RuntimeError("Build files changed during observation; retry after the build completes")
    report = {"observed_at": datetime.now().astimezone().isoformat(), "connection_mode": "HotPlug",
              "firmware_matched": True, "firmware_modified": False,
              "core_running": "Core is Running." in observation,
              "hex": image, "map": map_info, "symbols": values,
              "symbol_addresses": {name: hex(item[0]) for name, item in symbols.items()}}
    write_json(args.output, report)
    print(values)
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
