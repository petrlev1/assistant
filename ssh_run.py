#!/usr/bin/env python3
"""Run a command on the RAG server (no passwords stored anywhere).

Usage:
    python ssh_run.py '<command>' [timeout_seconds]

- With a shell on the server it detects that and runs the command locally.
- From Windows/another machine it connects over SSH using the alias
  `mitino-pub` from ~/.ssh/config (host 94.29.35.61, port 2222, key auth).
  Override the target with the RAG_SSH_TARGET env var:
      RAG_SSH_TARGET='mitino-lan' python ssh_run.py 'hostname'
      RAG_SSH_TARGET='mitino12@94.29.35.61:2222' python ssh_run.py 'hostname'
"""
import os
import subprocess
import sys

# ~/.ssh/config alias: keeps host/port/key in one place (the address changed
# once already: SSH moved from :443 behind sslh to :2222 behind the router).
SSH_TARGET = os.environ.get('RAG_SSH_TARGET', 'mitino-pub')
# Working copy of RAGSTONE on the server (also used to detect "we are on the server").
REMOTE_DIR = os.environ.get('RAG_REMOTE_DIR', '~/assistant/assistant')
# On the server this absolute path exists; on Windows it never does.
ON_SERVER = os.path.exists(os.path.expanduser(REMOTE_DIR))


def _communicate(p: subprocess.Popen, timeout: int) -> tuple[str, str, int]:
    try:
        stdout, stderr = p.communicate(timeout=timeout)
        return stdout, stderr, p.returncode
    except subprocess.TimeoutExpired:
        p.kill()
        stdout, stderr = p.communicate()
        return stdout, stderr + "\n[timeout]", 124


def run_local(cmd: str, timeout: int) -> tuple[str, str, int]:
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    return _communicate(p, timeout)


def _split_target(target: str) -> tuple[str, str | None]:
    """'user@host:2222' → ('user@host', '2222'); 'mitino-pub' → ('mitino-pub', None)."""
    if ':' in target:
        host, _, port = target.rpartition(':')
        if port.isdigit():
            return host, port
    return target, None


def run_ssh(cmd: str, timeout: int) -> tuple[str, str, int]:
    host, port = _split_target(SSH_TARGET)
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if port:
        argv += ["-p", port]
    argv += [host, cmd]
    kwargs = {}
    # CREATE_NO_WINDOW существует только на Windows; на POSIX флаг не нужен
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if no_window:
        kwargs["creationflags"] = no_window
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, **kwargs)
    return _communicate(p, timeout)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    if ON_SERVER:
        out, err, rc = run_local(cmd, timeout)
    else:
        print(f"[ssh_run] цель: {SSH_TARGET} → {REMOTE_DIR}", file=sys.stderr)
        out, err, rc = run_ssh(cmd, timeout)
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err:
        print("STDERR:", err, file=sys.stderr)
    print(f"RC={rc}", file=sys.stderr)
    sys.exit(0 if rc == 0 else 1)
