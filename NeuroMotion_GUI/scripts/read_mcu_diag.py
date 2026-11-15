"""Read one bounded diagnostic RAM block using its matching build's MAP."""
from pathlib import Path
import argparse
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.mcu_diagnostics import decode_diag
from neuromotion.session import write_json, file_info
from scripts.inspect_mcu_state import run_cli


def read_snapshot(image: Path, verify=True):
    mapping = image.with_suffix(".map").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^\s*g_nm_diag\s+(0x[0-9a-fA-F]+)\s+Data\s+(128|160|192|256)\s", mapping, re.M)
    if not match:
        raise RuntimeError("MAP has no supported diagnostic symbol")
    cli = Path(r"D:\Program Files (x86)\STMicroelectronics\STM32 ST-LINK Utility\ST-LINK Utility\ST-LINK_CLI.exe")
    if verify:
        compare = run_cli(cli, ["-CmpFile", str(image.resolve())])
        if "No difference found." not in compare:
            raise RuntimeError("Flashed image does not match diagnostic build")
    address = int(match[1], 16)
    size = int(match[2])
    output = run_cli(cli, ["-r8", hex(address), hex(size)])
    memory = {}
    for line in output.splitlines():
        row = re.match(r"\s*(0x[0-9a-fA-F]+)\s*:\s*(.*)", line)
        if row:
            for index, byte in enumerate(re.findall(r"\b[0-9a-fA-F]{2}\b", row[2])):
                memory[int(row[1], 16) + index] = int(byte, 16)
    data = bytes(memory[address + index] for index in range(size))
    report = {"hex": file_info(image), "address": hex(address), "snapshot": decode_diag(data)}
    return report, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hex", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output path")
    report, output = read_snapshot(args.hex)
    write_json(args.output, report)
    args.output.with_suffix(".read.txt").write_text(output, encoding="utf-8")
    print(report["snapshot"])


if __name__ == "__main__":
    main()
