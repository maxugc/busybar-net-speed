#!/usr/bin/env python3
"""
Net Speed helper: runs internet speed tests for the Net Speed app on a BUSY Bar.

A BUSY Bar can't test your connection itself: an app on the bar moves data at
under 1 Mbps. So this program does the measuring, on a computer that is on the
same network as the bar (wired if you can), and the bar is the button and the
screen.

    python3 netspeed_helper.py

Leave it running. It needs Python 3.8 or newer and nothing else.

What it does:
  - Finds the bar (USB address, busybar.local, then a look around the local
    network) and writes its own address into the app's storage on the bar, so
    the app knows where to ask. It repeats that every minute.
  - Answers the app on --port:
        /run      start a test
        /status   how the running test is going
        /stats    last result, averages, fastest and slowest
  - Runs the test: ping, then download, then upload.
        built-in (default): against Cloudflare's public speed test, several
            connections at once. Fine for most lines. On very fast lines
            Cloudflare may refuse several tests in a row.
        --engine ookla: with Ookla's Speedtest CLI, if you have installed it
            and accepted its license yourself (run "speedtest" once).
  - Keeps every result in a file in --data (default ~/.busybar-net-speed).

One thing it measures about itself: on a small computer and a very fast line
(a Raspberry Pi 4 on gigabit, for one), the download can use the whole network
card, and for those seconds nothing else gets through to the helper. It checks
for that during each test and tells the app, which then shows the download by
the clock instead of asking for numbers it can't get.

MIT licensed.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import pty
except ImportError:   # Windows
    pty = None

VERSION = "1.0.0"
APP_ID = "io.github.maxugc.netspeed"
APP_DATA = f"/ext/apps_data/js_apps/{APP_ID}"
DEFAULT_PORT = 8791

# Built-in test
HOST = "speed.cloudflare.com"   # plain HTTP: no encryption cost on small computers
HEADERS = {"User-Agent": f"busybar-net-speed/{VERSION}", "Accept-Encoding": "identity"}
CONNECTIONS = 4
PHASE_SECONDS = 6.0
RAMP_SECONDS = 1.5
# Few, long transfers: Cloudflare stalls a client that makes many requests in
# quick succession and refuses downloads of 100 MB or more.
DOWN_BYTES = 50_000_000
UP_BYTES = 50_000_000
PING_COUNT = 10
BUSY = "Test server is busy. Wait a few minutes."

OOKLA_TIMEOUT = 90
RUNNING = ("ping", "download", "upload")


class Config:
    bar = None            # the bar's address, once found
    bar_given = None
    password = None
    port = DEFAULT_PORT
    engine = "builtin"
    ookla = None          # path of Ookla's speedtest program, with --engine ookla
    results = None        # Path of the results file
    min_gap = 20          # seconds between tests


_lock = threading.Lock()
_state = {"state": "idle", "id": 0, "progress": 0.0, "live": 0.0, "ping": None, "down": None, "up": None, "error": None,
          "expect": None, "started": None}
_finished = {"at": -1e9}


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


def set_state(**changes) -> None:
    with _lock:
        _state.update(changes)


def get_state() -> dict:
    with _lock:
        s = dict(_state)
    started = s.pop("started")
    if started:
        s["elapsed"] = round(time.time() - started, 1)
    return s


def plain(value, limit: int = 16) -> str:
    """Printable ASCII only: the bar's fonts can't draw anything else."""
    return "".join(c for c in str(value or "") if " " <= c <= "~").strip()[:limit]


# ---------------------------------------------------------------- the bar

def bar_request(host: str, method: str, path: str, data: bytes | None = None, timeout: float = 10):
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/octet-stream"
    if Config.password:
        headers["X-API-Token"] = Config.password
    req = urllib.request.Request(f"http://{host}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def is_bar(host: str) -> bool:
    try:
        status = json.loads(bar_request(host, "GET", "/api/status", timeout=1.5))
        return "device" in status and "firmware" in status
    except Exception:
        return False


def local_networks() -> list:
    """The /24 networks this computer is on."""
    addresses = set()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))   # picks the outgoing interface; sends nothing
        addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    return [ipaddress.ip_network(a + "/24", strict=False) for a in sorted(addresses) if not a.startswith("127.")]


def numeric(host: str) -> str:
    """The numeric address behind a name. Everything later uses this: looking
    a name up can stall for seconds while a test fills the network card."""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


def find_bar() -> str | None:
    if Config.bar_given:
        return numeric(Config.bar_given) if is_bar(Config.bar_given) else None
    for host in ("10.0.4.20", "busybar.local"):   # USB, then the bar's usual name on Wi-Fi
        if is_bar(host):
            return numeric(host)
    for network in local_networks():
        hosts = [str(h) for h in network.hosts()]
        with ThreadPoolExecutor(max_workers=64) as pool:
            for host, found in zip(hosts, pool.map(is_bar, hosts)):
                if found:
                    return host
    return None


def my_address(bar: str) -> str:
    """This computer's address on the network the bar is on."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((bar, 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def announce_loop() -> None:
    """Keeps the app on the bar informed of where this helper is."""
    written, written_at, complained = None, 0.0, False
    while True:
        try:
            if not Config.bar or not is_bar(Config.bar):
                Config.bar = find_bar()
                written = None
                if Config.bar:
                    log(f"Found the BUSY Bar at {Config.bar}")
                    complained = False
                elif not complained:
                    where = Config.bar_given or "the USB address, busybar.local or the local network"
                    log(f"No BUSY Bar found at {where}. Still looking. If yours has a password or another address, use --password and --bar.")
                    complained = True
            if Config.bar:
                url = f"http://{my_address(Config.bar)}:{Config.port}"
                if url != written or time.monotonic() - written_at > 1800:
                    try:
                        bar_request(Config.bar, "POST", f"/api/storage/mkdir?path={APP_DATA}", b"")
                    except urllib.error.HTTPError:
                        pass   # the folder is already there
                    bar_request(Config.bar, "POST", f"/api/storage/write?path={APP_DATA}/helper.json", json.dumps({"url": url}).encode())
                    if url != written:
                        log(f"Told the bar that the helper is at {url}. Open Net Speed on the bar and press Start.")
                    written, written_at = url, time.monotonic()
        except Exception as e:
            log(f"Could not update the bar ({type(e).__name__}: {e}). Trying again in a minute.")
            written = None
        time.sleep(60 if Config.bar else 20)


