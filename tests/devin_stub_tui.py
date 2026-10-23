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
capture_path = os.environ.get("STUB_CAPTURE", "")
usage_answer = os.environ.get("STUB_USAGE", "rows")  # rows | file | silent
usage_file = os.environ.get("STUB_USAGE_FILE", "")
modal_after = float(os.environ.get("STUB_MODAL_AFTER", "0.2"))
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
# 실측 캡처(v3000.11.3)의 입력창 composer placeholder 줄 그대로다.
COMPOSER = "❭ Ask Devin to build features, fix bugs, or work on your code\r\n"
USAGE = (
    " Daily   ■■■■■■■■■■■■■■■■■■■■  0% used  · resets in 2h 7m\r\n"
    " Weekly  ■■■■■■■■■■■■■■■■■■■■  18% used  · resets Oct 11, 5:00 PM (UTC+9)\r\n"
)
UPDATE_MODAL = (
    "\r\n? A new version of Devin is available. Update now?\r\n"
    "❯ Yes, update and restart\r\n  No, remind me later\r\n"
)
QUESTION_PROMPT = "\r\n? Devin wants to continue the previous session. Continue? (Y/n) "
# ``?`` 질문줄 없이 chevron 옵션 목록만 — _POST_INPUT_MODAL 의 chevron 브랜치 전용.
CHEVRON_OPTIONS = "\r\n❯ Yes, update and restart\r\n  No, remind me later\r\n"
# 입력이 시작된 '뒤'에 모달이 그려지는 모드들 — cap_replay 와 같은 바이트
# 재생을 하되 모달 트리거가 다르다(STUB_CAPTURE 필수).
_POST_INPUT_MODES = (
    "cr_modal_usage",
    "cr_q_usage",
    "cr_modal_exit",
    "modal_usage_wait",
    "modal_usage_silent",
)

if mode in ("focus_adv", "require_focus", "usage_only_after_focus"):
    emit("\x1b[?2004h\x1b[?1004h")
if mode == "hang":
    time.sleep(120)
    sys.exit(0)
if mode == "prompt_only":
    # 입력창 composer placeholder 만 그리고 상태줄(쿼타 배너)은 안 그리는 TUI.
    emit(COMPOSER)
if mode == "login_prompt":
    # 로그인 프롬프트 흉내 — chevron 으로 시작하는 옵션 목록은 composer 가
    # 아니다: bare ❯ 행만 보고 입력을 여는 구현은 여기에 /usage 를 쳐 넣는다.
    emit("Welcome back — sign in to continue\r\n")
    emit("❯ Sign in with your org account\r\n")
    emit("❯ Use an API key\r\n")
if mode == "login_arrow_list":
    emit("Select a login method:\r\n❯ Browser login\r\n  API key\r\n  Exit\r\n")
if mode == "status_only":
    # 상태줄만 그리고 composer placeholder 는 안 그리는 TUI — 상태줄만 보고
    # 입력을 여는 구현은 여기에 /usage 를 쳐 넣는다.
    emit(BANNER)
if mode == "update_over_banner":
    # 상태줄 + composer 가 보이는 화면 위에 업데이트 모달이 열린 경우 —
    # 모달을 검사하지 않는 구현은 'Yes, update' 에 /usage·Enter 를 쳐 넣는다.
    emit(BANNER + COMPOSER)
    emit("? Update now?\r\n❯ Yes, update and restart\r\n  No, remind me later\r\n")
if mode == "question_over_banner":
    # 상태줄+composer 뒤에 ? 질문줄'만' 있는 화면 — chevron 이 없어도
    # ? 브랜치 하나로 입력 게이트가 닫혀야 한다(tester S1 핀).
    emit(BANNER + COMPOSER)
    emit(QUESTION_PROMPT)
if mode == "chevron_over_banner":
    # 상태줄+composer 뒤에 chevron 옵션 목록'만' 있는 화면 — ? 가 없어도
    # chevron 브랜치 하나로 입력 게이트가 닫혀야 한다(tester S1 핀).
    emit(BANNER + COMPOSER)
    emit("\r\n❯ Yes, update and restart\r\n  No, remind me later\r\n")
if mode in ("cap_replay", *_POST_INPUT_MODES):
    # 실측 캡처 바이트 재생(v3000.11.3, tests/fixtures/devin_captureN.txt) —
    # 합성 화면이 아니라 저장된 PTY 출력 그대로다. 첫 커서주소 diff
    # (ESC[?2026h CR ESC[9A …) 앞까지를 첫 페인트로 즉시 그리고,
    # STUB_STATUS_DELAY 초 뒤에 그 diff 부터 캡처 끝까지 흘린다. 세 캡처 모두
    # 상태줄 뒤에 composer placeholder 리페인트가 없다 — 여기서도 없다.
    with open(capture_path, "rb") as fh:
        cap = fh.read()
    cut = cap.index(b"\x1b[?2026h\r\x1b[9A")
    emit(cap[:cut])
    time.sleep(float(os.environ.get("STUB_STATUS_DELAY", "1.5")))
    log("paint-status")
    emit(cap[cut:])
