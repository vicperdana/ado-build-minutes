"""Tiny PEP 517/660 build backend for this dependency-light CLI package.

It avoids a build-time dependency download, which is useful for customer
environments where runtime dependencies are allowed but build isolation cannot
reach files.pythonhosted.org for setuptools/hatchling wheels.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
import csv
import io
import zipfile

NAME = "ado-build-minutes"
NORMALIZED = "ado_build_minutes"
VERSION = "0.1.0"
DIST_INFO = f"{NORMALIZED}-{VERSION}.dist-info"
SRC = Path("src")
PACKAGE = SRC / "ado_build_minutes"
LICENSE_PATH = Path("LICENSE")

RUNTIME_DEPS: list[str] = []
RUNTIME_EXTRA_DEPS = [
    "azure-identity>=1.16,<2",
    "httpx>=0.27,<1",
    "rich>=13,<15",
]
DEV_DEPS = ["pytest>=8,<9", "pytest-asyncio>=0.23,<1"]


def get_requires_for_build_wheel(config_settings=None):  # noqa: ANN001
    """Return build requirements for standard wheels."""
    return []


def get_requires_for_build_editable(config_settings=None):  # noqa: ANN001
    """Return build requirements for editable wheels."""
    return []


def _metadata() -> str:
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {NAME}",
        f"Version: {VERSION}",
        "Summary: Extract Azure DevOps build minutes by runner type across organisations",
        "License-Expression: MIT",
        "License-File: LICENSE",
        "Requires-Python: >=3.11",
    ]
    lines.extend(f"Requires-Dist: {dep}" for dep in RUNTIME_DEPS)
    lines.append("Provides-Extra: runtime")
    lines.extend(f'Requires-Dist: {dep}; extra == "runtime"' for dep in RUNTIME_EXTRA_DEPS)
    lines.append("Provides-Extra: dev")
    lines.extend(f'Requires-Dist: {dep}; extra == "dev"' for dep in DEV_DEPS)
    return "\n".join(lines) + "\n"


def _wheel() -> str:
    return "\n".join(
        [
            "Wheel-Version: 1.0",
            "Generator: ado-build-minutes-build-backend",
            "Root-Is-Purelib: true",
            "Tag: py3-none-any",
            "",
        ]
    )


def _entry_points() -> str:
    return "[console_scripts]\nado-build-minutes = ado_build_minutes.cli:main\n"


def _write_dist_info(metadata_directory: Path) -> str:
    dist = metadata_directory / DIST_INFO
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "METADATA").write_text(_metadata(), encoding="utf-8")
    (dist / "WHEEL").write_text(_wheel(), encoding="utf-8")
    (dist / "entry_points.txt").write_text(_entry_points(), encoding="utf-8")
    licenses = dist / "licenses"
    licenses.mkdir(exist_ok=True)
    (licenses / "LICENSE").write_text(LICENSE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return DIST_INFO


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):  # noqa: ANN001
    """Prepare wheel metadata."""
    return _write_dist_info(Path(metadata_directory))


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):  # noqa: ANN001
    """Prepare editable wheel metadata."""
    return _write_dist_info(Path(metadata_directory))


def _hash(data: bytes) -> str:
    digest = urlsafe_b64encode(sha256(data).digest()).decode("ascii").rstrip("=")
    return f"sha256={digest}"


def _build_wheel(wheel_directory: str, editable: bool) -> str:
    wheel_name = f"{NORMALIZED}-{VERSION}-py3-none-any.whl"
    wheel_path = Path(wheel_directory) / wheel_name
    records: list[tuple[str, str, str]] = []

    def write(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
        zf.writestr(arcname, data)
        records.append((arcname, _hash(data), str(len(data))))

    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if editable:
            pth = str((Path.cwd() / SRC).resolve()) + "\n"
            write(zf, f"{NORMALIZED}.pth", pth.encode("utf-8"))
        else:
            for file_path in PACKAGE.rglob("*.py"):
                write(zf, str(file_path.relative_to(SRC)), file_path.read_bytes())
        write(zf, f"{DIST_INFO}/METADATA", _metadata().encode("utf-8"))
        write(zf, f"{DIST_INFO}/WHEEL", _wheel().encode("utf-8"))
        write(zf, f"{DIST_INFO}/entry_points.txt", _entry_points().encode("utf-8"))
        write(zf, f"{DIST_INFO}/licenses/LICENSE", LICENSE_PATH.read_bytes())
        record_name = f"{DIST_INFO}/RECORD"
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        for row in records:
            writer.writerow(row)
        writer.writerow((record_name, "", ""))
        zf.writestr(record_name, output.getvalue().encode("utf-8"))
    return wheel_name


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):  # noqa: ANN001
    """Build a pure-Python wheel."""
    return _build_wheel(wheel_directory, editable=False)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):  # noqa: ANN001
    """Build an editable wheel pointing at ./src."""
    return _build_wheel(wheel_directory, editable=True)
