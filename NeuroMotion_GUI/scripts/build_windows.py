"""Build a self-contained Windows EXE without replacing source or experiment files."""

from pathlib import Path
import argparse
from datetime import datetime
from importlib.metadata import version
import os
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neuromotion.session import file_info, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-smoke-test", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Build the Windows EXE on Windows")
    root = Path(__file__).resolve().parents[1]
    build = root / "build/windows"
    build.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log = build / f"build_{stamp}.log"
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(build / "dist"),
               "--workpath", str(build / "work"), str(root / "NeuroMotion.spec")]
    env = {**os.environ, "PYTHONUTF8": "1", "MPLBACKEND": "Agg"}
    print(f"Building NeuroMotion.exe; log: {log}", flush=True)
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"PyInstaller failed; see {log}")
    built = build / "dist/NeuroMotion.exe"
    if not built.is_file():
        raise FileNotFoundError(built)
    smoke_reports = []
    if not args.skip_smoke_test:
        for channels in (2,4):
            smoke = root / "output" / f"package_smoke_{channels}ch_{stamp}"
            result = subprocess.run([str(built), "--smoke-test", str(smoke), "--channels", str(channels)], cwd=build, env=env, timeout=120)
            if result.returncode:
                raise RuntimeError(f"Packaged {channels}-channel smoke test failed: {smoke}; previous EXE retained")
            smoke_reports.append(str(smoke))
    target = root / "NeuroMotion.exe"
    temporary = root / "NeuroMotion.exe.new"
    shutil.copy2(built, temporary)
    try:
        temporary.replace(target)
    except PermissionError as exc:
        raise RuntimeError("Close the running NeuroMotion.exe, then rerun this build") from exc
    packages = {name: version(name) for name in ("pyinstaller", "numpy", "pandas", "opencv-python", "scipy", "matplotlib", "pyserial")}
    write_json(build / "last_build.json", {"executable": file_info(target), "log": str(log), "packages": packages,
               "built_at": datetime.now().astimezone().isoformat(), "smoke_reports": smoke_reports})
    print(f"Built and checked: {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
