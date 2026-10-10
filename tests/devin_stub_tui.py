#!/usr/bin/env python3
"""Stub devin TUI for probe-mechanics tests (NOT the real devin).

Behaviour is chosen with STUB_MODE; all raw stdin bytes are logged with times to STUB_LOG.
"""

import os
import select
import subprocess
import sys
import time
import tty

mode = os.environ.get("STUB_MODE", "normal")
log_path = os.environ.get("STUB_LOG", "/dev/null")
models = os.environ.get("STUB_MODELS", "")
t0 = time.monotonic()


def log(kind, data=b""):
    with open(log_path, "a") as fh:
        fh.write(f"{time.monotonic() - t0:7.2f} {kind} {data!r}\n")


if sys.argv[1:3] == ["models", "list"]:
    if models:
        with open(models) as fh:
            sys.stdout.write(fh.read())
    sys.exit(0)

log("start", repr(sys.argv[1:]).encode())
with open(log_path, "a") as fh:
    fh.write(f"pid {os.getpid()}\n")

try:
    tty.setraw(0)
except Exception as exc:  # pragma: no cover
    log("setraw-failed", str(exc).encode())

out = sys.stdout.buffer


def emit(text):
    out.write(text.encode() if isinstance(text, str) else text)
    out.flush()


BANNER = "v3000.11.3\r\nPro · 82% remaining (resets in 1d 2h)\r\n"
USAGE = (
    " Daily   ■■■■■■■■■■■■■■■■■■■■  0% used  · resets in 2h 7m\r\n"
    " Weekly  ■■■■■■■■■■■■■■■■■■■■  18% used  · resets Oct 11, 5:00 PM (UTC+9)\r\n"
)

if mode in ("focus_adv", "require_focus", "usage_only_after_focus"):
    emit("\x1b[?2004h\x1b[?1004h")
if mode == "hang":
    time.sleep(120)
    sys.exit(0)
if mode == "prompt_only":
    # 입력창 chevron 만 그리고 상태줄(쿼타 배너)은 안 그리는 TUI —
    # ❯ 마커만 봐도 /usage 를 보낼 수 있는지 확인하는 모드.
    emit("❯ Ask Devin to build anything...\r\n")
if mode not in ("no_banner", "prompt_only"):
    emit(BANNER)
if mode == "crash_after_banner":
    time.sleep(0.8)
    sys.exit(3)

state = "wait_usage"
typed = b""
deadline = time.monotonic() + 60
focus_seen = False
while time.monotonic() < deadline:
    r, _, _ = select.select([0], [], [], 0.1)
    if not r:
        continue
    chunk = os.read(0, 4096)
    if not chunk:
        break
    log("read", chunk)
    if b"\x1b[I" in chunk:
        focus_seen = True
    data = chunk.replace(b"\x1b[I", b"")
    if not data:
        continue
    if data == b"\r":
        cmd, typed = typed, b""
        if state == "wait_usage" and cmd == b"/usage":
            if mode in ("require_focus",) and not focus_seen:
                log("ignored-no-focus")
                continue
            if mode == "no_usage_answer":
                log("usage-no-answer")
            else:
                if mode == "slow_usage":
                    time.sleep(4.0)
                if mode == "malformed":
                    # /usage 응답 자체가 범위 밖 퍼센트를 싣는 경우.
                    emit(" Daily 230% used\r\n Weekly 999% used\r\n")
                else:
                    emit(USAGE)
            state = "wait_exit"
        elif state == "wait_exit" and cmd == b"/exit":
            log("exit-accepted")
            if mode == "ignore_exit":
                child = subprocess.Popen(
                    ["/bin/sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL
                )
                with open(log_path, "a") as fh:
                    fh.write(f"grandchild {child.pid}\n")
                time.sleep(120)
            sys.exit(0)
        else:
            log("unexpected-command", cmd)
            sys.exit(9)
    elif data.endswith(b"\r") and len(data) > 1:
        # text and Enter in ONE read: the slash palette swallows it (observed live).
        log("swallowed-merged-write", data)
        typed = b""
    else:
        typed += data
log("deadline")
sys.exit(0)
