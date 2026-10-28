"""A stand-in for kicad-cli, for testing the runner's failure handling.

Run as ``python fake_kicad_cli.py MODE [kicad-cli arguments...]``. Whatever
the mode, ``version`` prints the version given by ``FAKE_KICAD_VERSION``
(default ``10.0.6``). With ``FAKE_WRITES_SETTINGS`` set, any other command
first writes ``demo.kicad_prl`` in the working folder, as kicad-cli does.
Otherwise the mode decides what happens:

- ``report:PATH:CODE``: copy the file at PATH to the ``--output`` path and
  exit with CODE.
- ``svg``: write two SVG files into the ``--output`` folder and exit 0.
- ``tools``: answer each command the server uses: ``sch erc`` copies the
  file named by ``FAKE_ERC_REPORT`` and exits with ``FAKE_ERC_EXIT``
  (default 0), ``sch export netlist`` copies ``FAKE_NETLIST``, and
  ``sch export svg`` behaves as ``svg``.
- ``garbage``: write text that is not JSON to ``--output`` and exit 0.
- ``silent``: write nothing and exit 0.
- ``load-fail``: print what kicad-cli prints for an unreadable file, exit 3.
- ``hang``: write a KiCad-style lock file for this machine and user (or
  the user named by ``FAKE_LOCK_USER``) in the working folder, start a
  child that sleeps, write both process ids to ``pids.txt`` there, then
  sleep.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


def output_path(args: list[str]) -> Path:
    return Path(args[args.index("--output") + 1])


def main() -> int:
    mode, args = sys.argv[1], sys.argv[2:]
    if args and args[0] == "version":
        print(os.environ.get("FAKE_KICAD_VERSION", "10.0.6"))
        return 0
    if os.environ.get("FAKE_WRITES_SETTINGS"):
        # kicad-cli 10.0.6 writes this into a project that has none.
        Path("demo.kicad_prl").write_text("{}", encoding="utf-8")
    if mode == "tools":
        if args[:2] == ["sch", "erc"]:
            shutil.copyfile(os.environ["FAKE_ERC_REPORT"], output_path(args))
            return int(os.environ.get("FAKE_ERC_EXIT", "0"))
        if args[:3] == ["sch", "export", "netlist"]:
            shutil.copyfile(os.environ["FAKE_NETLIST"], output_path(args))
            return 0
        mode = "svg"
    if mode == "svg":
        folder = output_path(args)
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("demo.svg", "demo-Power.svg"):
            (folder / name).write_text("<svg/>", encoding="utf-8")
        return 0
    if mode.startswith("report:"):
        source, code = mode.removeprefix("report:").rsplit(":", 1)
        shutil.copyfile(source, output_path(args))
        return int(code)
    if mode == "garbage":
        output_path(args).write_text("this is not json", encoding="utf-8")
        return 0
    if mode == "silent":
        return 0
    if mode == "load-fail":
        print("Failed to load schematic")
        return 3
    if mode == "hang":
        here = Path.cwd()
        lock = {
            "hostname": socket.gethostname().upper(),
            "username": os.environ.get("FAKE_LOCK_USER", getpass.getuser()),
        }
        (here / "~demo.kicad_pro.lck").write_text(json.dumps(lock), encoding="utf-8")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        (here / "pids.txt").write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
        time.sleep(120)
        return 0
    print(f"unknown mode {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
