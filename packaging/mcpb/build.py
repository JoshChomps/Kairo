"""Build a Claude Desktop extension for Kairo.

Two kinds, from the repository root:

- **portable** (``--wheels dist/wheels``): the extension carries Kairo's
  wheels for each platform and a launcher for Windows and one for macOS
  and Linux, which run Kairo with ``uvx`` from those wheels. It works on
  any computer with uv installed; nothing else is needed but KiCad.
- **local** (no ``--wheels``): a launcher with the path of this checkout
  and of ``uv``, for working on Kairo itself; it always runs the code as
  it now is, on this computer only.

::

    uv run python packaging/mcpb/build.py --wheels dist/wheels

Install ``dist/kairo.mcpb`` from Claude Desktop's Settings, Extensions,
Advanced settings, Install Extension. Its icon, a zener diode, is
``icon.png`` beside this file.
"""

from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path

from kairo import __version__
from kairo.server import TOOL_DOCS

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parent.parent


def manifest(default_root: str | None, portable: bool = False) -> dict[str, object]:
    """The extension's manifest, in the format Claude Desktop reads.

    Args:
        default_root: The folder offered as the projects folder, if any.
        portable: True for the extension that carries the wheels and runs on
            Windows, macOS and Linux; False for this checkout's.

    Returns:
        The manifest as JSON-ready data.
    """
    folder: dict[str, object] = {
        "type": "directory",
        "title": "KiCad projects folder",
        "description": "The folder Kairo may read KiCad projects under.",
        "required": True,
    }
    if default_root:
        folder["default"] = default_root
    arguments = [
        "--allowed-root",
        "${user_config.allowed_root}",
        "--tier",
        "${user_config.tier}",
    ]
    if portable:
        # The shell runs the launcher, so it needs no executable bit kept.
        server: dict[str, object] = {
            "type": "binary",
            "entry_point": "launch.sh",
            "mcp_config": {
                "command": "/bin/sh",
                "args": ["${__dirname}/launch.sh", *arguments],
                "platform_overrides": {
                    "win32": {
                        "command": "${__dirname}/launch.cmd",
                        "args": arguments,
                    }
                },
            },
        }
        platforms = ["win32", "darwin", "linux"]
    else:
        server = {
            "type": "binary",
            "entry_point": "launch.cmd",
            "mcp_config": {"command": "${__dirname}/launch.cmd", "args": arguments},
        }
        platforms = ["win32"]
    return {
        "manifest_version": "0.2",
        "name": "kairo",
        "display_name": "Kairo",
        "version": __version__,
        "description": (
            "Design and check KiCad circuits, every number from a sourced "
            "formula or a simulation."
        ),
        "author": {"name": "The Kairo Authors"},
        "license": "Apache-2.0",
        "icon": "icon.png",
        "server": server,
        "tools": [
            {"name": name, "description": doc.what} for name, doc in TOOL_DOCS.items()
        ],
        "user_config": {
            "allowed_root": folder,
            "tier": {
                "type": "string",
                "title": "Tier",
                "description": (
                    "read (look only), propose (also checks and calculations "
                    "that write nothing) or write (also building and changing "
                    "projects)"
                ),
                "required": True,
                "default": "read",
            },
        },
        "compatibility": {"platforms": platforms},
    }


