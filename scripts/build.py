"""Local build script for Pony Express standalone executables.

Produces platform-appropriate artifacts in artifacts/:

  Windows  pony-windows-vX.Y.Z.zip          (portable, for Scoop)
           pony-windows-vX.Y.Z-setup.exe    (Inno Setup installer)
  macOS    pony-macos-vX.Y.Z.tar.gz         (portable, for Homebrew)
           pony-macos-vX.Y.Z.dmg            (drag-to-Applications)
  Linux    pony-linux-vX.Y.Z.tar.gz         (portable)
           pony-linux-vX.Y.Z.AppImage       (self-contained installer)

Usage:
    uv run python scripts/build.py [options]

Options:
    --skip-tests    Skip pytest
    --skip-docs     Skip mkdocs build (site/ must already exist)
    --installer     Also build the platform-specific installer
    --version VER   Override version (default: read from src/pony/version.py)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
DIST_DIR = REPO_ROOT / "dist" / "pony"
ARTIFACTS_DIR = REPO_ROOT / "artifacts"

# appimagetool release URL (x86_64 Linux)
APPIMAGETOOL_URL = (
    "https://github.com/AppImage/AppImageKit/releases/download/continuous/"
    "appimagetool-x86_64.AppImage"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(*cmd: str, cwd: Path | None = None) -> None:
    """Run a subprocess and raise on failure."""
    print(f"+ {' '.join(cmd)}", flush=True)
    subprocess.run(list(cmd), check=True, cwd=cwd or REPO_ROOT)


def current_platform() -> str:
    s = platform.system()
    if s == "Windows":
        return "windows"
    if s == "Darwin":
        return "macos"
    return "linux"


def read_version(override: str | None) -> str:
    if override:
        return override
    ver_file = REPO_ROOT / "src" / "pony" / "version.py"
    text = ver_file.read_text(encoding="utf-8")
    m = re.search(r'__version__:\s*str\s*=\s*"([^"]+)"', text)
    if not m:
        sys.exit("ERROR: could not read version from src/pony/version.py")
    return m.group(1)


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------


def run_tests() -> None:
    print("\n=== Running tests ===", flush=True)
    run("uv", "run", "python", "-m", "pytest")


def build_docs() -> None:
    print("\n=== Building documentation ===", flush=True)
    run("uv", "run", "mkdocs", "build", "--strict")


def build_binary() -> None:
    print("\n=== Building binary with PyInstaller ===", flush=True)
    run("uv", "run", "pyinstaller", "pony.spec", "--noconfirm", "--clean")


def package_archive(version: str, plat: str) -> Path:
    """Create the portable archive and return its path."""
    print(f"\n=== Packaging portable archive ({plat}) ===", flush=True)
    ARTIFACTS_DIR.mkdir(exist_ok=True)

    if plat == "windows":
        archive_name = f"pony-{plat}-v{version}.zip"
        archive_path = ARTIFACTS_DIR / archive_name
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as z:
            for path in DIST_DIR.rglob("*"):
                z.write(path, path.relative_to(DIST_DIR.parent))
    else:
        archive_name = f"pony-{plat}-v{version}.tar.gz"
        archive_path = ARTIFACTS_DIR / archive_name
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(DIST_DIR, arcname=DIST_DIR.name)

    print(f"Archive: {archive_path}", flush=True)
    return archive_path


def build_windows_installer(version: str) -> Path:
    """Build an Inno Setup installer and return its path."""
    print("\n=== Building Windows installer (Inno Setup) ===", flush=True)
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    iss = REPO_ROOT / "installers" / "windows" / "pony.iss"
    dist_dir = str(DIST_DIR)
    iscc = shutil.which("iscc") or shutil.which("ISCC")
    if iscc is None:
        # Check the default Inno Setup installation directories on Windows.
        # Winget installs per-user to %LOCALAPPDATA%\Programs; the traditional
        # EXE/MSI installer uses Program Files (x86).
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        for candidate in [
            os.path.join(local_appdata, "Programs", "Inno Setup 6", "ISCC.exe"),
            r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            r"C:\Program Files\Inno Setup 6\ISCC.exe",
        ]:
            if Path(candidate).is_file():
                iscc = candidate
                break
    if iscc is None:
        sys.exit(
            "ERROR: iscc not found. Install Inno Setup 6+ from "
            "https://jrsoftware.org/isinfo.php and ensure it is on PATH."
        )
    run(
        iscc,
        f"/DAppVersion={version}",
        f"/DDistDir={dist_dir}",
        f"/O{ARTIFACTS_DIR}",
        str(iss),
    )
    installer_path = ARTIFACTS_DIR / f"pony-windows-v{version}-setup.exe"
    print(f"Installer: {installer_path}", flush=True)
    return installer_path


def build_macos_dmg(version: str) -> Path:
    """Build a macOS DMG and return its path."""
    print("\n=== Building macOS DMG ===", flush=True)
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    staging = REPO_ROOT / "dmg-staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        shutil.copytree(DIST_DIR, staging / "Pony Express")
        (staging / "Applications").symlink_to("/Applications")
        dmg_path = ARTIFACTS_DIR / f"pony-macos-v{version}.dmg"
        run(
            "hdiutil",
            "create",
            "-volname",
            "Pony Express",
            "-srcfolder",
            str(staging),
            "-ov",
            "-format",
            "UDZO",
            str(dmg_path),
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(f"DMG: {dmg_path}", flush=True)
    return dmg_path


def build_linux_appimage(version: str) -> Path:
    """Build a Linux AppImage and return its path."""
    print("\n=== Building Linux AppImage ===", flush=True)
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    appdir = REPO_ROOT / "AppDir"
    if appdir.exists():
        shutil.rmtree(appdir)

    try:
        bundle_dest = appdir / "usr" / "bin" / "pony_bundle"
        shutil.copytree(DIST_DIR, bundle_dest)

        apprun = appdir / "AppRun"
        apprun.write_text(
            "#!/bin/sh\n"
            'HERE="$(dirname "$(readlink -f "$0")")"\n'
            'exec "$HERE/usr/bin/pony_bundle/pony" "$@"\n',
            encoding="utf-8",
        )
        apprun.chmod(apprun.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        shutil.copy(
            REPO_ROOT / "installers" / "linux" / "pony.desktop",
            appdir / "pony.desktop",
        )
        shutil.copy(
            REPO_ROOT / "icons" / "pony-express.png",
            appdir / "pony-express.png",
        )

        tool = REPO_ROOT / "appimagetool"
        if not tool.exists():
            print("Downloading appimagetool...", flush=True)
            urllib.request.urlretrieve(APPIMAGETOOL_URL, str(tool))
            tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        appimage_path = ARTIFACTS_DIR / f"pony-linux-v{version}.AppImage"
        # APPIMAGE_EXTRACT_AND_RUN=1 makes appimagetool self-extract rather
        # than mount via FUSE, which avoids needing libfuse on the host
        # (required on GitHub Actions ubuntu runners and many CI environments).
        env = {**os.environ, "ARCH": "x86_64", "APPIMAGE_EXTRACT_AND_RUN": "1"}
        subprocess.run(
            [str(tool), str(appdir), str(appimage_path)],
            check=True,
            cwd=REPO_ROOT,
            env=env,
        )
    finally:
        shutil.rmtree(appdir, ignore_errors=True)

    print(f"AppImage: {appimage_path}", flush=True)
    return appimage_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build Pony Express standalone executables.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--skip-tests", action="store_true", help="Skip pytest.")
    parser.add_argument(
        "--skip-docs",
        action="store_true",
        help="Skip mkdocs build (site/ must already exist).",
    )
    parser.add_argument(
        "--installer",
        action="store_true",
        help="Also build the platform-specific installer.",
    )
    parser.add_argument(
        "--version",
        metavar="VER",
        help="Override version string.",
    )
    args = parser.parse_args()

    os.chdir(REPO_ROOT)

    version = read_version(args.version)
    plat = current_platform()
    print(f"Building Pony Express v{version} for {plat}", flush=True)

    if not args.skip_tests:
        run_tests()

    if not args.skip_docs:
        build_docs()

    build_binary()

    artifacts: dict[str, str] = {}
    archive_path = package_archive(version, plat)
    artifacts["archive"] = str(archive_path)

    if args.installer:
        if plat == "windows":
            installer_path = build_windows_installer(version)
        elif plat == "macos":
            installer_path = build_macos_dmg(version)
        else:
            installer_path = build_linux_appimage(version)
        artifacts["installer"] = str(installer_path)

    manifest = ARTIFACTS_DIR / "artifacts.json"
    manifest.write_text(json.dumps(artifacts, indent=2), encoding="utf-8")

    print("\n=== Done ===", flush=True)
    for kind, path in artifacts.items():
        print(f"  {kind}: {path}")


if __name__ == "__main__":
    main()
