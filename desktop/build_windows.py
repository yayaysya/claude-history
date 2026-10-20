"""在独立虚拟环境中构建 Windows 单文件程序。"""

from pathlib import Path
import sys

import PyInstaller.__main__

from tray import make_icon


def main():
    if sys.platform != "win32":
        raise SystemExit("请在 Windows 下运行此脚本")
    root = Path(__file__).resolve().parent
    build = root / "build"
    build.mkdir(exist_ok=True)
    icon = build / "session-history.ico"
    make_icon(256).save(icon, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    PyInstaller.__main__.run([
        str(root / "app.py"), "--name", "SessionHistory", "--onefile", "--windowed", "--noconfirm",
        "--distpath", str(root / "dist"), "--workpath", str(build), "--specpath", str(root),
        "--add-data", str(root / "static") + ";static", "--icon", str(icon),
        "--hidden-import", "pystray._win32", "--collect-submodules", "pygments.lexers",
        "--collect-submodules", "pygments.styles",
    ])
    print("构建完成：" + str(root / "dist" / "SessionHistory.exe"))


if __name__ == "__main__":
    main()
