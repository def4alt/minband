#!/usr/bin/env python3
"""Start every process for one end-to-end MinBand/HardCounter run (P7): edge.py's
detect->track->ground-project loop, the node edge-bridge driving the real WASM Edge, the
bandwidth-capped bridge (loopback or real boards), hub/recon.py's reconstruction, the server,
and (optionally) the viewer -- then print the recon URL and wait.

Fixed ports (same on every run, so they're easy to remember and match the docs):
  7777  MinBand UDP ingest (server)            7788  bridge data ingress (board/loopback side A)
  8080  MinBand WS + HTTP api (server)          7791  bridge depth ingress (board/loopback side A)
  7701  edge.py's track datagrams (edge-bridge) 7790  recon.py's depth ingress (bridge forwards here)
  8090  recon.py SSE                            28765/28766  bridge telemetry, TX/RX side
  5173  viewer dev server

Usage:
  python run_e2e.py --mode loopback [--cap BLE|HF] [--loss 0.0] [--duration 60]
  python run_e2e.py --mode boards   [--cap BLE|HF] [--duration 60]   # needs both boards attached

Ctrl+C shuts every process down (tree-killed on Windows, so no orphaned node/python children --
this harness's own background-task stop does not reliably kill them, see docs/HACKATHON... no,
see the P6 session notes). edge.py's --duration ends the edge run on its own; the rest of the
stack (server/bridge/viewer) stays up afterward so the final twin state is still inspectable --
Ctrl+C when you're done looking.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
RUNS_DIR = REPO / "runs" / "e2e"
WINDOWS = os.name == "nt"


def default_venv_python() -> Path:
    bin_dir = "Scripts" if WINDOWS else "bin"
    exe = "python.exe" if WINDOWS else "python"
    return REPO / ".venv" / bin_dir / exe


def tsx_bin(dir_: Path) -> Path:
    return dir_ / "node_modules" / ".bin" / ("tsx.cmd" if WINDOWS else "tsx")


def vite_bin(dir_: Path) -> Path:
    return dir_ / "node_modules" / ".bin" / ("vite.cmd" if WINDOWS else "vite")


class Proc:
    """One child process, stdout+stderr to its own log file, killed (with children, on
    Windows) on stop -- this harness's background-task stop does not reliably kill a node/python
    child tree, so Ctrl+C here must do it properly."""

    def __init__(self, name: str, cmd: list, cwd: Path, env: dict | None, log_dir: Path):
        self.name = name
        self.log_path = log_dir / f"{name}.log"
        self._log = open(self.log_path, "w", encoding="utf-8")
        full_env = {**os.environ, **(env or {})}
        print(f"-> {name}: {' '.join(str(c) for c in cmd)}  (log: {self.log_path})")
        self.p = subprocess.Popen(cmd, cwd=str(cwd), env=full_env, stdout=self._log, stderr=subprocess.STDOUT)

    def alive(self) -> bool:
        return self.p.poll() is None

    def stop(self):
        if not self.alive():
            return
        if WINDOWS:
            subprocess.run(["taskkill", "/PID", str(self.p.pid), "/T", "/F"], capture_output=True)
        else:
            self.p.terminate()
            try:
                self.p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self._log.close()


def tcp_wait(host: str, port: int, timeout: float = 15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"nothing listening on {host}:{port} after {timeout}s")


def http_wait(url: str, timeout: float = 15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as r:
                if r.status < 500:
                    return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"{url} not responding after {timeout}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["loopback", "boards"], default="loopback")
    ap.add_argument("--cap", choices=["BLE", "HF"], default="BLE")
    ap.add_argument("--loss", type=float, default=0.0, help="loopback only: fraction of frames dropped (0..1)")
    ap.add_argument("--duration", type=float, default=60.0, help="edge.py's run length, seconds")
    ap.add_argument("--clip", default=None, help="absolute path; default is edge.py's own default (MEVA clip)")
    ap.add_argument("--cache-dir", default=None, help="absolute path; default is edge.py's own default")
    ap.add_argument("--venv-python", default=None, help="override the .venv interpreter edge.py runs under")
    ap.add_argument("--no-viewer", action="store_true", help="skip starting the vite dev server")
    args = ap.parse_args()

    venv_python = Path(args.venv_python) if args.venv_python else default_venv_python()
    if not venv_python.exists():
        print(f"run_e2e: no venv python at {venv_python} (edge.py needs clika_runtime/opencv there) "
              f"-- pass --venv-python", file=sys.stderr)
        return 2

    log_dir = RUNS_DIR / time.strftime("%Y%m%d-%H%M%S")
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"run_e2e: mode={args.mode} cap={args.cap} logs={log_dir}")

    procs: list[Proc] = []

    def spawn(name, cmd, cwd, env=None):
        p = Proc(name, cmd, cwd, env, log_dir)
        procs.append(p)
        return p

    def stop_all():
        print("run_e2e: stopping...")
        for p in reversed(procs):
            p.stop()

    try:
        # 1. server: UDP ingest + WS/HTTP twin.
        spawn("server", [str(tsx_bin(REPO / "server")), "src/main.ts"], REPO / "server",
              {"MINBAND_UDP_PORT": "7777", "MINBAND_WS_PORT": "8080"})
        http_wait("http://127.0.0.1:8080/api/metrics")

        # 2. bridge(s): loopback is one process; boards is two (TX on board A's COM, RX on board B's).
        if args.mode == "loopback":
            spawn("bridge", [
                str(venv_python), "bridge.py", "--loopback",
                "--udp-listen", "7788", "--udp-forward", "127.0.0.1:7777",
                "--udp-listen-depth", "7791", "--udp-forward-depth", "127.0.0.1:7790",
                "--rate-preset", args.cap, "--loopback-loss", str(args.loss),
                "--http-port", "28765",
            ], REPO / "nucode")
            tcp_wait("127.0.0.1", 28765)
        else:
            boards = json.loads((REPO / "nucode" / "boards.json").read_text())
            import serial.tools.list_ports as list_ports  # global python; pyserial already a dep
            ports = {p.serial_number: p.device for p in list_ports.comports()}
            tx_port = ports.get(boards["tx"]["usb_serial"], boards["tx"]["last_known_port"])
            rx_port = ports.get(boards["rx"]["usb_serial"], boards["rx"]["last_known_port"])
            print(f"run_e2e: boards -- TX {tx_port}, RX {rx_port} (re-resolved by USB serial)")
            spawn("bridge-rx", [
                sys.executable, "bridge.py", "--serial", rx_port,
                "--udp-forward", "127.0.0.1:7777", "--udp-forward-depth", "127.0.0.1:7790",
                "--rate-preset", args.cap, "--http-port", "28766",
            ], REPO / "nucode")
            spawn("bridge-tx", [
                sys.executable, "bridge.py", "--serial", tx_port,
                "--udp-listen", "7788", "--udp-listen-depth", "7791",
                "--http-port", "28765",
            ], REPO / "nucode")
            tcp_wait("127.0.0.1", 28765)
            tcp_wait("127.0.0.1", 28766)

        # 3. recon: depth-chunk reassembly + voxel SSE. (A plain GET to "/" streams forever, same
        # as "/events" -- tcp_wait only, an http_wait here would hang on the open connection.)
        spawn("recon", [sys.executable, "recon.py", "--listen-port", "7790", "--sse-port", "8090"], REPO / "hub")
        tcp_wait("127.0.0.1", 8090)

        # 4. edge-bridge: real WASM Edge between edge.py's tracks and the bridge's data ingress.
        # Needs the server already up for the Ack handshake (WasmEdge sends nothing past Hello
        # until it gets one back -- core/src/edge.rs:403).
        telemetry_port = 28765
        spawn("edge-bridge", [str(tsx_bin(REPO / "server")), "src/edge-bridge.ts"], REPO / "server", {
            "TRACK_PORT": "7701", "DEST": "127.0.0.1:7788", "DEVICE_ID": "1",
            "TELEMETRY_URL": f"http://127.0.0.1:{telemetry_port}/telemetry",
        })
        time.sleep(1.0)  # UDP listener; no portable "is it bound yet" check worth adding here

        # 5. viewer: optional, started before edge.py so the URL is up well ahead of a short
        # --duration run rather than racing it (edge.py's own ClikaRT cold-start takes a few
        # seconds).
        if not args.no_viewer:
            spawn("viewer", [str(vite_bin(REPO / "viewer")), "--port", "5173", "--strictPort", "--host", "127.0.0.1"], REPO / "viewer", {
                "VITE_WS_URL": "ws://localhost:8080",
                "VITE_RECON_URL": "http://localhost:8090",
                "VITE_TELEMETRY_URL": f"http://localhost:{telemetry_port}",
            })
            http_wait("http://127.0.0.1:5173/recon.html")
            print("\nrun_e2e: watch it at http://localhost:5173/recon.html\n")
        else:
            print("\nrun_e2e: stack is up (no viewer); recon SSE on http://localhost:8090/events\n")

        # 6. edge.py: the producer. Everything downstream is already listening by now.
        edge_cmd = [str(venv_python), "edge.py", "--track-port", "7701", "--depth-port", "7791",
                    "--duration", str(args.duration), "--loop"]
        if args.clip:
            edge_cmd += ["--clip", args.clip]
        if args.cache_dir:
            edge_cmd += ["--cache-dir", args.cache_dir]
        edge_proc = spawn("edge", edge_cmd, REPO / "edge")

        while edge_proc.alive():
            time.sleep(1.0)
        print(f"run_e2e: edge run finished ({args.duration:.0f}s elapsed). "
              f"Rest of the stack is still up to inspect -- Ctrl+C to stop everything.")
        while True:
            time.sleep(1.0)
    except TimeoutError as e:
        print(f"run_e2e: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