def watch_reachability(flag: dict) -> None:
    """A couple of seconds into the download, checks whether this computer can
    still reach the bar. If not, the app can't reach the helper either."""
    time.sleep(2.0)
    if not Config.bar:
        return
    for _ in range(2):
        try:
            socket.create_connection((Config.bar, 80), timeout=1.0).close()
            return
        except OSError:
            pass
    flag["quiet"] = True


# ---------------------------------------------------------------- built-in test (Cloudflare)

class Counter:
    def __init__(self):
        self.n = 0
        self._lock = threading.Lock()

    def add(self, k: int) -> None:
        with self._lock:
            self.n += k


def measure_ping() -> tuple:
    """Time to open a connection to the test server (one network round trip),
    PING_COUNT times. Returns (median ms, jitter ms)."""
    ip = socket.getaddrinfo(HOST, 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    times = []
    for i in range(PING_COUNT + 1):
        t0 = time.perf_counter()
        sock = socket.create_connection((ip, 80), timeout=3)
        ms = (time.perf_counter() - t0) * 1000
        sock.close()
        if i:   # the first one warms things up
            times.append(ms)
        set_state(progress=(i + 1) / (PING_COUNT + 1))
        time.sleep(0.05)
    jitter = statistics.mean(abs(x - y) for x, y in zip(times, times[1:]))
    return statistics.median(times), jitter


def download_worker(stop_at: float, counter: Counter, errors: list) -> None:
    while time.monotonic() < stop_at:
        conn = None
        try:
            conn = http.client.HTTPConnection(HOST, timeout=10)
            conn.request("GET", f"/__down?bytes={DOWN_BYTES}", headers=HEADERS)
            resp = conn.getresponse()
            if resp.status != 200:
                raise RuntimeError(f"download refused ({resp.status})")
            while time.monotonic() < stop_at:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                counter.add(len(chunk))
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
            time.sleep(0.3)
        finally:
            if conn:
                conn.close()


def upload_worker(stop_at: float, counter: Counter, errors: list) -> None:
    chunk = b"\0" * 65536
    while time.monotonic() < stop_at:
        conn = None
        try:
            conn = http.client.HTTPConnection(HOST, timeout=10)
            conn.putrequest("POST", "/__up")
            conn.putheader("User-Agent", HEADERS["User-Agent"])
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(UP_BYTES))
            conn.endheaders()
            sent = 0
            while sent < UP_BYTES and time.monotonic() < stop_at:
                piece = chunk[: min(len(chunk), UP_BYTES - sent)]
                conn.send(piece)
                sent += len(piece)
                counter.add(len(piece))
            if sent >= UP_BYTES:
                resp = conn.getresponse()
                resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"upload refused ({resp.status})")
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
            time.sleep(0.3)
        finally:
            if conn:
                conn.close()


