#!/usr/bin/env python3
"""Run a command on the RAG server (no passwords stored anywhere).

Usage: python ssh_run.py '<command>' [timeout_seconds]

- Run from Windows: connects via SSH key (port 443, key auth only, no password).
- Run ON the server: detects it is on the server and executes the command locally.
"""
import os
import subprocess
import sys

HOST = "petrlev@85.234.31.16"
PORT = "443"
# On the server this absolute path exists; on Windows it never does.
ON_SERVER = os.path.exists("/home/petrlev/peter/ai_bot/automation/assistant")


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


def run_ssh(cmd: str, timeout: int) -> tuple[str, str, int]:
    p = subprocess.Popen(
        ["ssh", "-p", PORT,
         "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=NUL",
         HOST, cmd],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return _communicate(p, timeout)


if __name__ == "__main__":
    cmd = sys.argv[1]
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    if ON_SERVER:
        out, err, rc = run_local(cmd, timeout)
    else:
        out, err, rc = run_ssh(cmd, timeout)
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err:
        print("STDERR:", err, file=sys.stderr)
    print(f"RC={rc}", file=sys.stderr)
    sys.exit(0 if rc == 0 else 1)
