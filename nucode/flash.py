#!/usr/bin/env python3
"""Flash a role (tx|rx) onto its NUCODE NU-40 board via arduino-cli.

Usage: python flash.py tx|rx [--sketch <dir>]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import serial.tools.list_ports

HERE = Path(__file__).resolve().parent
BOARDS_JSON = HERE / "boards.json"
ARDUINO_CLI = "arduino-cli"

DEFAULT_SKETCH = {
    "tx": HERE / "fw" / "tx_central",
    "rx": HERE / "fw" / "rx_peripheral",
}


def resolve_port(usb_serial: str) -> str | None:
    for p in serial.tools.list_ports.comports():
        if p.serial_number == usb_serial:
            return p.device
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("role", choices=["tx", "rx"])
    ap.add_argument("--sketch", type=Path, default=None)
    args = ap.parse_args()

    boards = json.loads(BOARDS_JSON.read_text())
    fqbn = boards["fqbn"]
    entry = boards[args.role]

    port = resolve_port(entry["usb_serial"])
    if port is None:
        port = entry["last_known_port"]
        print(f"warning: board {entry['usb_serial']} not found live, "
              f"falling back to last_known_port {port}", file=sys.stderr)
    elif port != entry["last_known_port"]:
        entry["last_known_port"] = port
        BOARDS_JSON.write_text(json.dumps(boards, indent=2) + "\n")

    sketch = args.sketch or DEFAULT_SKETCH[args.role]

    compile_cmd = [ARDUINO_CLI, "compile", "--fqbn", fqbn,
                   "--libraries", str(HERE / "fw" / "libraries"), str(sketch)]
    r = subprocess.run(compile_cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        return r.returncode

    upload_cmd = [ARDUINO_CLI, "upload", "-p", port, "--fqbn", fqbn, str(sketch)]
    r = subprocess.run(upload_cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        return r.returncode

    print(f"flashed {args.role} ({sketch}) on {port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
