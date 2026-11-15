"""Reset a verified diagnostic image using one ST-Link hardware-reset operation."""
from pathlib import Path
import re
import subprocess

from scripts.inspect_mcu_state import run_cli


def reset_diagnostic(image: Path, output: Path):
    cli = Path(r"D:\Program Files (x86)\STMicroelectronics\STM32 ST-LINK Utility\ST-LINK Utility\ST-LINK_CLI.exe")
    comparison = run_cli(cli, ["-CmpFile", str(image.resolve())])
    if "No difference found." not in comparison:
        raise RuntimeError("Target image differs; reset cancelled")
    output.mkdir(parents=True, exist_ok=True)
    (output / "reset_flash_compare.txt").write_text(comparison, encoding="utf-8")
    result = subprocess.run([str(cli), "-c", "ID=0", "SWD", "HOTPLUG", "-HardRst"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
    raw = re.sub(r"(ST-LINK SN:\s*)\S+", r"\1[redacted]", result.stdout.decode("utf-8", errors="replace"))
    (output / "hardware_reset.txt").write_text(raw, encoding="utf-8")
    if result.returncode or "Hard reset is performed." not in raw:
        raise RuntimeError("ST-Link hardware reset failed; see saved log")
