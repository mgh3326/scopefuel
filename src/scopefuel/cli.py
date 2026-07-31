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

from . import recommend, render
from .cache import DEFAULT_TTL_S, collect
from .model import SCHEMA, ProviderResult, overall_mark, overall_usage_mark
from .policy import clear_policy, list_policies, set_policy
from .providers import default_order, registry

MARK_RANK = {"ok": 0, "warn": 1, "degraded": 2, "crit": 3}


def _date_arg(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


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
        "--recommend",
        choices=["S", "A", "B", "C"],
        metavar="GRADE",
        help="해당 급의 모델 사용 우선순위 추천",
    )
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

    subparsers = parser.add_subparsers(dest="command")
    policy_parser = subparsers.add_parser("policy", help="pool-level policy config")
    policy_sub = policy_parser.add_subparsers(dest="policy_command", required=True)

    policy_sub.add_parser("list", help="정책 목록 보기")

    set_parser = policy_sub.add_parser("set", help="pool 정책 설정")
    set_parser.add_argument("pool", choices=available, help="provider pool 이름")
    set_parser.add_argument("pool_class", choices=["preserve", "spend"], metavar="class", help="정책 클래스")
    set_parser.add_argument("--until", type=_date_arg, required=True, help="YYYY-MM-DD 형식 만료일")
    set_parser.add_argument("--note", help="선택적 메모")

    clear_parser = policy_sub.add_parser("clear", help="pool 정책 제거")
    clear_parser.add_argument("pool", choices=available, help="provider pool 이름")

    return parser


def _render(results: list[ProviderResult], args: argparse.Namespace, now: dt.datetime) -> str:
    color = not args.no_color and sys.stdout.isatty()
    if args.raw:
        return json.dumps({r.id: r.raw for r in results}, indent=2, ensure_ascii=False)
    if args.json:
        payload = {
            "schema": SCHEMA,
            "generated_at": now.isoformat(),
            "summary": {
                "mark": overall_mark(results, now=now),
                "usage_mark": overall_usage_mark(results, now=now),
            },
            "providers": [r.as_dict(now=now) for r in results],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    if args.brief:
        return render.brief(results, color=color, horizon=args.horizon, now=now)
    return render.table(results, color=color, now=now)


def _policy_command(args: argparse.Namespace, fetchers: dict[str, object]) -> int:
    today = dt.datetime.now(dt.UTC).date()
    known_classes = {name: getattr(fetcher, "pool_class", "preserve") for name, fetcher in fetchers.items()}

    if args.policy_command == "list":
        for name, effective, status in list_policies(known_classes, today=today):
            status_s = f"  [{status}]" if status else ""
            print(f"{name:<12} {effective:<9}{status_s}")
        return 0

    if args.policy_command == "set":
        set_policy(args.pool, args.pool_class, until=args.until, note=args.note)
        note_s = f" (until {args.until})" if args.until else ""
        print(f"{args.pool} -> {args.pool_class}{note_s}")
        return 0

    if args.policy_command == "clear":
        if clear_policy(args.pool):
            print(f"{args.pool} policy cleared")
            return 0
        print(f"error: {args.pool} 에 설정된 정책이 없습니다", file=sys.stderr)
        return 2

    return 2


def _recommend_command(args: argparse.Namespace, fetchers: dict[str, object]) -> int:
    now = dt.datetime.now(dt.UTC)
    results = collect(fetchers, list(fetchers), ttl_s=args.cache_ttl, use_cache=not args.no_cache)
    print(recommend.recommend(results, args.recommend, today=now.date()))
    return 0


def main(argv: list[str] | None = None) -> int:
    fetchers = registry()
    args = build_parser(list(fetchers)).parse_args(argv)

    if args.command == "policy":
        return _policy_command(args, fetchers)

    if args.list_providers:
        for name in default_order(list(fetchers)):
            print(name)
        return 0

    if args.recommend:
        return _recommend_command(args, fetchers)

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
        if MARK_RANK[overall_mark(results, now=now)] >= threshold:
            return 2
        if MARK_RANK[overall_usage_mark(results, now=now)] >= threshold:
            return 2
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
