# -*- coding: utf-8 -*-
"""Termux/Android 用的精简 psutil，仅覆盖 viewer.py 用到的接口。"""
from __future__ import annotations

import os
import signal
import time

STATUS_ZOMBIE = "zombie"
STATUS_RUNNING = "running"
STATUS_SLEEPING = "sleeping"


class Error(Exception):
    pass


class NoSuchProcess(Error):
    pass


class AccessDenied(Error):
    pass


def pid_exists(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.path.exists(f"/proc/{pid}"):
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_stat(pid):
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError as exc:
        raise NoSuchProcess(pid) from exc
    rp = raw.rfind(")")
    if rp < 0:
        raise NoSuchProcess(pid)
    fields = raw[rp + 2 :].split()
    if len(fields) < 20:
        raise NoSuchProcess(pid)
    return fields


_btime = None
_clk_tck = None


def _boot_time():
    global _btime, _clk_tck
    if _btime is None:
        btime = None
        try:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("btime "):
                        btime = float(line.split()[1])
                        break
        except OSError:
            btime = time.time() - time.monotonic()
        _btime = 0.0 if btime is None else btime
        try:
            _clk_tck = float(os.sysconf("SC_CLK_TCK"))
        except (ValueError, OSError, AttributeError):
            _clk_tck = 100.0
    return _btime, _clk_tck


class Process:
    def __init__(self, pid):
        self.pid = int(pid)
        if not pid_exists(self.pid):
            raise NoSuchProcess(self.pid)
        self._ctime = None

    def is_running(self):
        return pid_exists(self.pid)

    def status(self):
        try:
            state = _read_stat(self.pid)[0]
        except Error:
            raise NoSuchProcess(self.pid)
        return {
            "Z": STATUS_ZOMBIE,
            "R": STATUS_RUNNING,
            "S": STATUS_SLEEPING,
            "D": "disk-sleep",
            "T": "stopped",
        }.get(state, state)

    def create_time(self):
        if self._ctime is None:
            start_ticks = float(_read_stat(self.pid)[19])
            btime, clk = _boot_time()
            self._ctime = btime + start_ticks / clk
        return self._ctime

    def children(self, recursive=False):
        kids_by_parent = {}
        try:
            names = os.listdir("/proc")
        except OSError:
            return []
        for name in names:
            if not name.isdigit():
                continue
            cpid = int(name)
            try:
                ppid = int(_read_stat(cpid)[1])
            except (Error, OSError, ValueError, IndexError):
                continue
            kids_by_parent.setdefault(ppid, []).append(cpid)
        result, stack, seen = [], list(kids_by_parent.get(self.pid, [])), set()
        while stack:
            cpid = stack.pop()
            if cpid in seen:
                continue
            seen.add(cpid)
            try:
                result.append(Process(cpid))
            except Error:
                continue
            if recursive:
                stack.extend(kids_by_parent.get(cpid, []))
        return result

    def terminate(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError as exc:
            raise NoSuchProcess(self.pid) from exc
        except PermissionError as exc:
            raise AccessDenied(self.pid) from exc

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError as exc:
            raise NoSuchProcess(self.pid) from exc
        except PermissionError as exc:
            raise AccessDenied(self.pid) from exc


def wait_procs(procs, timeout=None):
    deadline = None if timeout is None else time.monotonic() + timeout
    gone, alive = [], list(procs)
    while alive:
        still = []
        for proc in alive:
            try:
                running = proc.is_running() and proc.status() != STATUS_ZOMBIE
            except Error:
                running = False
            (still if running else gone).append(proc)
        alive = still
        if not alive:
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return gone, alive