def run_phase(worker, name: str) -> float:
    """Runs CONNECTIONS workers for PHASE_SECONDS and returns Mbps, measured
    after the ramp-up. Bytes are counted as they move, sampled 10 times a second.
    (Upload bytes are counted when handed to the system; over the steady part
    of the test that matches what the far end receives.)"""
    counter, errors = Counter(), []
    start = time.monotonic()
    stop_at = start + PHASE_SECONDS
    threads = [threading.Thread(target=worker, args=(stop_at, counter, errors), daemon=True) for _ in range(CONNECTIONS)]
    for t in threads:
        t.start()
    samples = []
    while True:
        now = time.monotonic()
        samples.append((now, counter.n))
        back = next(s for s in samples if s[0] >= now - 1.0)
        live = (counter.n - back[1]) * 8 / (now - back[0]) / 1e6 if now - back[0] > 0.25 else 0.0
        set_state(state=name, progress=min((now - start) / PHASE_SECONDS, 1.0), live=round(live, 1))
        if now >= stop_at:
            break
        time.sleep(0.1)
    for t in threads:
        t.join(timeout=3)
    ramp = next(s for s in samples if s[0] >= start + RAMP_SECONDS)
    end = samples[-1]
    if any("(429)" in e for e in errors):
        raise RuntimeError(BUSY)   # throttled: the number would be too low, so don't keep it
    if end[1] - ramp[1] <= 0:
        raise RuntimeError(errors[0] if errors else f"no data moved during {name}")
    return (end[1] - ramp[1]) * 8 / (end[0] - ramp[0]) / 1e6


def builtin_test() -> dict:
    began = time.monotonic()
    ping, jitter = measure_ping()
    set_state(ping=round(ping, 1))
    flag = {}
    download_at = time.monotonic()
    threading.Thread(target=watch_reachability, args=(flag,), daemon=True).start()
    down = run_phase(download_worker, "download")
    set_state(down=round(down, 1))
    upload_at = time.monotonic()
    up = run_phase(upload_worker, "upload")
    row = {"down": round(down, 1), "up": round(up, 1), "ping": round(ping, 1), "jitter": round(jitter, 1), "via": "cloudflare",
           "ping_s": round(download_at - began, 1), "dl_s": round(upload_at - download_at, 1)}
    if flag.get("quiet"):
        row["quiet"] = True
    return row


# ---------------------------------------------------------------- Ookla's Speedtest CLI (optional)

def find_ookla() -> str | None:
    """Path of Ookla's speedtest program, or None. (The unofficial Python
    speedtest-cli installs a command of the same name; that is not it.)"""
    exe = shutil.which("speedtest")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    return exe if "Speedtest by Ookla" in out else None


def output_lines(proc, master):
    """Lines the program prints, as they come."""
    if master is None:
        for raw in proc.stdout:
            yield raw.decode("utf-8", "replace").strip()
        return
    pending = b""
    while True:
        try:
            chunk = os.read(master, 65536)
        except OSError:   # the program finished and closed its end
            break
        if not chunk:
            break
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield line.decode("utf-8", "replace").strip()


