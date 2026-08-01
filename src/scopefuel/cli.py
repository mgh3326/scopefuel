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

from . import bench, recommend, render
from .cache import DEFAULT_TTL_S, collect
from .model import SCHEMA, ProviderResult, overall_mark, overall_usage_mark
from .policy import clear_policy, list_policy_rows, set_policy
from .providers import default_order, registry
from .recommend import grade_help_text

MARK_RANK = {"ok": 0, "warn": 1, "degraded": 2, "crit": 3}


def _date_arg(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def _boost_arg(value: str) -> int | str:
    if value.strip().lower() == "none":
        return "none"
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"boost 는 정수 또는 'none' 이어야 합니다: {value!r}") from exc


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("0 이상의 정수여야 합니다") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("0 이상의 정수여야 합니다")
    return parsed


def _completed_arg(value: str) -> int:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return 1
    if normalized in {"0", "false", "no"}:
        return 0
    raise argparse.ArgumentTypeError("completed 는 0/1 이어야 합니다")


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
        choices=["S+", "S", "A+", "A", "B", "C"],
        metavar="GRADE",
        help="해당 급의 모델 사용 우선순위 추천. " + grade_help_text(),
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="--recommend 시 연속 점수 구성요소(capacity/waste/throughput·제약 창)를 함께 표시",
    )
    parser.add_argument(
        "--hide-excluded",
        action="store_true",
        help="--recommend 시 제외(정책/소진/측정불가) 접힌 줄을 숨김",
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
    parser.add_argument(
        "--list-recommend-profiles",
        action="store_true",
        help="GRADE_TABLE 의 모든 추천 프로필 이름(기계 판독 가능, 한 줄에 하나) — wrk 교차검증용",
    )

    subparsers = parser.add_subparsers(dest="command")
    policy_parser = subparsers.add_parser("policy", help="pool-level policy config")
    policy_sub = policy_parser.add_subparsers(dest="policy_command", required=True)

    policy_sub.add_parser("list", help="정책 목록 보기")

    set_parser = policy_sub.add_parser("set", help="pool 정책 설정")
    set_parser.add_argument("pool", choices=available, help="provider pool 이름")
    set_parser.add_argument(
        "pool_class",
        nargs="?",
        choices=["preserve", "spend", "exclude"],
        metavar="class",
        help="정책 클래스 (boost만 바꿀 때는 생략 가능)",
    )
    set_parser.add_argument(
        "--until", type=_date_arg, help="YYYY-MM-DD 형식 만료일 (class 또는 boost 설정 시 필수)"
    )
    set_parser.add_argument("--note", help="선택적 메모")
    set_parser.add_argument(
        "--boost",
        type=_boost_arg,
        metavar="N|none",
        help="정수 boost (작을수록 먼저). 'none' 이면 boost만 해제. 숫자 설정 시 --until 필수",
    )

    clear_parser = policy_sub.add_parser("clear", help="pool 정책 제거")
    clear_parser.add_argument("pool", help="provider pool 이름")

    bench_parser = subparsers.add_parser("bench", help="출처별 벤치 점수 SQLite DB")
    bench_sub = bench_parser.add_subparsers(dest="bench_command", required=True)
    bench_sub.add_parser("sync", help="공식 Artificial Analysis 모델 점수 동기화")
    bench_sub.add_parser(
        "migrate-effort", help="기존 AA-model 행의 model_id effort 접미사를 effort 컬럼으로 백필"
    )

    bench_show = bench_sub.add_parser("show", help="모델의 출처별 벤치 점수 보기")
    bench_show.add_argument("model_id", help="정규화 모델 식별자")

    bench_import = bench_sub.add_parser("import", help="수동 벤치 점수 TOML 적재")
    bench_import.add_argument("file", help="[[scores]] 또는 [[model_scores]] TOML 파일")

    bench_sub.add_parser("coverage", help="프로필별 출처(AA-agent/AA-model/openrouter) 커버리지")

    reps_parser = subparsers.add_parser("reps", help="실측 대표 실행 기록")
    reps_sub = reps_parser.add_subparsers(dest="reps_command", required=True)
    reps_add = reps_sub.add_parser("add", help="대표 실행 1건 기록")
    reps_add.add_argument("--profile", required=True, help="herdr-spawn 프로필명")
    reps_add.add_argument("--model", dest="model_id", required=True, help="실제 실행 모델")
    reps_add.add_argument("--task", dest="task_ref", required=True, help="Linear 이슈 또는 PR")
    reps_add.add_argument("--tier", required=True, choices=["T0", "T1", "T2", "T3"])
    reps_add.add_argument("--role", required=True, choices=["impl", "verify", "fix", "orch"])
    reps_add.add_argument("--rounds", required=True, type=_nonnegative_int)
    reps_add.add_argument("--blockers-found", required=True, type=_nonnegative_int)
    reps_add.add_argument("--completed", required=True, type=_completed_arg, help="0/1")
    reps_add.add_argument("--notes")

    reps_list = reps_sub.add_parser("list", help="대표 실행 기록 조회")
    reps_list.add_argument("--limit", type=_nonnegative_int, help="최대 행 수 (1 이상)")

    all_profiles = sorted({p.name for profiles in recommend.GRADE_TABLE.values() for p in profiles})
    gate_parser = subparsers.add_parser(
        "gate",
        help="profile 하나의 스폰 가능 여부 판정 (exit 0=가능/3=차단/4=측정불가)",
    )
    gate_parser.add_argument(
        "-m", "--profile", required=True, choices=all_profiles, help="herdr-spawn profile 이름"
    )
    gate_parser.add_argument("--no-cache", action="store_true", help="캐시 무시하고 강제 조회")
    gate_parser.add_argument("--cache-ttl", type=float, default=DEFAULT_TTL_S, help="캐시 TTL(초)")

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


def _policy_command(
    args: argparse.Namespace, fetchers: dict[str, object], parser: argparse.ArgumentParser
) -> int:
    today = dt.datetime.now(dt.UTC).date()
    known_classes = {name: getattr(fetcher, "pool_class", "preserve") for name, fetcher in fetchers.items()}

    if args.policy_command == "list":
        for row in list_policy_rows(known_classes, today=today):
            class_tag = "[설정]" if row.class_configured else "[기본]"
            boost_s = str(row.boost) if row.boost is not None else "-"
            weight_s = f"{row.capacity_weight:g}" if row.capacity_weight_configured else "-"
            status_s = f"  [{row.status}]" if row.status else ""
            print(
                f"{row.pool:<12} {row.effective_class:<9} {class_tag:<6} "
                f"boost={boost_s:<4} capacity_weight={weight_s:<6}{status_s}"
            )
        return 0

    if args.policy_command == "set":
        if args.pool_class is not None and args.until is None:
            parser.error("--until 은 class 를 지정할 때 필수입니다")
        boost_arg: int | None | str = "__unset__"
        if args.boost is not None:
            if args.boost == "none":
                boost_arg = None
            else:
                boost_arg = args.boost
                if args.until is None:
                    parser.error("--until 은 --boost 로 값을 지정할 때 필수입니다")
        if args.pool_class is None and boost_arg == "__unset__":
            parser.error("class 또는 --boost 중 하나는 지정해야 합니다")

        set_policy(
            args.pool,
            args.pool_class,
            until=args.until,
            note=args.note,
            boost=boost_arg,
        )
        parts = []
        if args.pool_class is not None:
            until_s = f" (until {args.until})" if args.until else ""
            parts.append(f"{args.pool_class}{until_s}")
        if boost_arg != "__unset__":
            parts.append("boost cleared" if boost_arg is None else f"boost={boost_arg}")
        print(f"{args.pool} -> {', '.join(parts)}")
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
    print(
        recommend.recommend(
            results,
            args.recommend,
            today=now.date(),
            now=now,
            bench_scores=bench.read_scores(),
            explain=bool(getattr(args, "explain", False)),
            hide_excluded=bool(getattr(args, "hide_excluded", False)),
        )
    )
    return 0


def _gate_command(args: argparse.Namespace, fetchers: dict[str, object]) -> int:
    now = dt.datetime.now(dt.UTC)
    results = collect(fetchers, list(fetchers), ttl_s=args.cache_ttl, use_cache=not args.no_cache)
    result = recommend.gate_check(results, args.profile, today=now.date(), now=now)

    if result.ok:
        print(
            f"profile={result.profile} pool={result.provider_id} "
            f"used_pct={result.used_pct} class={result.pool_class}"
        )
        print(result.reason)
        return 0

    print(result.reason, file=sys.stderr)
    if result.alternatives:
        print(f"대안({result.grade}): {', '.join(result.alternatives)}", file=sys.stderr)
    else:
        print(f"대안({result.grade}) 없음 — 동일 grade 정상 후보 전부 소진/측정불가", file=sys.stderr)
    return 4 if result.unmeasurable else 3


def _bench_command(args: argparse.Namespace) -> int:
    if args.bench_command == "sync":
        return bench.run_sync(stderr=sys.stderr)
    if args.bench_command == "migrate-effort":
        count = bench.migrate_aa_model_effort_suffixes()
        print(f"bench migrate-effort: migrated {count} row(s)")
        return 0
    if args.bench_command == "coverage":
        print(bench.coverage_report())
        return 0
    if args.bench_command == "show":
        print(bench.show_scores(args.model_id))
        return 0
    if args.bench_command == "import":
        try:
            count = bench.import_scores(args.file)
        except bench.BenchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"bench import: stored {count} score(s)")
        return 0
    return 2


def _reps_command(args: argparse.Namespace) -> int:
    if args.reps_command == "add":
        try:
            rep = bench.add_rep(
                profile=args.profile,
                model_id=args.model_id,
                task_ref=args.task_ref,
                tier=args.tier,
                role=args.role,
                rounds=args.rounds,
                blockers_found=args.blockers_found,
                completed=args.completed,
                notes=args.notes,
            )
        except bench.BenchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"recorded rep id={rep.id}")
        return 0
    if args.reps_command == "list":
        try:
            reps = bench.read_reps(limit=args.limit)
        except bench.BenchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for rep in reps:
            print(bench.format_rep(rep))
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    fetchers = registry()
    parser = build_parser(list(fetchers))
    args = parser.parse_args(argv)

    if args.command == "bench":
        return _bench_command(args)

    if args.command == "reps":
        return _reps_command(args)

    if args.command == "policy":
        return _policy_command(args, fetchers, parser)

    if args.command == "gate":
        return _gate_command(args, fetchers)

    if args.list_providers:
        for name in default_order(list(fetchers)):
            print(name)
        return 0

    if args.list_recommend_profiles:
        for name in sorted({p.name for profiles in recommend.GRADE_TABLE.values() for p in profiles}):
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
