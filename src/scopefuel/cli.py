"""scopefuel CLI.

에이전트가 소비하는 계약은 `--json` (schema=scopefuel.v1) 과 `--exit-code-on` 이다.
사람이 보는 표/한 줄은 그 위의 표현일 뿐이다.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import pathlib
import sys
import time
from dataclasses import replace

from . import bench, grades, herdr, launch, manual, quota_share, quota_v2, recommend, render, served
from .cache import collect
from .model import SCHEMA, ProviderResult, account_tag, overall_mark, overall_usage_mark
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
    launch_parser.add_argument(
        "--purpose",
        help="호출자가 선언한 용도 (task #527). astra 정체성에 한해 허용 용도이면 "
        "consult_only 를 충족한다 — fable 에는 적용되지 않는다",
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

    reps_migrate = reps_sub.add_parser(
        "migrate", help="로컬 bench.db reps를 handoffkeep reps 저장소로 1회 이관 (기본 dry-run)"
    )
    reps_migrate.add_argument("--apply", action="store_true", help="실제로 기록 (기본은 dry-run)")
    reps_migrate.add_argument(
        "--allow-plaintext-http",
        action="store_true",
        help="이번 실행 한정으로 평문 http endpoint 허용 (allow_plaintext_reps 의 1회성 대안)",
    )
    reps_migrate.add_argument("--host", help="이관 행에 기록할 출처 호스트 (기본: 이 머신의 hostname)")
    reps_migrate.add_argument(
        "--sample", type=_nonnegative_int, default=5, help="dry-run 에서 보일 to-insert 샘플 수"
    )
    reps_migrate.add_argument(
        "--force",
        action="store_true",
        help="원격이 같은 derived id 로 다른 rep 을 이미 갖고 있어도 덮어쓰기 진행",
    )

    grades_parser = subparsers.add_parser(
        "grades", help="측정 rep 증거로 카탈로그 (profile, effort) 런그 급 제안/적용"
    )
    grades_sub = grades_parser.add_subparsers(dest="grades_command", required=True)
    grades_propose = grades_sub.add_parser("propose", help="rep 증거 평가 → 런그별 급 변경 제안 (읽기 전용)")
    grades_propose.add_argument(
        "--min-passes",
        type=_nonnegative_int,
        default=grades.MIN_PASSES,
        help=f"승급에 필요한 같은-급 PASS 수 (기본 {grades.MIN_PASSES}; decision 4088 draft N=2)",
    )
    grades_propose.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="대표 실행 중복 제외 — OLD 는 NEW 에 의해 superseded 된 ref "
        "(N | srv:N | local:N | local@host:N). 반복 가능",
    )
    grades_propose.add_argument(
        "--rung",
        metavar="PROFILE[@EFFORT]",
        help="한 런그의 증거 전체를 detail 출력 (카탈로그 스펠링)",
    )
    grades_propose.add_argument("--json", action="store_true", help="proposal artifact JSON 출력")
    grades_propose.add_argument("--out", help="--json 결과를 이 파일에 기록")
    grades_propose.add_argument(
        "--allow-plaintext-http",
        action="store_true",
        help="이번 실행 한정 평문 http endpoint 허용 (allow_plaintext_reps 의 1회성 대안)",
    )

    grades_apply = grades_sub.add_parser(
        "apply", help="propose artifact 를 재검증하고 갱신된 카탈로그 JSON 생성"
    )
    grades_apply.add_argument(
        "--proposal", required=True, help="grades propose --json --out 으로 만든 artifact 파일"
    )
    grades_apply.add_argument("--out", required=True, help="갱신된 카탈로그 JSON 출력 경로 (C1)")
    grades_apply.add_argument("--decided-by", required=True, help="카탈로그 행 provenance (필수)")
    grades_apply.add_argument(
        "--deviation-ref",
        default="hk:decision/2026-09-26/effort-efficiency",
        help="변경 근거 참조 (기본: decision 4088)",
    )
    grades_apply.add_argument(
        "--allow-plaintext-http",
        action="store_true",
        help="이번 실행 한정 평문 http endpoint 허용 (allow_plaintext_reps 의 1회성 대안)",
    )

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
            "escalation/consult_only 프로필 전용 운영자 명시 요청 참조 (hk:doc/<key> 또는 "
            "hk:task/<정수>만 허용). fable 같은 consult_only 프로필은 이 REF 없이는 거부된다. "
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
    gate_parser.add_argument(
        "--effort",
        choices=sorted(rung for rung in bench.CATALOG_EFFORT_RANKS if rung),
        help=(
            "#692: 판정할 런그(예: low/max). 주어지면 그 런그의 행으로 판정하고, "
            "표가 모르는 런그는 프로필 기본 배치로 답한다. #716: 배치 행은 그 런그의 "
            "쿼타 규칙으로 판정한다(escalation 대안 거부 없음). 미측정 E6 측정 런그는 "
            f"{recommend.E6_ARM_MARKER_ENV}=<profile>@<effort> 표식이 있을 때만 열린다"
        ),
    )

    refresh_parser = subparsers.add_parser("refresh", help="한 pool만 이벤트 기반으로 캐시 갱신")
    refresh_parser.add_argument("pool", choices=REFRESH_POOLS, help="갱신할 provider pool")
    refresh_parser.add_argument("--background", action="store_true", help="즉시 반환하고 백그라운드 갱신")
    refresh_parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)

    subparsers.add_parser(
        "herdr-event",
        help="Herdr pane 이벤트를 표시 전용 쿼타 메타데이터로 갱신 (plugin 내부용)",
    )

    v2_parser = subparsers.add_parser(
        "quota-v2",
        help="계정 단위 쿼타 v2 (#578 1단계, shadow 전용 — 실제 gate 판정은 바꾸지 않음)",
    )
    v2_sub = v2_parser.add_subparsers(dest="v2_command", required=True)
    v2_slot = v2_sub.add_parser(
        "slot", help="이 실행 환경의 login slot 위치자 (운영자 등록 입력용, 신원 아님)"
    )
    v2_slot.add_argument("--provider", required=True)
    v2_bindings = v2_sub.add_parser("bindings", help="hub binding 미러")
    v2_bindings_sub = v2_bindings.add_subparsers(dest="v2_bindings_command", required=True)
    v2_bindings_import = v2_bindings_sub.add_parser(
        "import", help="node 토큰으로 받은 hub GET /v2/quota/bindings 응답을 미러로 저장"
    )
    v2_bindings_import.add_argument("file", help="JSON 파일 경로 (- 는 stdin)")
    v2_bindings_sub.add_parser("show", help="미러와 provider별 현재 신원 해석 결과")
    v2_obs = v2_sub.add_parser("observations", help="account-scoped 관측 저장소")
    v2_obs_sub = v2_obs.add_subparsers(dest="v2_obs_command", required=True)
    v2_obs_import = v2_obs_sub.add_parser(
        "import", help="hub GET /v2/quota/observations 응답을 저장 (binding 있는 계정만)"
    )
    v2_obs_import.add_argument("file", help="JSON 파일 경로 (- 는 stdin)")
    v2_obs_export = v2_obs_sub.add_parser(
        "export", help="이 node 가 측정하고 아직 hub 수신 전인 관측을 JSONL 로 (POST 본문용)"
    )
    v2_obs_export.add_argument("--provider", required=True)
    v2_eval = v2_sub.add_parser("evaluate", help="로컬 v2 스냅샷만으로 판정 (네트워크 0, 진단 전용)")
    v2_eval.add_argument("-m", "--profile", required=True, choices=all_profiles)
    v2_eval.add_argument("--purpose", metavar="PURPOSE")

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
    result: recommend.GateResult,
    exit_code: int,
    now: dt.datetime,
    purpose: str | None = None,
    account: str | None = None,
) -> dict:
    """gate 판정의 감사 레코드 (``--gate-output`` JSON 본체)."""
    return (
        {
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
            "exhaust_notice": result.exhaust_notice,
            "missing_windows": list(result.missing_windows),
        }
        | ({"account": account} if account else {})
        | (
            # #692: only an opened E6 measurement rung carries this — every other
            # record stays byte-identical (the #635 golden compares whole records).
            {"e6_arm": result.e6_arm} if result.e6_arm else {}
        )
    )


def _gate_args(args: argparse.Namespace) -> dict:
    return {
        "operator_request": args.operator_request,
        "requested_by": args.requested_by,
        "purpose": args.purpose,
    }


def _e6_arm_marker() -> str | None:
    """The spawner's E6 arm declaration, read from the environment (#692).

    One mechanism, set by the spawner on the spawn command:
    ``SCOPEFUEL_E6_ARM=<profile>@<effort>``. wrk forwards its environment to the
    ``scopefuel`` subprocesses it calls, so both the catalog route and the quota
    gate see the same declaration. Unset (the default) means no E6 arm.
    """

    return os.environ.get(recommend.E6_ARM_MARKER_ENV)


def _retired_e6_rungs() -> frozenset[tuple[str, str]]:
    """#692: the E6 rungs the canon retired.

    A marker must not revive a rung the operator closed, and the gate has no
    catalog view of its own — the runtime grade table drops retired rows without
    saying why. Read the view (memoised per process) and hand the keys over.
    """

    try:
        view = bench.read_catalog()
    except bench.BenchError:
        return frozenset()
    return frozenset(
        (entry.profile, entry.effort)
        for entry in view.entries
        if entry.retired_at and (entry.profile, entry.effort) in recommend.E6_ARM_KEYS
    )


def _gate_rung_args(args: argparse.Namespace) -> dict:
    """#692: the rung context — the explicit ``--effort`` plus the E6 marker.

    Kept apart from :func:`_gate_args` so the v2 shadow, which has no rung input
    in its contract, is not handed keys it cannot consume.
    """

    return {
        "effort": getattr(args, "effort", None),
        "e6_arm": _e6_arm_marker(),
        "retired_rungs": _retired_e6_rungs(),
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


def _shadow_gate(
    args, fetchers, automatic_results, result, exit_code, now, bench_scores, model_prices, grade_table
) -> None:
    # The shadow evaluates at its own moment: the attempts it reads were stamped
    # at fetch completion, after the gate's `now` was taken (contract §5.1, §5.6).
    quota_v2.shadow_gate(
        profile=args.profile,
        legacy=result,
        legacy_exit=exit_code,
        now=dt.datetime.now(dt.UTC),
        pool_classes={item.id: item.pool_class for item in automatic_results},
        bench_scores=bench_scores,
        model_prices=model_prices,
        grade_table=grade_table,
        gate_kwargs=_gate_args(args),
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
        **_gate_rung_args(args),
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
                    **_gate_rung_args(args),
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
                        **_gate_rung_args(args),
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
    # task #654 — 원격 스냅샷(hk 문서)으로 판정했으면 provenance 를 남긴다.
    # 자기신고(source=operator)와 구분되는 "remote measured (host)" 라벨이다.
    provider_id = recommend.profile_pool(args.profile)[0]
    remote_used = next(
        (
            item
            for item in automatic_results
            if item.id == provider_id and item.source == quota_share.REMOTE_SOURCE
        ),
        None,
    )
    if remote_used is not None and result.source != manual.SOURCE:
        result = replace(
            result,
            source=quota_share.REMOTE_SOURCE,
            source_label=remote_used.note,
            measured_at=(
                dt.datetime.fromtimestamp(remote_used.fetched_at, dt.UTC).isoformat()
                if remote_used.fetched_at is not None
                else None
            ),
            observed_age_s=remote_used.age_s,
        )
    exit_code = 0 if result.ok else (5 if result.role_denied else (4 if result.unmeasurable else 3))

    # task #578 1단계 — shadow 전용: v2(account-scoped) 판정을 계산해 비교 로그에만 남긴다.
    # result·exit_code·출력은 건드리지 않으며, 미등록 노드에서는 아무것도 하지 않는다.
    with contextlib.suppress(Exception):
        _shadow_gate(
            args, fetchers, automatic_results, result, exit_code, now, bench_scores, model_prices, grade_table
        )

    # task #659 — 이 판정이 어느 계정의 측정에 기반하는지 통과·거부 모두 보인다
    # (지문 앞 8자 + 안전 라벨 — 이메일·토큰은 절대 표시하지 않는다).
    measured = next((item for item in automatic_results if item.id == provider_id), None)
    tag = (
        account_tag(measured.account_fp, measured.account_label, measured.account_fp_kind)
        if measured is not None
        else ""
    )

    if args.gate_output:
        record = _gate_record(result, exit_code, now, purpose=args.purpose, account=tag or None)
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
        if tag:
            first_line += f' account="{tag}"'
        # #692: an opened E6 measurement rung says so on the allow line itself —
        # a C-graded rung admitted for an E6 arm must be visible, not implied.
        if result.e6_arm:
            first_line += f" {recommend.e6_arm_tag(result.e6_arm)}"
        if result.stale_accepted:
            first_line += " stale_accepted=true"
        if result.operator_request_ref is not None:
            first_line += (
                f" escalation_override={'true' if result.escalation_override else 'false'}"
                f" operator_request_ref={result.operator_request_ref}"
                f" requested_by={result.requested_by or 'unknown'}"
                f" ref_resolution={result.ref_resolution or 'unverified'}"
            )
        if result.source == quota_share.REMOTE_SOURCE:
            first_line += (
                f" source={quota_share.REMOTE_SOURCE}"
                f' source_label="{result.source_label}"'
                f" measured_at={result.measured_at}"
                f" observed_age_s={result.observed_age_s}"
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

    deny_reason = result.reason if not tag else f'{result.reason} account="{tag}"'
    print(deny_reason, file=sys.stderr)
    if result.role_denied:
        # task #527: a role denial is not a quota refusal — print no
        # "alternatives exhausted" line (the profile has no grade) and say
        # what would actually change the answer.
        print(
            "역할 거부 — 쿼타와 무관. 허용 용도로 --purpose 를 지정해야 쿼타 검사로 진행한다",
            file=sys.stderr,
        )
    elif result.e6_arm and not result.ok:
        # #692: an E6 rung refusal is decided on the rung (the marker is missing,
        # or the canon retired it), not on alternatives — the generic "no
        # alternatives" line would misread as quota exhaustion. The reason line
        # above carries the specific remedy.
        print(
            f"E6 측정 런그 판정 — {recommend.E6_ARM_MARKER_ENV}={result.e6_arm} (#594, 사유 첫 줄 참조)",
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
            purpose=getattr(args, "purpose", None),
            e6_arm=_e6_arm_marker(),
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
    # #692: the E6 measurement rungs are catalog rows too — the canon has to carry
    # them, or a server-backed host cannot resolve the arm. Each keeps its own
    # deviation_ref (the E6 plan) rather than the generic seed provenance.
    for entry in launch.e6_arm_entries():
        row = entry.as_dict()
        row["decided_by"] = decided_by
        row["deviation_ref"] = entry.deviation_ref or deviation_ref
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
    if args.reps_command == "migrate":
        try:
            result = bench.migrate_reps(
                apply=args.apply,
                host=args.host,
                allow_plaintext_http=args.allow_plaintext_http,
                force=args.force,
            )
        except bench.BenchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not result.applied:
            overwrite = f" would-overwrite={len(result.would_overwrite)}" if result.would_overwrite else ""
            print(
                f"reps migrate (dry-run) host={result.host} local={result.local_count} "
                f"already-present={result.present_count} to-insert={len(result.pending)}{overwrite}"
            )
            for rep in result.pending[: args.sample]:
                print(f"  {bench.format_rep(rep)}")
            if result.would_overwrite:
                print(
                    "warning: --apply will refuse these rows without --force "
                    "(derived id already taken by a different remote rep)"
                )
            print("pass --apply to write")
            return 0
        print(
            f"reps migrate applied host={result.host} inserted={result.inserted_count} "
            f"skipped={result.present_count}"
        )
        print(
            f"reconcile: local={result.local_count} remote-this-host={result.remote_for_host} "
            f"missing={len(result.missing)} extra-remote={result.extra_remote_count}"
        )
        for rep in result.missing[:10]:
            print(f"  missing: {bench.format_rep(rep)}")
        if len(result.missing) > 10:
            print(f"  ... +{len(result.missing) - 10} more")
        if result.missing:
            return 2
        return 0
    return 2


def _grades_command(args: argparse.Namespace) -> int:
    if args.grades_command == "propose":
        if args.min_passes < 1:
            print("error: --min-passes 는 1 이상이어야 합니다", file=sys.stderr)
            return 2
        exclusions: list[tuple[str, str]] = []
        for spec in args.exclude:
            old, sep, new = spec.partition("=")
            if not sep or not old.strip() or not new.strip():
                print(f"error: --exclude 는 OLD=NEW 형식이어야 합니다: {spec!r}", file=sys.stderr)
                return 2
            exclusions.append((old.strip(), new.strip()))
        try:
            view = bench.read_catalog(commit_cache=False, allow_plaintext_http=args.allow_plaintext_http)
            evidence = grades.gather_reps(
                view=view,
                exclusions=exclusions,
                allow_plaintext_http=args.allow_plaintext_http,
            )
            proposal = grades.evaluate(evidence, view, min_passes=args.min_passes)
        except bench.BenchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            payload = grades.proposal_to_json(proposal, view)
            text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            if args.out:
                pathlib.Path(args.out).write_text(text, encoding="utf-8")
                print(f"proposal artifact written: {args.out} digest={proposal.digest}")
            else:
                print(text, end="")
        else:
            focus = grades.parse_rung_spec(args.rung) if args.rung else None
            print(grades.render_proposal(proposal, view, focus=focus))
        return 0
    if args.grades_command == "apply":
        try:
            proposal_file = json.loads(pathlib.Path(args.proposal).read_text(encoding="utf-8"))
            entries, live, _view = grades.apply_proposals(
                proposal_file,
                decided_by=args.decided_by,
                deviation_ref=args.deviation_ref,
                allow_plaintext_http=args.allow_plaintext_http,
            )
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        changes = live.changes()
        if not changes:
            print("grades apply: proposal carries no grade changes — nothing written")
            return 0
        # The "catalog" list carries only the stamped changed rows so the file
        # can go straight into `bench push-catalog` (which requires decided_by
        # on every row it PUTs). The full post-apply catalog rides along under
        # "snapshot" for the audit record.
        changed_rows = {r.key for r in changes}
        payload = {
            "catalog": [e.as_dict() for e in entries if e.key in changed_rows],
            "snapshot": [e.as_dict() for e in entries],
        }
        pathlib.Path(args.out).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"grades apply: wrote {args.out} changed={len(changed_rows)} rows={len(entries)}")
        for result in changes:
            print(
                f"  {result.action} {result.label()} "
                f"{result.row.grade} -> {result.target} "
                f"(evidence: {', '.join(result.evidence_refs)})"
            )
        print("propagate with: scopefuel bench push-catalog <out> (operator token)")
        return 0
    return 2


def _read_json_arg(path: str) -> object:
    text = sys.stdin.read() if path == "-" else pathlib.Path(path).read_text()
    return json.loads(text)


def _quota_v2_command(args: argparse.Namespace, fetchers: dict[str, object]) -> int:
    now = dt.datetime.now(dt.UTC)
    try:
        if args.v2_command == "slot":
            slot = quota_v2.slot_locator(args.provider)
            if slot is None:
                print("error: slot 위치자를 만들 환경 변수가 없다", file=sys.stderr)
                return 2
            print(slot)
            return 0
        if args.v2_command == "bindings" and args.v2_bindings_command == "import":
            mirror = quota_v2.import_bindings(_read_json_arg(args.file))
            print(f"bindings={len(mirror['bindings'])} machine_id={mirror['machine_id']}")
            return 0
        if args.v2_command == "bindings":
            mirror = quota_v2.load_bindings()
            rows = []
            for name in fetchers:
                identity, reason = quota_v2.resolve_identity(name, now.timestamp(), mirror=mirror)
                rows.append({"provider": name, "reason": reason, "identity": identity and identity.as_dict()})
            print(json.dumps({"mirror": mirror, "resolved": rows}, indent=2, ensure_ascii=False))
            return 0
        if args.v2_command == "observations" and args.v2_obs_command == "import":
            added = quota_v2.import_observations(_read_json_arg(args.file))
            print(f"added={added}")
            return 0
        if args.v2_command == "observations":
            identity, reason = quota_v2.resolve_identity(args.provider, now.timestamp())
            if identity is None:
                print(f"error: 신원 불명 ({reason})", file=sys.stderr)
                return 4
            for obs in quota_v2.export_observations(identity):
                print(json.dumps(obs, ensure_ascii=False))
            return 0
        provider, _ = recommend.profile_pool(args.profile)
        identity, _reason = quota_v2.resolve_identity(provider, now.timestamp())
        evaluation = quota_v2.evaluate(
            quota_v2.snapshot_for(provider, identity), args.profile, now=now, purpose=args.purpose
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(evaluation.as_dict(), indent=2, ensure_ascii=False))
    return 0


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

    if args.command == "grades":
        return _grades_command(args)

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

    if args.command == "quota-v2":
        return _quota_v2_command(args, fetchers)

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
