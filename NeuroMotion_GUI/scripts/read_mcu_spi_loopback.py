"""Read the bounded result of the isolated PA7-to-PA6 SPI loopback test."""
from pathlib import Path
import argparse
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.inspect_mcu_state import run_cli
from neuromotion.mcu_diagnostics import decode_spi_loopback
from neuromotion.session import file_info, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hex", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output path")
    mapping = args.hex.with_suffix(".map").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^\s*g_nm_spi_selftest\s+(0x[0-9a-fA-F]+)\s+Data\s+128\s", mapping, re.M)
    if not match:
        raise RuntimeError("Image MAP lacks the SPI loopback result")
    cli = Path(r"D:\Program Files (x86)\STMicroelectronics\STM32 ST-LINK Utility\ST-LINK Utility\ST-LINK_CLI.exe")
    comparison = run_cli(cli, ["-CmpFile", str(args.hex.resolve())])
    if "No difference found." not in comparison:
        raise RuntimeError("Target differs from loopback image")
    address = int(match[1], 16)
    raw = run_cli(cli, ["-r8", hex(address), "0x80"])
    memory = {}
    for line in raw.splitlines():
        row = re.match(r"\s*(0x[0-9a-fA-F]+)\s*:\s*(.*)", line)
        if row:
            for i, value in enumerate(re.findall(r"\b[0-9a-fA-F]{2}\b", row[2])):
                memory[int(row[1], 16)+i] = int(value, 16)
    result = decode_spi_loopback(bytes(memory[address+i] for i in range(128)))
    result["image"] = file_info(args.hex)
    write_json(args.output, result)
    args.output.with_suffix(".read.txt").write_text(raw, encoding="utf-8")
    print({k: v for k, v in result.items() if k != "image"})


if __name__ == "__main__":
    main()
