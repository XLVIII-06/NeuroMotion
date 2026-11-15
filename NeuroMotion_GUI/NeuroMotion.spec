# Build only into build/windows/dist; never point PyInstaller at the source tree.
from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root / "NeuroMotion.pyw")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "config.sync-template.json"), "."),
           (str(root / "config.full-example.json"), "."),
           (str(root / "calibration/board_spec.json"), "calibration")],
    hiddenimports=["serial.tools.list_ports", "serial.tools.list_ports_windows"],
    hooksconfig={"matplotlib": {"backends": ["Agg"]}},
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "notebook", "jupyter",
              "pytest", "torch", "tensorflow", "numpy.tests", "pandas.tests", "scipy.tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="NeuroMotion", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False)
