"""Double-click desktop entry point, with visible startup errors on Windows."""

from pathlib import Path
import os
import sys
import traceback


def main() -> None:
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    os.chdir(root)
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--smoke-test":
            from neuromotion.package_check import main as check_package
            raise SystemExit(check_package(sys.argv[2:]))
        from neuromotion.gui import launch
        launch()
    except Exception:
        detail = traceback.format_exc()
        log_path = root / "startup_error.log"
        try:
            log_path.write_text(detail, encoding="utf-8")
        except OSError:
            pass
        message = (
            "NeuroMotion 启动失败。\n\n" +
            ("请保留此错误日志，并重新构建或替换完整的 NeuroMotion.exe。\n\n" if getattr(sys, "frozen", False)
             else "请在工程目录运行：\npython -m pip install -r requirements.txt\n\n") +
            f"详细信息：{log_path}\n\n{detail[-1600:]}"
        )
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "NeuroMotion", 0x10)
        else:
            print(message)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
