"""Keep unmodified upstream runtime notices and LGPL package source with builds."""
import hashlib
import importlib.metadata
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "third_party"
DEST.mkdir(exist_ok=True)
for name in ("Pillow", "pystray", "six", "PyInstaller"):
    distribution = importlib.metadata.distribution(name)
    output = DEST / f"{name}-{distribution.version}"
    output.mkdir(exist_ok=True)
    for item in distribution.files or []:
        if "license" in item.name.lower() or item.name.lower().startswith("copying"):
            source = Path(distribution.locate_file(item))
            if source.is_file():
                shutil.copyfile(source, output / source.name)
    if name == "pystray":
        source = Path(distribution.locate_file("pystray"))
        shutil.copytree(source, output / "source" / "pystray", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
    shutil.copyfile(python_license, DEST / "CPython-LICENSE.txt")
for name in ("tcl8.6", "tk8.6"):
    source = Path(sys.base_prefix) / "tcl" / name / "license.terms"
    if source.is_file():
        shutil.copyfile(source, DEST / f"{name}-license.terms")

# These version-specific, verified upstream materials are collected separately.
# Keep builds offline: never silently fetch license text during packaging.
required = (
    "SOURCE-MANIFEST.json", "README.md", "Microsoft-VCRUNTIME-REDISTRIBUTION.md",
    "CPython-3.11.4/INCORPORATED-LICENSES.rst",
    "CPython-3.11.4/libmpdec-LICENSE.txt",
    "CPython-3.11.4/Windows-REDISTRIBUTION.txt",
    "CPython-3.11.4/Windows-runtime-provenance.json",
    "Expat-CPython-3.11.4/COPYING",
    "Tcl-8.6.12.1/license.terms", "Tk-8.6.12.1/license.terms",
    "OpenSSL-1.1.1u/LICENSE", "XZ-5.2.5/COPYING", "libffi-3.4.4/LICENSE",
    "PyInstaller-6.22.3/Apache-2.0.txt",
    "pystray-0.19.5/upstream/setup.py",
    "pystray-0.19.5/upstream/README.rst",
    "pystray-0.19.5/upstream/COPYING",
    "pystray-0.19.5/upstream/COPYING.LGPL",
    "pystray-0.19.5/upstream-files.json",
)
for relative in required:
    if not (DEST / relative).is_file():
        raise RuntimeError(f"Missing distribution material: third_party/{relative}")

manifest = json.loads((DEST / "SOURCE-MANIFEST.json").read_text(encoding="utf-8"))
for record in manifest["records"]:
    target = (DEST / record["path"]).resolve()
    target.relative_to(DEST.resolve())
    # The complete extracted preferred source is required above. Its original
    # source ZIP can be omitted from a source-control checkout if supplied as
    # a separate source artifact; do not make replacing the LGPL library harder.
    if not target.is_file() and record["path"].endswith("upstream-source.zip"):
        continue
    data = target.read_bytes()
    if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise RuntimeError(f"Distribution material checksum mismatch: {record['path']}")

source_manifest = json.loads((DEST / "pystray-0.19.5/upstream-files.json").read_text(encoding="utf-8"))
for record in source_manifest["files"]:
    target = (DEST / record["path"]).resolve()
    target.relative_to(DEST.resolve())
    data = target.read_bytes()
    if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise RuntimeError(f"Upstream source checksum mismatch: {record['path']}")

print("Runtime notices and complete upstream pystray source verified.")
