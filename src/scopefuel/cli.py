"""scopefuel CLI.

에이전트가 소비하는 계약은 `--json` (schema=scopefuel.v1) 과 `--exit-code-on` 이다.
사람이 보는 표/한 줄은 그 위의 표현일 뿐이다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

from . import render
from .cache import DEFAULT_TTL_S, collect
from .model import SCHEMA, ProviderResult, overall_mark
from .providers import default_order, registry

MARK_RANK = {"ok": 0, "warn": 1, "degraded": 2, "crit": 3}


def build_parser(available: list[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scopefuel",
        description="AI 코딩 플랜의 남은 여유를 스코프(계정/모델/그룹)와 지평(지금/이번주)으로 구분해 조회",
    )
    parser.add_argument(
        "--only",
        default=",".join(default_order(available)),
        help=f"조회할 provider (콤마 구분). 사용 가능: {', '.join(default_order(available))}",
    )
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="정규화 JSON (schema=scopefuel.v1)")
    out.add_argument("--raw", action="store_true", help="provider 원본 응답")
    out.add_argument("--brief", action="store_true", help="한 줄 요약 (pane/statusline용)")
    parser.add_argument(
        "--horizon", choices=["now", "week", "both"], default="both", help="--brief 에 표시할 지평"
    )
    parser.add_argument("--no-cache", action="store_true", help="캐시 무시하고 강제 조회")
    parser.add_argument("--cache-ttl", type=float, default=DEFAULT_TTL_S, help="캐시 TTL(초)")
    parser.add_argument("--no-color", action="store_true", help="ANSI 색 끄기")
    parser.add_argument(
        "--exit-code-on",
        choices=["never", "warn", "crit"],
        default="never",
        help="이 심각도 이상이면 종료코드 2 (Monitor/알림 연동용)",
    )
    parser.add_argument(
        "--watch", type=float, metavar="SECONDS", help="주기적으로 다시 그린다 (herdr pane용)"
    )
    parser.add_argument("--list-providers", action="store_true", help="사용 가능한 provider 목록")
    return parser


def _render(results: list[ProviderResult], args: argparse.Namespace, now: dt.datetime) -> str:
    color = not args.no_color and sys.stdout.isatty()
    if args.raw:
        return json.dumps({r.id: r.raw for r in results}, indent=2, ensure_ascii=False)
    if args.json:
        payload = {
            "schema": SCHEMA,
            "generated_at": now.isoformat(),
            "summary": {"mark": overall_mark(results)},
            "providers": [r.as_dict(now=now) for r in results],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    if args.brief:
        return render.brief(results, color=color, horizon=args.horizon, now=now)
    return render.table(results, color=color, now=now)


def main(argv: list[str] | None = None) -> int:
    fetchers = registry()
    args = build_parser(list(fetchers)).parse_args(argv)

    if args.list_providers:
        for name in default_order(list(fetchers)):
            print(name)
        return 0

    names = [n.strip() for n in args.only.split(",") if n.strip()]
    if unknown := [n for n in names if n not in fetchers]:
        print(f"error: 알 수 없는 provider {unknown} (--list-providers 로 확인)", file=sys.stderr)
        return 2

    while True:
        now = dt.datetime.now(dt.UTC)
        results = collect(fetchers, names, ttl_s=args.cache_ttl, use_cache=not args.no_cache)
        print(_render(results, args, now), flush=True)
        if not args.watch:
            break
        try:
            time.sleep(max(5.0, args.watch))
        except KeyboardInterrupt:
            return 0
        print("\033[2J\033[H", end="")  # pane 재렌더

    if args.exit_code_on != "never":
        threshold = MARK_RANK[args.exit_code_on]
        if MARK_RANK[overall_mark(results)] >= threshold:
            return 2
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
