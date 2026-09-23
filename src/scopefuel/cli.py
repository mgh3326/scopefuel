"""scopefuel CLI.

에이전트가 소비하는 계약은 `--json` (schema=scopefuel.v1) 과 `--exit-code-on` 이다.
사람이 보는 표/한 줄은 그 위의 표현일 뿐이다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
from dataclasses import replace

from . import bench, herdr, launch, manual, recommend, render, served
from .cache import collect
from .model import SCHEMA, ProviderResult, overall_mark, overall_usage_mark
from .policy import clear_policy, list_policy_rows, set_policy
from .providers import default_order, registry
from .recommend import grade_help_text
from .refresh import REFRESH_POOLS, run_worker, spawn

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


def _manual_used_arg(value: str) -> float:
    try:
        return manual.parse_used_pct(value)
    except manual.ManualError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


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
        help=(
            "--recommend 시 연속 점수 구성요소(capacity/waste/throughput·제약 창) 및 "
            "추정(내삽/외삽) 근거를 함께 표시"
        ),
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
    parser.add_argument(
        "--cache-ttl", type=float, default=None, help="캐시 TTL(초; 지정 시 전 provider 공통)"
    )
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

    launch_parser = policy_sub.add_parser(
        "launch",
        help="프로필의 정본 model_id·기본 effort·pool·gate (wrk 등 런처가 소비)",
    )
    launch_parser.add_argument("profile", help="카탈로그 프로필 이름")
    launch_parser.add_argument(
        "--effort",
        help="effort 단계를 명시 고정 (생략 시 카탈로그·런처 기본값)",
    )
    launch_parser.add_argument("--json", action="store_true", help="JSON 한 줄로 출력")
    launch_parser.add_argument(
        "--operator-request",
        action="store_true",
        help="운영자 명시 요청 — consult_only 및 stale 상태의 비-default gate 에 필요",
    )

    manual_parser = subparsers.add_parser("manual", help="로컬 수동 쿼타 관측 관리")
    manual_sub = manual_parser.add_subparsers(dest="manual_command", required=True)

    manual_set = manual_sub.add_parser("set", help="수동 관측 추가")
    manual_set.add_argument("--pool", required=True, choices=available, help="provider pool 이름")
    manual_set.add_argument("--used", required=True, type=_manual_used_arg, help="사용률 0..100")
    manual_set.add_argument(
        "--window",
        choices=sorted(manual.WINDOW_ALIASES),
        help="한도 창. 생략 가능한 단일-window provider는 정책에서 추론",
    )
    reset_group = manual_set.add_mutually_exclusive_group()
    reset_group.add_argument("--resets-in", metavar="DURATION", help="관측 시점부터 reset까지 기간")
    reset_group.add_argument("--resets-at", metavar="TIMESTAMP", help="reset 절대 시각")
    manual_set.add_argument(
        "--measured-at", required=True, metavar="TIMESTAMP|now", help="실제로 관측한 시각"
    )
    manual_set.add_argument("--reason", required=True, help="측정 장애 또는 정정 사유")
    manual_set.add_argument("--ttl", default="15m", metavar="DURATION", help="효력 기간, 최대 2h")

    manual_list = manual_sub.add_parser("list", help="append-only 수동 관측 이력 보기")
    manual_list.add_argument("--pool", choices=available, help="provider pool 필터")

    manual_clear = manual_sub.add_parser("clear", help="pool의 현재 수동 관측 무효화")
    manual_clear.add_argument("--pool", required=True, choices=available, help="provider pool 이름")

    bench_parser = subparsers.add_parser("bench", help="출처별 벤치 점수 SQLite DB")
    bench_sub = bench_parser.add_subparsers(dest="bench_command", required=True)
    bench_sub.add_parser("sync", help="공식 Artificial Analysis 모델 점수 동기화")
    bench_sub.add_parser(
        "migrate-effort", help="기존 AA-model 행의 model_id effort 접미사를 effort 컬럼으로 백필"
    )
    bench_sub.add_parser("backfill-aa-metrics", help="승인된 AA-agent 26행의 실행시간·비용 메타데이터 백필")

    bench_show = bench_sub.add_parser("show", help="모델의 출처별 벤치 점수 보기")
    bench_show.add_argument("model_id", help="정규화 모델 식별자")

    bench_import = bench_sub.add_parser("import", help="수동 벤치 점수 TOML 적재")
    bench_import.add_argument("file", help="[[scores]] 또는 [[model_scores]] TOML 파일")

    bench_sub.add_parser("coverage", help="프로필별 출처(AA-agent/AA-model/openrouter) 커버리지")
    bench_sub.add_parser("push-local", help="기존 로컬 bench 행을 handoffkeep으로 1회 이관")

    grades_parser = bench_sub.add_parser("grades", help="서버 정본 급 배치 관리")
    grades_sub = grades_parser.add_subparsers(dest="grades_command", required=True)
    grades_set = grades_sub.add_parser("set", help="프로필의 서버 급 배치 설정")
    grades_set.add_argument("--profile", required=True)
    grades_set.add_argument("--grade", required=True, choices=bench.REP_GRADES)
    grades_set.add_argument("--deviation-ref", required=True)
    grades_set.add_argument("--boundary-version")
    grades_sub.add_parser("list", help="서버 급 배치와 코드 표 비교")

    push_catalog = bench_sub.add_parser(
        "push-catalog", help="(profile, effort) 카탈로그를 handoffkeep 에 기록 — 운영자 토큰"
    )
    push_catalog.add_argument("json", nargs="?", help="카탈로그 행 JSON 파일 (C1 스키마)")
    push_catalog.add_argument(
        "--emit-seed",
        action="store_true",
        help="쓰지 않고, 번들 스냅샷에서 만든 최초 시드 JSON 을 stdout 으로",
    )
    push_catalog.add_argument(
        "--decided-by", help="--emit-seed 가 각 행에 넣을 provenance (카탈로그 route 필수 필드)"
    )
    push_catalog.add_argument("--deviation-ref", help="--emit-seed 가 각 행에 넣을 근거 참조 (예: hk:doc/…)")

    catalog_parser = bench_sub.add_parser("catalog", help="정본 카탈로그 조회")
    catalog_sub = catalog_parser.add_subparsers(dest="catalog_command", required=True)
    catalog_sub.add_parser("list", help="카탈로그 행 전체")
    catalog_sub.add_parser("status", help="backend·카탈로그 출처·stale·미커버 프로필")

    reps_parser = subparsers.add_parser("reps", help="실측 대표 실행 기록")
    reps_sub = reps_parser.add_subparsers(dest="reps_command", required=True)
    reps_add = reps_sub.add_parser("add", help="대표 실행 1건 기록")
    reps_add.add_argument("--profile", required=True, help="herdr-spawn 프로필명")
    reps_add.add_argument("--model", dest="model_id", required=True, help="실제 실행 모델")
    reps_add.add_argument("--task", dest="task_ref", required=True, help="Linear 이슈 또는 PR")
    reps_add.add_argument("--tier", required=True, choices=["T0", "T1", "T2", "T3"])
    reps_add.add_argument("--role", required=True, choices=["impl", "verify", "fix", "orch"])
    reps_add.add_argument(
        "--effort", choices=bench.REP_EFFORTS, help="실행 effort (low/medium/high/xhigh/max)"
    )
    reps_add.add_argument(
        "--grade",
        choices=bench.REP_GRADES,
        help="과제가 요구한 급 (S+/S/A+/A/B/C) — 프로필의 급표 배치가 아니라 과제 난이도",
    )
    reps_add.add_argument("--rounds", required=True, type=_nonnegative_int)
    reps_add.add_argument("--blockers-found", required=True, type=_nonnegative_int)
    reps_add.add_argument("--completed", required=True, type=_completed_arg, help="0/1")
    reps_add.add_argument("--input-tokens", type=_nonnegative_int, help="입력 토큰 수")
    reps_add.add_argument("--output-tokens", type=_nonnegative_int, help="출력 토큰 수")
    reps_add.add_argument("--notes")

    reps_list = reps_sub.add_parser("list", help="대표 실행 기록 조회")
    reps_list.add_argument("--limit", type=_nonnegative_int, help="최대 행 수 (1 이상)")
    reps_list.add_argument(
        "--grade",
        choices=bench.REP_GRADES,
        help="과제가 요구한 급 필터 (S+/S/A+/A/B/C) — 프로필의 급표 배치가 아니라 과제 난이도",
    )
    reps_list.add_argument("--profile", help="프로필 필터")
    reps_list.add_argument("--effort", choices=bench.REP_EFFORTS, help="effort 필터")

    reps_compare = reps_sub.add_parser("compare", help="같은 급 안 프로필별 대표 실행 비교")
    reps_compare.add_argument(
        "--grade",
        required=True,
        choices=bench.REP_GRADES,
        help="비교할 과제가 요구한 급 (S+/S/A+/A/B/C) — 프로필의 급표 배치가 아니라 과제 난이도",
    )
    reps_compare.add_argument("--profile", help="프로필 필터")
    reps_compare.add_argument("--effort", choices=bench.REP_EFFORTS, help="effort 필터")

    all_profiles = sorted(
        {p.name for profiles in recommend.GRADE_TABLE.values() for p in profiles}
        | set(recommend.PROFILE_ALIASES)
        | set(recommend.RETIRED_PROFILES.keys())
        | set(recommend.ASTRA_ROLE_PROFILES)
        | set(recommend.CONSULT_ONLY_PROFILES)
    )
    gate_parser = subparsers.add_parser(
        "gate",
        help="profile 하나의 스폰 가능 여부 판정 (exit 0=가능/3=차단/4=측정불가/5=역할 거부)",
    )
    gate_parser.add_argument(
        "-m", "--profile", required=True, choices=all_profiles, help="herdr-spawn profile 이름"
    )
    gate_parser.add_argument(
        "--purpose",
        metavar="PURPOSE",
        help=(
            "astra 역할 제한 프로필의 사용 용도 "
            f"({'|'.join(sorted(recommend.ASTRA_ALLOWED_PURPOSES))}). "
            "허용 용도만 쿼타 검사로 진행하고 미지정·그 외 값은 역할 거부(exit 5) — "
            "비-astra 프로필에서는 무시"
        ),
    )
    gate_parser.add_argument(
        "--operator-request",
        metavar="REF",
        help=(
            "escalation 프로필 전용 운영자 명시 요청 참조 (hk:doc/<key> 또는 hk:task/<정수>만 허용). "
            "감사 가능한 주장을 기록하는 경로이며 운영자 신원·동의를 증명하지 않는다"
        ),
    )
    gate_parser.add_argument(
        "--requested-by",
        metavar="NAME",
        help="--operator-request 의 자기신고 요청자 이름 (기본 unknown) — 신원 증명 아님",
    )
    gate_parser.add_argument(
        "--gate-output",
        metavar="PATH",
        help="gate 판정 감사 레코드를 JSON으로 저장할 경로",
    )
    gate_parser.add_argument("--no-cache", action="store_true", help="캐시 무시하고 강제 조회")
    gate_parser.add_argument(
        "--cache-ttl", type=float, default=None, help="캐시 TTL(초; 지정 시 전 provider 공통)"
    )

    refresh_parser = subparsers.add_parser("refresh", help="한 pool만 이벤트 기반으로 캐시 갱신")
    refresh_parser.add_argument("pool", choices=REFRESH_POOLS, help="갱신할 provider pool")
    refresh_parser.add_argument("--background", action="store_true", help="즉시 반환하고 백그라운드 갱신")
    refresh_parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)

    subparsers.add_parser(
        "herdr-event",
        help="Herdr pane 이벤트를 표시 전용 쿼타 메타데이터로 갱신 (plugin 내부용)",
    )

    models_parser = subparsers.add_parser("models", help="업스트림 서빙 모델 기록/drift 검증")
    models_sub = models_parser.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser(
        "verify",
        help="ClinePass 프로필 요청 슬러그의 라이브 서빙을 기록값과 대조 (drift 시 exit 1)",
    )

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

    if args.policy_command == "launch":
        return _policy_launch_command(args)

    if args.policy_command == "clear":
        if clear_policy(args.pool):
            print(f"{args.pool} policy cleared")
            return 0
        print(f"error: {args.pool} 에 설정된 정책이 없습니다", file=sys.stderr)
        return 2

    return 2


def _manual_command(args: argparse.Namespace) -> int:
    now = dt.datetime.now(dt.UTC)
    try:
        if args.manual_command == "set":
            ttl_s = manual.parse_duration(args.ttl)
            resets_in_s = manual.parse_duration(args.resets_in) if args.resets_in else None
            resets_at = manual.parse_timestamp(args.resets_at, now=now) if args.resets_at else None
            measured_at = manual.parse_timestamp(args.measured_at, now=now)
            entry = manual.record_observation(
                pool=args.pool,
                used_pct=args.used,
                window=args.window,
                measured_at=measured_at,
                reason=args.reason,
                ttl_s=ttl_s,
                resets_in_s=resets_in_s,
                resets_at=resets_at,
                now=now,
            )
            if args.json:
                print(json.dumps(entry, indent=2, ensure_ascii=False, allow_nan=False))
            else:
                print(
                    f"manual set pool={entry['pool']} window={entry['window']} "
                    f"used_pct={entry['used_pct']:g} measured_at={entry['measured_at']} "
                    f"expires_at={entry['expires_at']} source={manual.SOURCE} "
                    f"source_verification={manual.SOURCE_VERIFICATION} label={manual.SOURCE_LABEL} "
                    f"id={entry['manual_observation_id']}"
                )
            return 0
        if args.manual_command == "list":
            payload = manual.list_payload(pool=args.pool, now=now)
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False))
            else:
                print(manual.format_list(payload))
            return 0
        if args.manual_command == "clear":
            event = manual.clear_pool(args.pool, now=now)
            if args.json:
                print(json.dumps(event, indent=2, ensure_ascii=False, allow_nan=False))
            else:
                print(
                    f"manual clear pool={event['pool']} source={manual.SOURCE} "
                    f"source_verification={manual.SOURCE_VERIFICATION} label={manual.SOURCE_LABEL} "
                    f"id={event['manual_clear_id']}"
                )
            return 0
    except manual.ManualError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _recommend_command(args: argparse.Namespace, fetchers: dict[str, object]) -> int:
    now = dt.datetime.now(dt.UTC)
    results = collect(fetchers, list(fetchers), ttl_s=args.cache_ttl, use_cache=not args.no_cache)
    results = manual.apply_for_display(results, now=now)
    bench_scores = bench.read_scores()
    model_prices = bench.read_prices()
    grade_table = bench.runtime_grade_table()
    catalog = bench.read_catalog()
    print(
        recommend.recommend(
            results,
            args.recommend,
            today=now.date(),
            now=now,
            bench_scores=bench_scores,
            model_prices=model_prices,
            explain=bool(getattr(args, "explain", False)),
            hide_excluded=bool(getattr(args, "hide_excluded", False)),
            grade_table=grade_table,
        )
    )
    # Provenance line, always printed: a table that silently came from the
    # bundled snapshot instead of the canon is the failure this whole change
    # exists to make impossible to miss (hk:doc 2558).
    print(catalog.label)
    return 0


def _gate_record(
    result: recommend.GateResult, exit_code: int, now: dt.datetime, purpose: str | None = None
) -> dict:
    """gate 판정의 감사 레코드 (``--gate-output`` JSON 본체)."""
    return {
        "schema": "scopefuel.gate.v1",
        "generated_at": now.isoformat(),
        "profile": result.profile,
        "grade": result.grade,
        "provider_id": result.provider_id,
        "ok": result.ok,
        "exit_code": exit_code,
        "unmeasurable": result.unmeasurable,
        "stale_accepted": result.stale_accepted,
        "used_pct": result.used_pct,
        "pool_class": result.pool_class,
        "reason": result.reason,
        "alternatives": list(result.alternatives),
        "escalation_override": result.escalation_override,
        "operator_request_ref": result.operator_request_ref,
        "requested_by": result.requested_by,
        "ref_resolution": result.ref_resolution,
        "role_denied": result.role_denied,
        "purpose": purpose,
        "source": result.source,
        "source_verification": result.source_verification,
        "source_label": result.source_label,
        "manual_observation_ids": list(result.manual_observation_ids),
        "manual_observations": list(result.manual_observations),
        "measured_at": result.measured_at,
        "expires_at": result.expires_at,
        "observed_age_s": result.observed_age_s,
        "remaining_effect_s": result.remaining_effect_s,
        "last_auto_error": result.last_auto_error,
    }


def _gate_args(args: argparse.Namespace) -> dict:
    return {
        "operator_request": args.operator_request,
        "requested_by": args.requested_by,
        "purpose": args.purpose,
    }


def _manual_gate_audit(
    result: recommend.GateResult,
    resolution: manual.Resolution,
) -> recommend.GateResult:
    selected = resolution.selected_entries
    observed_age_s = max(float(entry.get("age_s") or 0.0) for entry in selected)
    remaining_effect_s = min(float(entry.get("remaining_effect_s") or 0.0) for entry in selected)
    measured_at = min(str(entry["measured_at"]) for entry in selected)
    expires_at = min(str(entry["expires_at"]) for entry in selected)
    observations = tuple(
        {
            "manual_observation_id": entry["manual_observation_id"],
            "account_ref": entry["account_ref"],
            "entitlement_and_limits": entry["entitlement_and_limits"],
            "window": entry["window"],
            "measured_at": entry["measured_at"],
            "entered_at": entry["entered_at"],
            "expires_at": entry["expires_at"],
            "supersedes_ref": entry["supersedes_ref"],
            "status": entry["status"],
            "source": manual.SOURCE,
            "source_verification": manual.SOURCE_VERIFICATION,
            "source_label": manual.SOURCE_LABEL,
        }
        for entry in selected
    )
    observation_ids = ",".join(str(entry["manual_observation_id"]) for entry in selected)
    covered_windows = ",".join(str(entry["window"]) for entry in selected)
    supersedes_prior = any(entry.get("supersedes_ref") is not None for entry in selected)
    audit = (
        f"source={manual.SOURCE} · {manual.SOURCE_LABEL} · "
        f"수동 항목 {observation_ids} · covered limits {covered_windows} · "
        f"supersedes {'yes' if supersedes_prior else 'no'} · "
        f"관측 {manual.format_age_seconds(observed_age_s)} 전 · "
        f"남은 효력 {manual.format_age_seconds(remaining_effect_s)} · "
        f"자동 측정 마지막 오류 {resolution.last_auto_error}"
    )
    return replace(
        result,
        reason=f"{result.reason} [{audit}]",
        source=manual.SOURCE,
        source_verification=manual.SOURCE_VERIFICATION,
        source_label=manual.SOURCE_LABEL,
        manual_observation_ids=tuple(str(entry["manual_observation_id"]) for entry in selected),
        manual_observations=observations,
        measured_at=measured_at,
        expires_at=expires_at,
        observed_age_s=round(observed_age_s, 1),
        remaining_effect_s=round(remaining_effect_s, 1),
        last_auto_error=resolution.last_auto_error,
    )


def _gate_command(args: argparse.Namespace, fetchers: dict[str, object]) -> int:
    now = dt.datetime.now(dt.UTC)
    automatic_results = collect(fetchers, list(fetchers), ttl_s=args.cache_ttl, use_cache=not args.no_cache)
    # ``read_scores`` itself preserves the legacy local route and only enters
    # the cache/network path for the configured canonical backend.  Passing it
    # here keeps gate alternatives on the same benchmark view as recommend.
    bench_scores = bench.read_scores()
    model_prices = bench.read_prices()
    grade_table = bench.runtime_grade_table()
    result = recommend.gate_check(
        automatic_results,
        args.profile,
        today=now.date(),
        now=now,
        bench_scores=bench_scores,
        model_prices=model_prices,
        grade_table=grade_table,
        **_gate_args(args),
    )

    # Manual observations are considered only after the automatic path is
    # genuinely unmeasurable.  A cached automatic snapshot that already proves
    # cutoff/exclude remains authoritative and cannot be hidden by a lower
    # manual number.
    if result.unmeasurable:
        provider_id, group_name = recommend.profile_pool(args.profile)
        target = next((item for item in automatic_results if item.id == provider_id), None)
        if target is not None:
            trusted_snapshot = replace(
                target,
                error=None,
                warning=None,
                stale=False,
                manual=None,
            )
            snapshot_results = [
                trusted_snapshot if item.id == provider_id else item for item in automatic_results
            ]
            failure_kind, _failure_text = manual.classify_automatic_failure(target)
            confirmed_cutoff = manual.confirmed_automatic_cutoff(target, now=now)
            snapshot_gate = (
                recommend.gate_check(
                    snapshot_results,
                    args.profile,
                    today=now.date(),
                    now=now,
                    bench_scores=bench_scores,
                    model_prices=model_prices,
                    grade_table=grade_table,
                    **_gate_args(args),
                )
                if failure_kind != "auth"
                and target.buckets
                and (target.fetched_at is not None or confirmed_cutoff is not None)
                else result
            )
            if not snapshot_gate.ok and not snapshot_gate.unmeasurable:
                result = snapshot_gate
            else:
                try:
                    resolution = manual.resolve_result(target, now=now, group_name=group_name)
                except manual.ManualError as exc:
                    resolution = None
                    result = replace(result, reason=f"{result.reason} [manual store 오류: {exc}]")
                if resolution is not None and resolution.applied:
                    effective_results = [
                        resolution.result if item.id == provider_id else item for item in automatic_results
                    ]
                    result = recommend.gate_check(
                        effective_results,
                        args.profile,
                        today=now.date(),
                        now=now,
                        bench_scores=bench_scores,
                        model_prices=model_prices,
                        grade_table=grade_table,
                        **_gate_args(args),
                    )
                    result = _manual_gate_audit(result, resolution)
                elif resolution is not None:
                    result = replace(
                        result,
                        reason=(
                            f"{result.reason} "
                            f"[manual fallback 불가: {resolution.failure_reason or 'reason unavailable'}]"
                        ),
                        last_auto_error=resolution.last_auto_error,
                    )
    exit_code = 0 if result.ok else (5 if result.role_denied else (4 if result.unmeasurable else 3))

    if args.gate_output:
        record = _gate_record(result, exit_code, now, purpose=args.purpose)
        try:
            pathlib.Path(args.gate_output).write_text(
                json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            )
        except OSError as exc:
            print(f"error: --gate-output 기록 실패 ({exc})", file=sys.stderr)
            return 2

    if result.ok:
        first_line = (
            f"profile={result.profile} pool={result.provider_id} "
            f"used_pct={result.used_pct} class={result.pool_class}"
        )
        if result.stale_accepted:
            first_line += " stale_accepted=true"
        if result.operator_request_ref is not None:
            first_line += (
                f" escalation_override={'true' if result.escalation_override else 'false'}"
                f" operator_request_ref={result.operator_request_ref}"
                f" requested_by={result.requested_by or 'unknown'}"
                f" ref_resolution={result.ref_resolution or 'unverified'}"
            )
        if result.source == manual.SOURCE:
            windows = ",".join(str(observation["window"]) for observation in result.manual_observations)
            supersedes_prior = any(
                observation.get("supersedes_ref") is not None for observation in result.manual_observations
            )
            first_line += (
                f" source={manual.SOURCE}"
                f" source_verification={manual.SOURCE_VERIFICATION}"
                " source_claim=self_reported_unverified"
                f" manual_observation_ids={','.join(result.manual_observation_ids)}"
                f" manual_windows={windows}"
                f" manual_supersedes={'true' if supersedes_prior else 'false'}"
                f" measured_at={result.measured_at}"
                f" expires_at={result.expires_at}"
            )
        print(first_line)
        print(result.reason)
        return 0

    print(result.reason, file=sys.stderr)
    if result.role_denied:
        # task #527: a role denial is not a quota refusal — print no
        # "alternatives exhausted" line (the profile has no grade) and say
        # what would actually change the answer.
        print(
            "역할 거부 — 쿼타와 무관. 허용 용도로 --purpose 를 지정해야 쿼타 검사로 진행한다",
            file=sys.stderr,
        )
    elif result.alternatives:
        print(f"대안({result.grade}): {', '.join(result.alternatives)}", file=sys.stderr)
    else:
        print(f"대안({result.grade}) 없음 — 동일 grade 정상 후보 전부 소진/측정불가", file=sys.stderr)
    return exit_code


def _policy_launch_command(args: argparse.Namespace) -> int:
    """Resolve one launch. rc 3 is a refusal, rc 2 is a usage/plumbing error.

    The two are kept apart because a launcher has to react differently: rc 3
    means "the canon says no" (do not start), rc 2 means "scopefuel could not
    answer" (fall back to the bundled values and say so).
    """

    try:
        decision = launch.resolve_launch(
            args.profile,
            effort=args.effort,
            operator_request=bool(args.operator_request),
        )
    except launch.LaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except bench.BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if decision.catalog_stale:
        print(
            "warning: catalog=stale — resolved from the bundled snapshot, not the server",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps(decision.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(decision.render())
    return 0


def _seed_catalog_json(args: argparse.Namespace) -> int:
    decided_by = args.decided_by or "operator-seed"
    deviation_ref = args.deviation_ref or "hk:doc/task/2026-09-23/scopefuel-catalog-server-canonical"
    rows = []
    for entry in launch.snapshot_entries():
        row = entry.as_dict()
        row["decided_by"] = decided_by
        row["deviation_ref"] = deviation_ref
        rows.append(row)
    print(json.dumps({"catalog": rows}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _push_catalog_command(args: argparse.Namespace) -> int:
    if args.emit_seed:
        return _seed_catalog_json(args)
    if not args.json:
        print("error: push-catalog needs a JSON file (or --emit-seed)", file=sys.stderr)
        return 2
    try:
        written = bench.push_catalog(args.json)
    except bench.BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"catalog rows written: {written}")
    return 0


def _bench_command(args: argparse.Namespace) -> int:
    if args.bench_command == "sync":
        return bench.run_sync(stderr=sys.stderr)
    if args.bench_command == "migrate-effort":
        count = bench.migrate_aa_model_effort_suffixes()
        print(f"bench migrate-effort: migrated {count} row(s)")
        return 0
    if args.bench_command == "backfill-aa-metrics":
        count = bench.backfill_aa_agent_metrics()
        print(f"bench backfill-aa-metrics: updated {count} row(s)")
        return 0
    if args.bench_command == "coverage":
        print(bench.coverage_report())
        return 0
    if args.bench_command == "push-local":
        try:
            score_count, rep_count = bench.push_local()
        except bench.BenchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"bench push-local: pushed {score_count} score(s), {rep_count} rep(s)")
        return 0
    if args.bench_command == "grades":
        if args.grades_command == "list":
            print(bench.grades_report())
            return 0
        if args.grades_command == "set":
            try:
                count = bench.set_grade(
                    profile=args.profile,
                    grade=args.grade,
                    deviation_ref=args.deviation_ref,
                    boundary_version=args.boundary_version,
                )
            except bench.BenchError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(f"bench grades set: stored {count} grade(s)")
            return 0
    if args.bench_command == "push-catalog":
        return _push_catalog_command(args)

    if args.bench_command == "catalog":
        if args.catalog_command == "list":
            print(bench.catalog_report())
            return 0
        if args.catalog_command == "status":
            print(bench.catalog_status_report())
            return 0
        return 2

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
                effort=args.effort,
                grade=args.grade,
                rounds=args.rounds,
                blockers_found=args.blockers_found,
                completed=args.completed,
                input_tokens=args.input_tokens,
                output_tokens=args.output_tokens,
                notes=args.notes,
            )
        except bench.BenchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"recorded rep id={rep.id}")
        return 0
    if args.reps_command == "list":
        try:
            reps = bench.read_reps(
                limit=args.limit,
                grade=args.grade,
                profile=args.profile,
                effort=args.effort,
            )
        except bench.BenchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not reps:
            filters = []
            if args.grade:
                filters.append(f"grade={args.grade}")
            if args.profile:
                filters.append(f"profile={args.profile}")
            if args.effort:
                filters.append(f"effort={args.effort}")
            suffix = " (" + ", ".join(filters) + ")" if filters else ""
            print(f"reps 기록 없음 — 조건에 맞는 대표 실행이 없습니다{suffix}")
            return 0
        for rep in reps:
            print(bench.format_rep(rep))
        return 0
    if args.reps_command == "compare":
        try:
            comparisons = bench.compare_reps(
                grade=args.grade,
                profile=args.profile,
                effort=args.effort,
            )
        except bench.BenchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"reps 비교 grade={args.grade}")
        if not comparisons:
            print("reps 비교 기록 없음 — 비교할 프로필별 대표 실행이 없습니다")
            return 0
        for comparison in comparisons:
            print(bench.format_rep_comparison(comparison))
        return 0
    return 2


def _models_command(args: argparse.Namespace) -> int:
    if args.models_command != "verify":
        return 2
    key = served.clinepass_key()
    verdicts, exit_code = served.run_verification(key=key)
    print(served.render(verdicts, key_present=bool(key)))
    return exit_code


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

    if args.command == "manual":
        return _manual_command(args)

    if args.command == "gate":
        return _gate_command(args, fetchers)

    if args.command == "refresh":
        if args._worker:
            return run_worker(fetchers, args.pool)
        return spawn(args.pool, background=args.background)

    if args.command == "herdr-event":
        return herdr.handle_event(fetchers)

    if args.command == "models":
        return _models_command(args)

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
        if not args.raw:
            results = manual.apply_for_display(results, now=now)
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
