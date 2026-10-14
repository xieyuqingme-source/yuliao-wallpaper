"""Build the Windows folder bundle with an isolated child PATH and source notices."""
import argparse
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import struct
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", choices=("dist", "dist-0.2"), default="dist")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("This build targets Windows 10/11 x64.")
    if sys.version_info[:3] != (3, 11, 4):
        raise SystemExit("Use CPython 3.11.4 to match the bundled runtime notices.")
    if struct.calcsize("P") != 8:
        raise SystemExit("Use x64 CPython to build the documented Windows x64 bundle.")
    for filename in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
            if "==" not in line or line.lstrip().startswith("#"):
                continue
            package, pinned = line.strip().split("==", 1)
            installed = importlib.metadata.version(package)
            if installed != pinned:
                raise SystemExit(f"Version mismatch for {package}: install {pinned} before building.")
    python_root = Path(sys.base_prefix)
    windows_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(str(p) for p in (
        Path(sys.executable).parent, python_root, python_root / "DLLs",
        windows_root / "System32", windows_root,
    ))
    env["PYTHONNOUSERSITE"] = "1"
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    subprocess.run([sys.executable, "scripts/collect_notices.py"], cwd=ROOT,
                   env=env, check=True)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--windowed", "--name", "YuliaoWallpaper", "--distpath", args.output,
        "--workpath", "build-0.2", "--specpath", "build-spec-0.2",
        "--paths", str(ROOT), "--version-file", str(ROOT / "scripts/version-info.txt"),
        "--hidden-import", "pystray._win32", "scripts/portable_entry.py",
    ], cwd=ROOT, env=env, check=True)
    bundle = ROOT / args.output / "YuliaoWallpaper"
    runtime = (bundle / "_internal").resolve()
    runtime.relative_to(ROOT.resolve())
    for item in runtime.iterdir():
        name = item.name.lower()
        if item.is_file() and (name.startswith("api-ms-win-") and name.endswith(".dll")
                               or name == "ucrtbase.dll"
                               or name.startswith("vcruntime140") and name.endswith(".dll")):
            item.resolve().relative_to(runtime)
            item.unlink()
    shutil.copytree(ROOT / "third_party", bundle / "third_party", dirs_exist_ok=True)
    for name in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, bundle / name)
    # LGPL 4d0: include the matching application and library sources, fixed
    # dependencies and rebuild scripts with every portable folder distribution.
    source = bundle / "corresponding-source"
    source.mkdir(exist_ok=True)
    for name in (".gitignore", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
                 "requirements.txt", "requirements-dev.txt", "start.cmd"):
        shutil.copyfile(ROOT / name, source / name)
    for name in ("yuliao_wallpaper", "tests", "scripts", "docs", "third_party"):
        shutil.copytree(ROOT / name, source / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".git"))
    print("Portable folder built with runtime notices and matching rebuild source.")


if __name__ == "__main__":
    main()
