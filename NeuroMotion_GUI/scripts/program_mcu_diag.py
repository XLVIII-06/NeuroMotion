"""Download a reviewed application image; retain the baseline rollback.

No mass erase or option-byte commands are issued. Each CLI process receives
one operation. This is an explicit programming tool, not the read-only reader.
"""
from pathlib import Path
import argparse
from datetime import datetime
import json
import re
import struct
import subprocess
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.session import file_info, write_json
from scripts.inspect_mcu_state import run_cli


def image_memory(path):
    upper, memory = 0, {}
    for line in path.read_text().splitlines():
        if not line.startswith(":"):
            raise ValueError("Expected Intel HEX records")
        row = bytes.fromhex(line[1:])
        if sum(row) % 256 or len(row) != row[0] + 5:
            raise ValueError("Invalid HEX checksum/length")
        size, address, kind = row[0], int.from_bytes(row[1:3], "big"), row[3]
        if kind == 4:
            upper = int.from_bytes(row[4:4 + size], "big") << 16
        elif kind == 0:
            for index, value in enumerate(row[4:4 + size]):
                memory[upper + address + index] = value
        elif kind not in (1, 5):
            raise ValueError(f"Unsupported HEX record type: {kind}")
    if not memory or not all(0x08000000 <= address < 0x08080000 for address in memory):
        raise ValueError("Only STM32F446 application Flash addresses are allowed")
    stack, reset = struct.unpack("<II", bytes(memory[0x08000000 + index] for index in range(8)))
    if not 0x20000000 <= stack <= 0x20020000 or not reset & 1 or not 0x08000000 <= reset < 0x08080000:
        raise ValueError("Invalid application vector table")
    return memory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hex", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--project-root", type=Path,
                        help="Source project to archive; defaults to the isolated diagnostic project")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2] / "NeuroMotion_MCU"
    active = json.loads((root / "mcu_diagnosis/active_run.json").read_text())
    rollback = Path(active["baseline"]) / "MDK-ARM/ads1292_f446re/ads1292_f446re.hex"
    image_memory(rollback)
    memory = image_memory(args.hex)
    generated = (args.project_root or root / "mcu_diagnosis/project").resolve()
    projects = list((generated / "MDK-ARM").glob("*.uvprojx"))
    if not (generated / "Core").is_dir() or len(projects) != 1:
        raise ValueError("Source project must contain Core and exactly one MDK-ARM/*.uvprojx")
    if not args.hex.resolve().is_relative_to(generated / "MDK-ARM"):
        raise ValueError("HEX must belong to the source project being archived")
    cli = Path(r"D:\Program Files (x86)\STMicroelectronics\STM32 ST-LINK Utility\ST-LINK Utility\ST-LINK_CLI.exe")
    run_cli(cli, ["-SCore"])  # Validate target family before any write.
    folder = Path(active["run"]) / args.label
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = folder / f"build_{stamp}"
    archive.mkdir(exist_ok=False)
    retained = []
    for suffix in (".hex", ".map", ".axf"):
        artifact = args.hex.with_suffix(suffix)
        if artifact.exists():
            copy = archive / artifact.name
            shutil.copy2(artifact, copy)
            retained.append(file_info(copy))
    shutil.copytree(generated / "Core", archive / "Core")
    shutil.copy2(projects[0], archive)
    write_json(archive / "manifest.json", {"artifacts": retained, "source": str(generated)})
    result = subprocess.run([str(cli), "-c", "ID=0", "SWD", "UR", "-P", str(args.hex.resolve())],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
    output = result.stdout.decode("utf-8", errors="replace")
    output = re.sub(r"(ST-LINK SN:\s*)\S+", r"\1[redacted]", output)
    (folder / f"program_{stamp}.txt").write_text(output, encoding="utf-8")
    if result.returncode or "Programming Complete." not in output:
        raise RuntimeError("Programming failed; see saved log")
    comparison = run_cli(cli, ["-CmpFile", str(args.hex.resolve())])
    (folder / f"compare_{stamp}.txt").write_text(comparison, encoding="utf-8")
    if "No difference found." not in comparison:
        raise RuntimeError("Post-download Flash comparison failed")
    report = {"image": file_info(args.hex), "rollback": file_info(rollback),
              "region": [hex(min(memory)), hex(max(memory) + 1)], "flash_matches": True,
              "programmed_at": datetime.now().astimezone().isoformat(), "label": args.label}
    write_json(folder / f"program_{stamp}.json", report)
    print(f"Programmed and verified: {args.label}")


if __name__ == "__main__":
    main()