if mode == "modal_after_gate":
    # 상태줄+composer 로 입력 게이트가 열린 뒤 STUB_MODAL_AFTER 초 후 업데이트
    # 모달이 덮는 경우 — 게이트가 닫힐 때 ready_at 을 되돌리지 않는 구현은
    # 모달 위에 /usage·Enter 를 쳐 넣는다.
    emit(BANNER + COMPOSER)
    time.sleep(modal_after)
    log("paint-modal")
    emit("? Update now?\r\n❯ Yes, update and restart\r\n  No, remind me later\r\n")
if mode not in (
    "no_banner",
    "prompt_only",
    "login_prompt",
    "login_arrow_list",
    "status_only",
    "update_over_banner",
    "cap_replay",
    "modal_after_gate",
    "question_over_banner",
    "chevron_over_banner",
    *_POST_INPUT_MODES,
):
    emit(BANNER + COMPOSER)
if mode == "crash_after_banner":
    time.sleep(0.8)
    sys.exit(3)

state = "wait_usage"
typed = b""
deadline = time.monotonic() + 60
focus_seen = False
modal = None  # None | "update" | "question" — 떠 있는 동안 입력은 모달이 삼킨다
sched = []  # (t0 기준 경과초, 모달 종류) — 입력 '뒤'에 그려질 모달 예약


def show_modal(kind):
    global modal
    emit({"update": UPDATE_MODAL, "question": QUESTION_PROMPT, "chevron": CHEVRON_OPTIONS}[kind])
    modal = kind
    log("paint-modal-" + kind)


while time.monotonic() < deadline:
    now = time.monotonic() - t0
    while sched and sched[0][0] <= now:
        _, scheduled = sched.pop(0)
        show_modal(scheduled)
    r, _, _ = select.select([0], [], [], 0.1)
    if not r:
        continue
    chunk = os.read(0, 4096)
    if not chunk:
        break
    # 모달이 떠 있는 동안의 printable 입력은 KEYS-INTO-MODAL 로, Enter 는
    # MODAL-CONFIRMED <하이라이트 옵션> 으로 기록한다(ink select / y-n 프롬프트).
    log("KEYS-INTO-MODAL" if modal else "read", chunk)
    if b"\x1b[I" in chunk:
        focus_seen = True
    data = chunk.replace(b"\x1b[I", b"")
    if not data:
        continue
    if data == b"\r":
        if modal is not None:
            option = {
                "update": "Yes, update and restart",
                "question": "Y (default)",
                "chevron": "Yes, update and restart",
            }[modal]
            log("MODAL-CONFIRMED", option.encode())
            if modal in ("update", "chevron"):
                emit("\r\nUpdating Devin and restarting…\r\n")
                log("restart-exit")
                sys.exit(0)
            modal = None
            typed = b""
            continue
        cmd, typed = typed, b""
        if state == "wait_usage" and cmd == b"/usage":
            if mode in ("require_focus",) and not focus_seen:
                log("ignored-no-focus")
                continue
            if mode in ("no_usage_answer", "modal_usage_silent") or usage_answer == "silent":
                log("usage-no-answer")
            else:
                if mode == "slow_usage":
                    time.sleep(4.0)
                if mode == "malformed":
                    # /usage 응답 자체가 범위 밖 퍼센트를 싣는 경우.
                    emit(" Daily 230% used\r\n Weekly 999% used\r\n")
                elif usage_answer == "file":
                    # 실측 /usage 화면 픽스처를 바이트 그대로 응답한다.
                    with open(usage_file, "rb") as fh:
                        emit(fh.read())
                else:
                    emit(USAGE)
            if mode in ("modal_usage_wait", "modal_usage_silent"):
                # /usage 처리(응답 또는 침묵) 직후 모달을 예약한다 — /exit 를
                # 치기 전에 화면이 모달로 바뀌는 경우다.
                sched.append((time.monotonic() - t0 + modal_after, "update"))
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
    elif modal is not None:
        continue  # printable 키는 모달이 삼킨다 — composer 에 쌓이지 않는다
    elif data.endswith(b"\r") and len(data) > 1:
        # text and Enter in ONE read: the slash palette swallows it (observed live).
        log("swallowed-merged-write", data)
        typed = b""
    else:
        typed += data
        if mode == "echo_composer":
            # 입력 에코를 chevron composer 줄로 되그린다 — 실측 캡처의 에코는 맨 /usage 줄이라
            # 이 모드가 '❭ /usage' 에코 줄 제외(_POST_INPUT_MODAL 의 lookahead)를 핀한다.
            emit("\r\n❭ " + data.decode("utf-8", errors="replace") + "\r\n")
        # 명령 텍스트가 다 치해지는 순간(Enter 전) 모달을 그리는 트리거들.
        if mode == "cr_modal_usage" and state == "wait_usage" and typed == b"/usage":
            show_modal("update")
        elif mode == "cr_q_usage" and state == "wait_usage" and typed == b"/usage":
            show_modal("question")
        elif mode == "cr_modal_exit" and typed == b"/exit":
            show_modal("update")
        elif mode == "chevron_after_input" and typed in (b"/usage", b"/exit"):
            show_modal("chevron")
log("deadline")
sys.exit(0)
