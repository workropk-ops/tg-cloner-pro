#!/usr/bin/env python3
"""Supervise a child process and stop it after sustained inactivity.

Activity signals (any one resets the idle timer):
  • process CPU time advances
  • process I/O counters advance
  • host network RX/TX bytes advance beyond a small noise floor

The supervisor also exits when the child exits (any exit code).
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def log(message: str) -> None:
    print(f"[watchdog] {message}", flush=True)


@dataclass
class Sample:
    cpu_ticks: int
    io_bytes: int
    net_bytes: int
    monotonic: float


def read_cpu_ticks(pid: int) -> Optional[int]:
    try:
        # utime + stime (fields 14 and 15, 1-indexed) in clock ticks
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return int(fields[13]) + int(fields[14])
    except (FileNotFoundError, IndexError, ValueError, OSError):
        return None


def read_io_bytes(pid: int) -> int:
    path = Path(f"/proc/{pid}/io")
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return 0
    read_b = write_b = 0
    for line in text.splitlines():
        if line.startswith("read_bytes:"):
            read_b = int(line.split()[1])
        elif line.startswith("write_bytes:"):
            write_b = int(line.split()[1])
    return read_b + write_b


def read_net_bytes() -> int:
    """Sum RX+TX across non-loopback interfaces (network namespace)."""
    path = Path("/proc/net/dev")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return 0
    total = 0
    for line in lines[2:]:
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        name = iface.strip()
        if name == "lo":
            continue
        parts = rest.split()
        if len(parts) < 16:
            continue
        rx = int(parts[0])
        tx = int(parts[8])
        total += rx + tx
    return total


def sample_process(pid: int) -> Optional[Sample]:
    cpu = read_cpu_ticks(pid)
    if cpu is None:
        return None
    return Sample(
        cpu_ticks=cpu,
        io_bytes=read_io_bytes(pid),
        net_bytes=read_net_bytes(),
        monotonic=time.monotonic(),
    )


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_tree(proc: subprocess.Popen[bytes], grace_seconds: int = 15) -> int:
    if proc.poll() is not None:
        return int(proc.returncode)

    pid = proc.pid
    log(f"Sending SIGTERM to process group {pid}")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return int(proc.returncode)
        time.sleep(0.25)

    log(f"Sending SIGKILL to process group {pid}")
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        return int(proc.wait(timeout=10))
    except subprocess.TimeoutExpired:
        return 137


def run_supervised(
    command: list[str],
    cwd: Path,
    idle_seconds: int,
    poll_seconds: float,
    net_noise_bytes: int,
) -> int:
    log(f"Starting: {' '.join(command)}")
    log(f"Working directory: {cwd}")
    log(f"Idle timeout: {idle_seconds}s (poll every {poll_seconds:.0f}s)")

    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        start_new_session=True,  # own process group for clean teardown
        stdout=None,
        stderr=None,
    )
    pid = proc.pid
    log(f"Child PID: {pid}")

    last = sample_process(pid)
    if last is None:
        code = proc.wait()
        log(f"Child exited immediately with code {code}")
        return int(code)

    last_activity = time.monotonic()
    stop_reason = "child-exit"

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stop_reason
        name = signal.Signals(signum).name
        log(f"Received {name} — stopping child")
        stop_reason = f"signal:{name}"
        terminate_tree(proc)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while True:
            time.sleep(poll_seconds)
            code = proc.poll()
            if code is not None:
                log(f"Child exited with code {code}")
                return int(code)

            if not process_alive(pid):
                code = proc.wait()
                log(f"Child no longer alive; exit code {code}")
                return int(code)

            now_sample = sample_process(pid)
            if now_sample is None:
                code = proc.wait()
                log(f"Lost process stats; exit code {code}")
                return int(code)

            cpu_delta = now_sample.cpu_ticks - last.cpu_ticks
            io_delta = now_sample.io_bytes - last.io_bytes
            net_delta = now_sample.net_bytes - last.net_bytes

            active = False
            reasons: list[str] = []
            if cpu_delta > 0:
                active = True
                reasons.append(f"cpu+{cpu_delta}ticks")
            if io_delta > 0:
                active = True
                reasons.append(f"io+{io_delta}B")
            if net_delta > net_noise_bytes:
                active = True
                reasons.append(f"net+{net_delta}B")

            if active:
                last_activity = now_sample.monotonic
                log(f"Activity: {', '.join(reasons)}")
            else:
                idle_for = now_sample.monotonic - last_activity
                remaining = max(0, idle_seconds - idle_for)
                log(
                    f"Idle for {idle_for:.0f}s "
                    f"(stop in {remaining:.0f}s if quiet continues)"
                )
                if idle_for >= idle_seconds:
                    stop_reason = "idle-timeout"
                    log(
                        f"No activity for {idle_seconds}s — "
                        "stopping TG Clone Pro"
                    )
                    code = terminate_tree(proc)
                    log(f"Stopped ({stop_reason}); child exit code {code}")
                    # Idle stop is intentional — treat as success for the job.
                    return 0

            last = now_sample
    except Exception:
        terminate_tree(proc)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a command and stop it after sustained idle time",
    )
    parser.add_argument(
        "--cwd",
        required=True,
        help="Working directory for the supervised command",
    )
    parser.add_argument(
        "--idle-seconds",
        type=int,
        default=int(os.environ.get("IDLE_TIMEOUT_SECONDS", "900")),
        help="Seconds of inactivity before stop (default: 900 = 15 min)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("IDLE_POLL_SECONDS", "30")),
        help="How often to sample activity (default: 30)",
    )
    parser.add_argument(
        "--net-noise-bytes",
        type=int,
        default=int(os.environ.get("IDLE_NET_NOISE_BYTES", "4096")),
        help="Network delta below this is treated as noise (default: 4096)",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run after -- separator",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        log("ERROR: no command provided (use: idle_watchdog.py --cwd DIR -- ./tg-cl bot)")
        return 2
    if args.idle_seconds < 60:
        log("ERROR: --idle-seconds must be >= 60")
        return 2

    cwd = Path(args.cwd).resolve()
    if not cwd.is_dir():
        log(f"ERROR: working directory does not exist: {cwd}")
        return 2

    return run_supervised(
        command=command,
        cwd=cwd,
        idle_seconds=args.idle_seconds,
        poll_seconds=max(5.0, args.poll_seconds),
        net_noise_bytes=max(0, args.net_noise_bytes),
    )


if __name__ == "__main__":
    sys.exit(main())
