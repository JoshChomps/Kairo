"""The ``kairo`` command: start the MCP server on standard input and output."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from kairo import __version__
from kairo.guard import CALLS_PER_WINDOW
from kairo.server import TIERS, Settings, build_server, default_cache_dir


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``kairo`` command.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="kairo",
        description=(
            "MCP server that turns design intent into verified KiCad schematics. "
            "Run with no arguments it serves the current folder over stdio."
        ),
    )
    parser.add_argument("--version", action="version", version=f"kairo {__version__}")
    parser.add_argument(
        "--allowed-root",
        action="append",
        type=Path,
        metavar="DIR",
        help=(
            "a folder the tools may read KiCad projects under; may be given "
            "more than once (default: the current folder)"
        ),
    )
    parser.add_argument(
        "--kicad-cli",
        type=Path,
        metavar="PATH",
        help="the kicad-cli program to use (default: found automatically)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        metavar="DIR",
        help="where the library index and drawings are kept",
    )
    parser.add_argument(
        "--tier",
        choices=TIERS,
        default="read",
        help=(
            "the highest tier of tools to offer: read (look only), propose "
            "(also checks and calculations that write nothing) or write (also "
            "building and changing projects); default: read"
        ),
    )
    parser.add_argument(
        "--parts-dir",
        type=Path,
        metavar="DIR",
        help="a folder of datasheet facts, one JSON file per part number",
    )
    parser.add_argument(
        "--parts-catalogue",
        type=Path,
        metavar="FILE",
        help="a parts catalogue built by kairo.parts.catalogue; without it, "
        "parts-catalogue.sqlite3 in the cache folder if there is one",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        metavar="DIR",
        help="a folder of registered simulation models (see kairo.spice.registry)",
    )
    parser.add_argument(
        "--ngspice",
        type=Path,
        metavar="PATH",
        help="the ngspice program to use (default: KAIRO_NGSPICE, then PATH)",
    )
    parser.add_argument(
        "--xyce",
        type=Path,
        metavar="PATH",
        help="the Xyce program to use (default: KAIRO_XYCE, then PATH)",
    )
    parser.add_argument(
        "--calls-per-ten-minutes",
        type=int,
        default=CALLS_PER_WINDOW,
        metavar="N",
        help=(
            "the most tool calls accepted in any ten minutes, so a model "
            "in a loop is stopped (default: %(default)s; 0 for no limit, "
            "for a model that costs nothing to run)"
        ),
    )
    parser.add_argument(
        "--tools-dir",
        type=Path,
        metavar="DIR",
        help=(
            "the folder holding freerouting-<version>.jar and a jdk-25 or "
            "newer folder, for make_board (default: CACHE_DIR/tools)"
        ),
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        metavar="DIR",
        help=(
            "the Claude Desktop extension's own folder, passed by its "
            "launcher so server_info can check its manifest"
        ),
    )
    return parser


def settings_from(args: argparse.Namespace) -> Settings:
    """Turn parsed arguments into the server's settings.

    Args:
        args: What `build_parser` parsed.

    Returns:
        The settings, with defaults filled in.
    """
    roots: list[Path] = args.allowed_root or [Path.cwd()]
    cache: Path = args.cache_dir or default_cache_dir(os.environ)
    kicad_cli: Path | None = args.kicad_cli
    return Settings(
        allowed_roots=tuple(roots),
        cache_dir=cache,
        kicad_cli=kicad_cli,
        tier=args.tier,
        parts_dir=args.parts_dir,
        parts_catalogue=args.parts_catalogue,
        ngspice=args.ngspice,
        xyce=args.xyce,
        models_dir=args.models_dir,
        calls_per_ten_minutes=max(args.calls_per_ten_minutes, 0),
        bundle_dir=args.bundle_dir,
        tools_dir=args.tools_dir,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``kairo`` command.

    Args:
        argv: Command-line arguments without the program name. ``None`` reads
            them from ``sys.argv``.

    Returns:
        The process exit code, once the client closes the connection.
        ``--help`` and ``--version`` exit through argparse with code 0.
    """
    args = build_parser().parse_args(argv)
    build_server(settings_from(args)).run("stdio")
    return 0