def ookla_test() -> dict:
    """One test with Ookla's Speedtest CLI. It prints one JSON line per
    progress update (types: testStart, ping, download, upload, result), with
    speeds in bytes per second. Where possible its output goes to a
    pseudo-terminal: into a pipe it holds output back in blocks, so progress
    arrives late."""
    cmd = [Config.ookla, "-f", "jsonl", "-p", "yes", "--progress-update-interval=300"]
    master = slave = None
    if pty:
        master, slave = pty.openpty()
    proc = subprocess.Popen(cmd, stdout=slave if pty else subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    if slave is not None:
        os.close(slave)
    stopper = threading.Timer(OOKLA_TIMEOUT, proc.kill)
    stopper.start()
    result, problem, down, other = None, None, None, []
    began, download_at, upload_at, flag = time.monotonic(), None, None, {}
    try:
        for line in output_lines(proc, master):
            try:
                msg = json.loads(line)
            except ValueError:
                if line:
                    other.append(line)
                continue
            kind = msg.get("type")
            if kind == "ping":
                set_state(state="ping", progress=msg["ping"].get("progress", 0.0), ping=round(msg["ping"]["latency"], 1))
            elif kind == "download":
                if download_at is None:
                    download_at = time.monotonic()
                    threading.Thread(target=watch_reachability, args=(flag,), daemon=True).start()
                down = msg["download"]["bandwidth"] * 8 / 1e6
                set_state(state="download", progress=msg["download"].get("progress", 0.0), live=round(down, 1))
            elif kind == "upload":
                upload_at = upload_at or time.monotonic()
                set_state(state="upload", progress=msg["upload"].get("progress", 0.0),
                          live=round(msg["upload"]["bandwidth"] * 8 / 1e6, 1), down=round(down, 1) if down else None)
            elif kind == "result":
                result = msg
            elif kind == "log" and msg.get("level") == "error":
                problem = msg.get("message")
    finally:
        stopper.cancel()
        if master is not None:
            os.close(master)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not result:
        said = " ".join(other + proc.stderr.read().decode("utf-8", "replace").splitlines())
        if "license" in said.lower():
            raise RuntimeError("Accept Ookla's license first: run speedtest once on the helper's computer.")
        raise RuntimeError(problem or plain(said, 80) or f"Speedtest stopped without a result (exit {proc.returncode})")
    row = {
        "down": round(result["download"]["bandwidth"] * 8 / 1e6, 1),
        "up": round(result["upload"]["bandwidth"] * 8 / 1e6, 1),
        "ping": round(result["ping"]["latency"], 1),
        "jitter": round(result["ping"]["jitter"], 1),
        "via": "ookla",
    }
    server = plain(result.get("server", {}).get("name"))
    if server:
        row["server"] = server
    if download_at and upload_at:
        row["ping_s"] = round(download_at - began, 1)
        row["dl_s"] = round(upload_at - download_at, 1)
    if flag.get("quiet"):
        row["quiet"] = True
    return row


# ---------------------------------------------------------------- running and remembering tests

def run_test() -> dict | None:
    """One full test. Updates the shared state as it goes and saves the result."""
    try:
        set_state(state="ping", progress=0.0, live=0.0, ping=None, down=None, up=None, error=None)
        row = ookla_test() if Config.engine == "ookla" else builtin_test()
        row = {"t": int(time.time()), **row}
        with Config.results.open("a") as f:
            f.write(json.dumps(row) + "\n")
        set_state(state="done", progress=1.0, live=0.0, down=row["down"], up=row["up"], ping=row["ping"])
        _finished["at"] = time.monotonic()
        log(f"Test: {row['down']} Mbps down, {row['up']} Mbps up, ping {row['ping']} ms" + (" (the helper was unreachable during the download)" if row.get("quiet") else ""))
        return row
    except Exception as e:
        set_state(state="error", error=plain(e, 80))
        _finished["at"] = time.monotonic()
        log(f"Test failed: {e}")
        return None


def load_results() -> list:
    rows = []
    try:
        for line in Config.results.read_text().splitlines():
            try:
                row = json.loads(line)
                if "down" in row and "up" in row and "t" in row:
                    rows.append(row)
            except ValueError:
                pass
    except FileNotFoundError:
        pass
    return rows


def expected() -> dict:
    """How long the ping and download parts took last time, and whether the
    helper could be reached during the download."""
    for row in reversed(load_results()):
        if "dl_s" in row:
            return {"ping": row.get("ping_s", 1.5), "download": row["dl_s"], "quiet": bool(row.get("quiet"))}
    return {"ping": 1.5, "download": 8.0, "quiet": False}


def start_test() -> dict:
    with _lock:
        public = {k: v for k, v in _state.items() if k != "started"}
        if _state["state"] in RUNNING:
            return public
        wait = Config.min_gap - (time.monotonic() - _finished["at"])
        if wait > 0:
            return {**public, "state": "error", "error": f"Wait {int(wait) + 1} s between tests."}
        _state.update(state="ping", id=_state["id"] + 1, progress=0.0, live=0.0, ping=None, down=None, up=None, error=None,
                      expect=expected(), started=time.time())
        threading.Thread(target=run_test, daemon=True).start()
        return {k: v for k, v in _state.items() if k != "started"}


def one(row: dict, now: float) -> dict:
    out = {k: row[k] for k in ("down", "up", "ping", "jitter", "server") if k in row}
    out["age"] = int(now - row["t"])
    return out


def window(rows: list, now: float, seconds: float) -> dict:
    rs = [r for r in rows if r["t"] >= now - seconds]
    if not rs:
        return {"n": 0}
    return {
        "n": len(rs),
        "down": round(statistics.mean(r["down"] for r in rs), 1),
        "up": round(statistics.mean(r["up"] for r in rs), 1),
        "ping": round(statistics.mean(r["ping"] for r in rs), 1),
        "min": min(r["down"] for r in rs),
        "max": max(r["down"] for r in rs),
    }


def stats() -> dict:
    rows, now = load_results(), time.time()
    out = {"count": len(rows)}
    if rows:
        month = [r for r in rows if r["t"] >= now - 30 * 86400] or rows
        out["last"] = one(rows[-1], now)
        out["h24"] = window(rows, now, 86400)
        out["d7"] = window(rows, now, 7 * 86400)
        out["d30"] = window(rows, now, 30 * 86400)
        out["best"] = one(max(month, key=lambda r: r["down"]), now)
        out["worst"] = one(min(month, key=lambda r: r["down"]), now)
    return out


# ---------------------------------------------------------------- web server for the app

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"   # one request per connection, which is all the bar's client needs

    def _answer(self, obj, code=200):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/status":
            self._answer(get_state())
        elif path == "/stats":
            self._answer(stats())
        elif path == "/run":
            self._answer(start_test())
        elif path == "":
            self._answer({"helper": "busybar-net-speed", "version": VERSION, "engine": Config.engine, "bar": Config.bar})
        else:
            self._answer({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.do_GET()

    def log_message(self, *args):   # the app asks twice a second during a test; don't print each call
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Runs internet speed tests for the Net Speed app on a BUSY Bar.")
    parser.add_argument("--bar", help="the bar's address, if it isn't found by itself (for example 192.168.1.42)")
    parser.add_argument("--password", help="the bar's HTTP API password, if you set one")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port the app reaches this helper on (default {DEFAULT_PORT})")
    parser.add_argument("--engine", choices=("builtin", "ookla"), default="builtin",
                        help="builtin: Cloudflare's public speed test (default). ookla: Ookla's Speedtest CLI, which you install and accept the license for yourself")
    parser.add_argument("--data", default=str(Path.home() / ".busybar-net-speed"), help="folder for the results file")
    parser.add_argument("--once", action="store_true", help="run one test, print the result and exit")
    parser.add_argument("--version", action="version", version=f"Net Speed helper {VERSION}")
    args = parser.parse_args()

    Config.bar_given, Config.password, Config.port, Config.engine = args.bar, args.password, args.port, args.engine
    data = Path(args.data).expanduser()
    data.mkdir(parents=True, exist_ok=True)
    Config.results = data / "results.jsonl"
    if Config.engine == "ookla":
        Config.ookla = find_ookla()
        if not Config.ookla:
            print("Ookla's Speedtest CLI was not found. Install it from https://www.speedtest.net/apps/cli, "
                  "run 'speedtest' once to accept its license, then start the helper again. "
                  "Or leave out --engine ookla to use the built-in test.", file=sys.stderr)
            return 2
        Config.min_gap = 10

    if args.once:
        row = run_test()
        print(json.dumps(row or get_state(), indent=1))
        return 0 if row else 1

    try:
        server = ThreadingHTTPServer(("0.0.0.0", Config.port), Handler)
    except OSError as e:
        print(f"Can't listen on port {Config.port} ({e}). Pick another with --port.", file=sys.stderr)
        return 2
    log(f"Net Speed helper {VERSION}. Tests run with {'Ookla Speedtest' if Config.engine == 'ookla' else 'the built-in test (Cloudflare)'}. Results in {Config.results}")
    threading.Thread(target=announce_loop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        log("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