#: Where uv's installer and package managers put uv, for a launcher started
#: by Claude Desktop, which on macOS does not get the shell's PATH.
_UV_PLACES = (
    "$HOME/.local/bin",
    "$HOME/.cargo/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def portable_launchers() -> tuple[str, str]:
    """The Windows and the macOS and Linux launchers of the portable extension.

    Each runs Kairo with ``uvx`` from the wheel beside it for its own
    platform, named by file so that no package of the same name on an
    index can stand in for it; the dependencies come from PyPI. It passes
    the folder it was unpacked in so ``server_info`` can compare the
    extension with the build.

    Returns:
        ``launch.cmd`` and ``launch.sh``, as text.
    """
    windows = (
        "@echo off\r\n"
        'set "PATH=%USERPROFILE%\\.local\\bin;%USERPROFILE%\\.cargo\\bin;%PATH%"\r\n'
        'for %%W in ("%~dp0wheels\\*win_amd64.whl") do set "WHEEL=%%~W"\r\n'
        'uvx --from "%WHEEL%" kairo --bundle-dir "%~dp0." %*\r\n'
    )
    unix = (
        "#!/bin/sh\n"
        f'PATH="{":".join(_UV_PLACES)}:$PATH"\n'
        'here="$(cd "$(dirname "$0")" && pwd)"\n'
        'case "$(uname -s)-$(uname -m)" in\n'
        '  Darwin-arm64) pick="*macosx*arm64.whl" ;;\n'
        '  Darwin-*) pick="*macosx*x86_64.whl" ;;\n'
        '  Linux-aarch64|Linux-arm64) pick="*manylinux*aarch64.whl" ;;\n'
        '  *) pick="*manylinux*x86_64.whl" ;;\n'
        "esac\n"
        'for wheel in "$here"/wheels/$pick; do :; done\n'
        'exec uvx --from "$wheel" kairo --bundle-dir "$here" "$@"\n'
    )
    return windows, unix


def build(
    output: Path,
    default_root: str | None,
    fixed: tuple[tuple[str, str], ...] = (),
    wheels: Path | None = None,
) -> Path:
    """Write the extension file.

    Args:
        output: Where to write it.
        default_root: The folder offered as the projects folder, if any.
        wheels: A folder of Kairo's wheels, to make the portable extension
            that carries them; none for this checkout's.
        fixed: Server options and their values to put in the launcher,
            such as ``("--ngspice", "C:/ngspice/bin/ngspice_con.exe")``.
            They are this machine's paths, so they belong with the
            launcher and not among the settings a user fills in.

    Returns:
        The file written.

    Raises:
        FileNotFoundError: If ``uv`` is not on the path, or ``wheels`` holds
            no wheel.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    if wheels is not None:
        found = sorted(wheels.glob("kairo_eda-*.whl"))
        if not found:
            raise FileNotFoundError(f"no kairo_eda wheel in {wheels}")
        windows, unix = portable_launchers()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(
                "manifest.json",
                json.dumps(manifest(default_root, portable=True), indent=2),
            )
            bundle.writestr("launch.cmd", windows)
            bundle.writestr("launch.sh", unix)
            bundle.write(HERE / "icon.png", "icon.png")
            for wheel in found:
                bundle.write(wheel, f"wheels/{wheel.name}")
        return output
    uv = shutil.which("uv")
    if uv is None:
        raise FileNotFoundError("uv is not on the path")
    # Through the interpreter, not the kairo command: a running kairo.exe
    # cannot be replaced, so uv could not rebuild the environment while a
    # client had the server open.
    options = "".join(f' {name} "{value}"' for name, value in fixed)
    # %~dp0 is the launcher's own folder, where Claude Desktop unpacked the
    # manifest; server_info compares it with the tools this build has. It
    # ends in a backslash, which would escape the closing quote, hence the
    # trailing dot.
    launcher = (
        f'@echo off\r\n"{uv}" run --directory "{REPOSITORY}" python -m kairo'
        f'{options} --bundle-dir "%~dp0." %*\r\n'
    )
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest(default_root), indent=2))
        bundle.writestr("launch.cmd", launcher)
        bundle.write(HERE / "icon.png", "icon.png")
    return output


def main() -> None:
    """Build ``dist/kairo.mcpb`` and say where it is."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--default-root", help="folder offered as the projects folder")
    parser.add_argument(
        "--output", type=Path, default=REPOSITORY / "dist" / "kairo.mcpb"
    )
    parser.add_argument("--ngspice", help="path of ngspice, for simulate")
    parser.add_argument("--xyce", help="path of Xyce, for simulate")
    parser.add_argument("--models-dir", help="folder of registered maker models")
    parser.add_argument(
        "--wheels",
        type=Path,
        help="folder of Kairo's wheels: build the portable extension",
    )
    args = parser.parse_args()
    fixed = tuple(
        (name, value)
        for name, value in (
            ("--ngspice", args.ngspice),
            ("--xyce", args.xyce),
            ("--models-dir", args.models_dir),
        )
        if value
    )
    print(build(args.output, args.default_root, fixed, args.wheels))


if __name__ == "__main__":
    main()
