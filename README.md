# scopefuel

**Scope-aware headroom gauge for AI coding agent plans.** 여러 AI 코딩 플랜(Claude Code, OpenAI
Codex, Google Antigravity …)의 남은 한도를 한 번에 조회하되, **무엇이 실제로 막히는지**를 구분해서
보여줍니다. 사람이 보는 표와 에이전트가 읽는 JSON을 같은 데이터로 제공합니다.

```
$ scopefuel
claude [max]  [CRIT] 지금(5h급) 6% · 이번주 97%
  now  5h                          6%   reset 07-25 13:09
  week 7d all                     97%   reset 07-25 17:59
  week 7d Fable                  100%   reset 07-25 17:59  [active, 이 모델만]
  ! Fable 소진(100%, reset 07-25 17:59) — 다른 모델은 계정 한도 범위에서 사용 가능

codex [pro]  [WARN] 지금(5h급) ? · 이번주 82%
  week 7d                         82%   reset 07-29 02:31
  week GPT-5.3-Codex-Spark 7d      0%   reset 08-01 12:05  [이 모델만]

agy  [ok] 그룹별 독립: 3p 57.3% / gemini 7.4%
  week gemini weekly             2.1%   reset 07-29 06:00
  now  gemini 5h                 7.4%   reset 07-25 15:53
  week 3p weekly                19.1%   reset 08-01 11:03
  now  3p 5h                    57.3%   reset 07-25 16:03
```

## 왜 또 하나의 쿼타 도구인가

이미 좋은 도구들이 있습니다([cclimits](https://github.com/cruzanstx/cclimits),
[CodexBar](https://github.com/steipete/CodexBar) 등). scopefuel이 다르게 하는 것은 **두 축의 분리**뿐입니다.

- **scope — 무엇이 막히는가**: `account`(계정 전체) / `model`(그 모델만) / `group`(그 모델 그룹만).
  하나의 최대값으로 뭉개면 "특정 모델 하나 소진"을 "계정 차단"으로 오독합니다. 위 예시의 Fable 100%가
  정확히 그 경우이고, Opus·Sonnet은 계정 한도(97%) 범위에서 여전히 쓸 수 있습니다.
- **horizon — 언제의 이야기인가**: `now`(5시간급 창) / `week`(주간급 창).
  "지금 워커를 띄울 수 있나"와 "이번 주 예산이 남았나"는 다른 질문이고, 라우팅 결정도 갈립니다.

provider와 계정에는 **읽기 전용**입니다. 토큰을 갱신하거나 provider 한도를 조작하지 않습니다.
`manual` 명령만 로컬 감사 파일에 운영자가 본 관측을 추가합니다.

## 설치

```bash
uv tool install git+https://github.com/mgh3326/scopefuel     # 또는
uvx --from git+https://github.com/mgh3326/scopefuel scopefuel
```

의존성 0(stdlib만)입니다. `scopefuel` / `sfuel` 두 이름으로 설치됩니다.

## 사용법

```bash
scopefuel                          # 표
scopefuel --brief                  # 한 줄 (pane/statusline/알림용)
scopefuel --brief --horizon now    # "지금 띄울 수 있나"만
scopefuel --json                   # 에이전트 계약 (schema=scopefuel.v1)
scopefuel --raw                    # provider 원본 응답
scopefuel --only claude,agy
scopefuel --exit-code-on crit      # 임계 이상이면 종료코드 2
scopefuel --watch 60               # 주기 재렌더 (herdr pane)
scopefuel --list-providers
```

캐시는 provider별 60초(`--cache-ttl`), 위치는 `~/.cache/scopefuel/snapshots.json`
(`SCOPEFUEL_CACHE`로 변경). 조회가 실패하면 **6시간 이내의 마지막 스냅샷으로 폴백하되 나이를 함께
표시**합니다 — 옛 값을 신선한 값처럼 보여주지 않는 것이 원칙입니다.

## manual — 로컬 수동 관측

자동 측정이 429, 파싱 오류, 전송 오류로 막혔을 때 화면에서 직접 본 값을 짧게 공급할 수 있습니다.

```bash
scopefuel manual set --pool claude --window 5h --used 18 \
  --measured-at now --resets-in 3h40m --reason "usage 화면 확인"
scopefuel manual set --pool claude --window 7d --used 77 \
  --measured-at now --resets-at 2026-09-25T00:00:00Z --reason "usage 화면 확인" --ttl 30m
scopefuel manual list
scopefuel manual clear --pool claude
```

- 저장소는 자동 snapshot과 분리된 `~/.cache/scopefuel/manual.json`이며 권한은 0600입니다.
  `SCOPEFUEL_CACHE`를 지정하면 같은 디렉터리의 `manual.json`을 사용합니다. 이력은 append-only이고
  clear도 삭제가 아니라 감사 이벤트입니다. 자동 snapshot 자체의 이름이 `manual.json`이면 충돌을
  피하기 위해 수동 저장소는 같은 디렉터리의 `manual-observations.json`을 사용합니다.
- 기본 TTL은 15분, 상한은 2시간입니다. 효력은 입력 시각이 아니라 `--measured-at`부터 세며 reset
  경계가 더 이르면 거기서 끝납니다. 오래된 화면을 늦게 입력하거나 같은 값을 다시 넣어도 수명이
  새로 시작되지 않습니다.
- `source=operator`는 인증된 사람 신원이 아닙니다. 저장 항목, `manual list`, `--json`, gate receipt에
  항상 **자기신고 · 미검증**으로 표시합니다. 부모 프로세스 pid와 이름, 짧은 조상 요약, stdin/stdout
  TTY 여부, 알려진 에이전트 환경 변수의 존재 여부만 기록하며 환경 변수 값은 기록하지 않습니다.
- 향후 hub 스키마와 맞추기 위한 `account_ref`와 `author_principal`도 로컬 host/pool 및 OS 사용자
  기반의 **미검증 placeholder**일 뿐입니다. hub가 승격할 때 인증된 계정 binding과 human identity로
  대체해야 하며, 로컬 값을 verified identity로 받아들이면 안 됩니다.
- 자동 측정이 fresh이면 자동 값이 우선합니다. 401/403, 자동 cutoff 초과 확정, 정책 exclude는 수동
  값으로 숨길 수 없습니다. 필수 bucket이 모두 있어야 하며 없는 5h나 daily 값을 0으로 합성하지 않습니다.
- 이 파일은 **현재 호스트에서만** 효력이 있습니다. 다른 기기로 전파되지 않으므로 각 기기에 별도로
  입력해야 합니다. 향후 계정별 hub 저장소가 이 스키마를 승격하기 전까지 중앙 인증이나 동기화를
  주장하지 않습니다.

## gate — 프로필 스폰 판정

```bash
scopefuel gate -m fable --operator-request hk:task/625        # exit 0=가능 / 3=차단 / 4=측정불가
scopefuel gate -m fable --operator-request hk:task/625 --gate-output f.json  # 감사 레코드(JSON)
```

`fable`은 급표(GRADE_TABLE) 밖의 운영자 명시 자문 전용 프로필입니다(ROB-591,
`CONSULT_ONLY_PROFILES`). `--recommend`의 후보·승급 후보에는 나오지 않습니다. `gate -m
fable`은 `policy launch`와 같은 consult_only 규칙을 적용합니다(#625): 유효한
`--operator-request` 없이는 거부되고, 있으면 쿼타·cutoff·provider 상태·policy
`exclude`를 검사한 뒤 통과 시 감사 필드를 남깁니다.

```bash
scopefuel gate -m fable --operator-request hk:task/625 --requested-by operator
```

escalation 프로필(GRADE_TABLE의 `gate="escalation"` 행, 예: `oc-omni`)은 같은 grade의 정상
후보가 하나라도 가용하면 차단됩니다. 운영자가 그 프로필을 명시 지정한 경우에만 아래 경로로 그
한 갈래를 건너뛸 수 있습니다.

```bash
scopefuel gate -m oc-omni --operator-request hk:task/461 --requested-by operator
```

- `--operator-request REF` — durable 참조만 받습니다: `hk:doc/<key>` 또는 `hk:task/<정수>`.
  자유 텍스트·경로 탐색·허용 문자 밖·초장 입력은 `operator_request_ref_invalid`로 거부합니다.
  escalation도 consult_only도 아닌 프로필에 주면 `operator_request_not_applicable`로
  거부합니다(무시하지 않음).
- 이 경로는 **감사 가능한 주장을 기록하는 경로이며 운영자 신원이나 동의를 증명하지 않습니다.**
  플래그를 넣는 주체가 에이전트일 수 있고 `--requested-by`도 자기신고(미지정 시 `unknown`)입니다.
  scopefuel은 REF를 해석하지 않으므로 기록은 항상 `ref_resolution=unverified`입니다.
- override가 적용돼도 "같은 grade 정상 대안 가용" 거부만 건너뜁니다. 측정불가·정책
  exclude·quota cutoff 등 나머지 검사는 그대로 적용되며 실패하면 여전히 차단됩니다.
- 통과 시 stdout 첫 줄과 `reason`, 그리고 `--gate-output` 레코드에
  `escalation_override`·`operator_request_ref`·`requested_by`·`ref_resolution`이 남습니다.
  이 필드가 override로 통과한 스폰을 세는 근거입니다.

### 필수 창과 미측정 (task #690)

각 pool은 `manual.REQUIRED_WINDOWS`로 판정에 필요한 한도 창 집합을 정합니다. grok는 주간
한도만 존재하므로 `{"7d"}`이 전부입니다 — 5h 칸의 `?`는 결함이 아니라 provider 특성입니다.
규칙: (1) 스냅샷에 없거나 값이 읽히지 않는 필수 창은 부족으로도 소진으로도 간주하지 않습니다 —
측정된 창만으로 판정하되, 통과·거부 사유에 `[필수 창 미측정: <창>]`로 이름을 남기고 감사
레코드의 `missing_windows` 필드에도 기록됩니다(요청 프로필이 조용히 떨어지거나 조용히
통과하지 않습니다). (2) 측정된 창 하나라도 cutoff를 넘으면 거부입니다 — 미측정 창이 있다고
소진 판정이 완화되지 않습니다. (3) stale 폴백 수용은 계속 fail-closed입니다 — 필수 창 커버리지가
갖춰지지 않은 stale 스냅샷은 수용되지 않으며, 거부 사유가 부족한 창을 지목합니다.

### E6 측정 런그와 arm 표식 (task #692)

E6(#594, `hk:doc plan/2026-09-25/e6-effort-ladder`)는 한 모델을 여러 effort로 실과제에서
비교합니다. 그 런그 중 표가 몰랐던 것은 이제 **카탈로그 행**으로 존재합니다 — 급 C·점수 없음
(`미측정`), `--recommend` 후보가 아니고 런처 기본값도 아닙니다. 배치가 아니라 측정 행이므로
`launch.snapshot_entries()`(배치 스냅샷)에는 들어가지 않고 `bench catalog list`/정본 시드에는
들어갑니다.

| profile | model | effort | pool | 급 |
|---|---|---|---|---|
| `sonnet` | claude-sonnet-5 | max | claude | C |
| `codex-sol` | gpt-6-sol | high | codex | C |
| `kimi-k3` | kimi-k3 | high | kimi | C |
| `kimi-k3` | kimi-k3 | max | kimi | C |
| `grok-hi` | grok-4.7 | xhigh | grok | C |

이 런그는 **명시적 arm 표식** 하나로만 열립니다 — 스포너가 스폰 명령에 붙이는 환경변수입니다.
wrk는 자신의 환경을 자식 `scopefuel` 호출에 그대로 넘기므로 카탈로그 조회와 쿼타 게이트가
같은 선언을 봅니다.

```bash
SCOPEFUEL_E6_ARM=sonnet@max wrk spawn -m sonnet --effort max ...   # E6 arm 스폰
scopefuel gate -m sonnet --effort max                              # 표식 없음 → exit 3, 런그 이름을 댄 사유
SCOPEFUEL_E6_ARM=sonnet@max scopefuel gate -m sonnet --effort max  # → exit 0, allow 라인에 [E6 arm, unmeasured C: sonnet@max]
```

- 표식 값은 `<profile>@<effort>`이며 effort 어휘는 소문자 폐쇄 집합입니다(`--effort`와 같은
  정규화). 별칭도 받습니다(`codex-max@high` = `codex-sol@high`).
- 표식이 그 런그를 가리키지 않으면 **아무것도 넓히지 못합니다** — 다른 프로필·다른 런그·
  형식 오류는 무시되고, C 급 E6 런그는 계속 거부됩니다(exit 3).
- 표식은 C 급 런그의 admission key이면서 **게이트가 판정할 런그를 지명하는 스포너 경로**입니다
  (wrk 는 `gate` 에 `--effort` 를 넘기지 않습니다). 그래서 arm B의 `opus@low` 같은 escalation
  런그는 표식으로 런그를 지명한 뒤 기존 `--operator-request` 경로로 엽니다 — 표식 자체가
  escalation 을 우회하지는 않습니다. 배치된 런그를 지명하면 판정이 그 런그로 **좁아질 뿐**
  넓어지지는 않습니다(예: `SCOPEFUEL_E6_ARM=opus@low gate -m opus` 는 low 런그의 escalation
  판정).
- 캐논이 런그를 retire 하면 표식으로도 열리지 않습니다 — 닫는 수단은 retire 또는 C 밖 배치입니다.
- 표식 없이 `policy launch`는 기존 폴백을 그대로 씁니다(`wrk -m codex`는 codex-sol@high를,
  `-m builder-grok`은 grok-hi@xhigh를 pin하므로 이 스펠링들은 변하지 않습니다). 캐논이 E6
  행을 싣고 있어도 표식 없는 기본값 계산은 C 행을 고르지 않습니다.
- `--recommend`는 어느 급에서도 E6 런그를 나열하지 않습니다. 캐논이 그 런그를 C 밖으로
  측정하면 제한은 자동으로 풀립니다(그때는 평범한 런그).
- 게이트의 `--effort`는 판정 대상을 런그로 좁힙니다 — `gate -m opus --effort low`는 low 런그
  (S, escalation)의 판정이고, 표가 모르는 런그는 프로필 기본 배치로 답합니다(기존 동작).
- 아직 wrk에 effort 경로가 없는 런그(`kimi-k3`)는 카탈로그 행이 먼저 서 있습니다. 표식은
  ambient 환경변수이므로 자식 스폰에 상속되면 그 스폰의 게이트 태그도 표식 런그를 가리킵니다 —
  arm 단위로만 설정하세요.

## Benchmark backend

벤치 점수와 대표 실행 기록의 backend는 `auto`(기본)·`handoffkeep`·`local` 중 하나입니다.
`auto`는 handoffkeep 엔드포인트 URL과 토큰이 **둘 다** 있고 그 URL이 베어러 토큰을 평문으로
싣지 않을 때만 `handoffkeep`으로, 아니면 `local`로 해석됩니다. 자격증명은 환경변수
(`HANDOFFKEEP_URL`/`HANDOFFKEEP_TOKEN`) 다음으로 handoffkeep CLI 자신의
`~/.config/handoffkeep/config.env`에서 찾습니다 — 이미 handoffkeep을 쓰는 호스트는 별도 설정
없이 서버 정본을 읽습니다. 개별 옵트아웃은 명시적 `backend = "local"`입니다.

```toml
[bench]
# backend = "auto"          # 기본값
cache_ttl_s = 21600         # scores/reps/grades 캐시 TTL
catalog_ttl_s = 3600        # 카탈로그는 1시간 — 배치·모델 id의 정본이라 수명이 다르다
catalog_stale_max_s = 86400 # 이 나이를 넘겨 서버가 불가하면 번들 스냅샷(= stale)
```

로컬 SQLite는 캐시가 되며, 읽기 실패는 캐시와 나이를 표시해 계속 동작하고, 쓰기 실패는
종료코드 2로 끝납니다. `scopefuel bench push-local`은 기존 로컬 점수·reps를 지우지 않고 한 번
이관할 때 사용합니다. 급 배치는 `scopefuel bench grades set`에서 deviation reference를 반드시
함께 남겨야 합니다.

### 정본 카탈로그 (#593)

모델 id·급 배치·pool·gate는 handoffkeep `bench_catalog`가 정본이고, `recommend.py GRADE_TABLE`은
오프라인 폴백 스냅샷입니다. 런처(`bin/wrk`)는 `scopefuel policy launch <profile>`로 모델 id와
기본 effort를 받아갑니다 — 프로필 철자와 argv 골격은 런처에 남습니다.

```console
$ scopefuel policy launch opus --json      # {model_id, effort, pool, gate, catalog}
$ scopefuel bench catalog status           # 이 호스트가 정본을 읽고 있는가, 아니면 왜 못 읽는가
$ scopefuel bench catalog list
$ scopefuel bench push-catalog --emit-seed --decided-by operator-desk > seed.json
$ scopefuel bench push-catalog seed.json   # 운영자 토큰 전용
```

서버 불가·stale은 "마지막으로 확인된 정본의 재현"까지만 허용합니다: `consult_only`는 절대 완화되지
않고, stale 상태의 비-`default` gate는 `--operator-request`를 요구합니다("서버 다운 ≠ 자유 배정").

정본이 아닌 출처는 세 가지로 구분해 표시하며, `stale`은 그중 하나뿐입니다 — 셋을 뭉뚱그리면
"서버가 죽었다"와 "이 호스트는 원래 서버를 안 본다"가 같은 경고가 되어 둘 다 무시됩니다.

| `catalog.source` | 뜻 | `stale` |
|---|---|---|
| `cache` | TTL 내 캐시, 또는 서버 불가지만 `catalog_stale_max_s` 이내 — 정본의 사본 | false |
| `unsupported` | 엔드포인트가 404 — 그 배포본에 카탈로그 라우트가 없다. `/v1/bench/grades` 투영으로 계속 | false |
| `snapshot` (backend=local) | 이 호스트는 정본을 읽도록 설정돼 있지 않다 | false |
| `snapshot` (backend=handoffkeep) | 읽어야 할 정본을 잃었다 | **true** |

호스트 전환 방법·실패 정책·머지 후 실행 절차는
[docs/catalog-server-mode.md](docs/catalog-server-mode.md)를 보세요.

## herdr 통합

`herdr-plugin.toml`이 포함되어 있어 그대로 설치할 수 있습니다.

```bash
herdr plugin install mgh3326/scopefuel     # 또는 로컬 개발 시
herdr plugin link ~/work/scopefuel
```

- 액션 `scopefuel.check` — 한 줄 요약 출력
- 페인 `scopefuel.gauge` — `--watch 60`으로 상시 계기판 (overlay)

에이전트 pane 이벤트(`pane.agent_detected`, `pane.agent_status_changed`, `pane.focused`)에서는 해당
pane의 agent/provider만 60초 debounce로 다시 확인해 표시 전용 metadata 토큰 `scopefuel_quota`를 보냅니다.
Herdr sidebar에서 이 토큰을 참조하면 다음처럼 pane별 라벨을 볼 수 있습니다.

```
claude·max · now 6% · wk 97% · [Fable 100%] · credential=default
```

이 경로는 pane title, agent state, 기존 action/overlay를 바꾸지 않습니다. 이벤트가 제공하는
`CLAUDE_CONFIG_DIR`/`CODEX_HOME`/`HOME` 위치는 짧은 익명 credential ID로만 구분하며 경로·토큰은 metadata에
쓰지 않습니다.

## 지원 provider

| id | 경로 | 얻는 것 | 제약 |
|---|---|---|---|
| `claude` | `~/.claude/.credentials.json` → `api.anthropic.com/api/oauth/usage` | 5h·7d 계정 한도 + 모델별(weekly_scoped) | 토큰 만료 시 claude 세션이 갱신해야 함 |
| `codex` | `~/.codex/auth.json` → `chatgpt.com/backend-api/wham/usage` | primary/secondary 창 + 모델 전용 버킷 | — |
| `agy` | 실행 중 `agy`의 로컬 language server → 실패 시 cloudcode-pa | 로컬=weekly+5h, 클라우드=5h만 | 모델별 분해 불가(아래) |
| `kiro` | `kiro-cli` 에 `/usage` 를 물려 출력 파싱 | 월 크레딧 1줄 (플랜+애드온 합산) | 5h급 창 없음, 호출 7초(아래) |
| `grok` | 인증된 `grok` CLI를 PTY로 실행해 `/usage` 출력 파싱 | 주간 account 한도 → used_pct/reset | 출력 형식 의존; 실패 시 월간 폴백 없이 degraded |
| `kimi` | `kimi` CLI를 PTY로 실행해 `/usage` 출력 파싱 | 5h·weekly account 한도 | CLI 출력 형식 의존; 429/rate-limit 재시도 없음 |
| `devin` | 인증된 `devin models list` 출력에서 SWE-2 행의 `Free` 태그만 파싱 | SWE-2 Free이면 account used_pct=0 | Free-only; SWE-2/Free 누락·다른 모델만 Free·출력/프로세스 실패는 unknown fail-closed. 추천표·gate 등재는 `devin-swe2`·`devin-glm52`·`devin-swe17`(Free)·`devin-ds41`(최저가 유료) 4종과 effort 변형 `devin-swe2-medium`·`devin-swe2-max`(Free)·`devin-ds41-max` 3종(#635; effort 는 모델 id 안에 있고, high 급을 상속하지 않는 C 미측정)이며 모두 같은 devin 계정 풀을 공유 |

**kiro는 API가 아니라 CLI를 읽습니다.** 같은 값을 주는 `GetUsageLimits` API가 있지만 토큰이
JSON 파일이 아니라 sqlite(`data.sqlite3`의 `auth_kv`)에 있고 만료 시 갱신이 필요합니다. 읽기 전용
계기판이 남의 토큰 저장소와 갱신 흐름까지 떠안는 것보다, 이미 인증을 끝낸 CLI에 물어보는 편이
경계가 깨끗합니다. 대가는 두 가지 — **출력 포맷이 바뀌면 파싱이 깨지고**(그때는 0을 채우지 않고
error로 보고합니다), **한 번에 7초쯤** 걸립니다(60초 캐시 전제). 액세스 토큰이 만료된 상태면
CLI 호출 자체가 갱신하므로 1회 재시도합니다.

`kiro`에는 **5시간급 창이 없습니다** — 월 크레딧 한 줄뿐이라 `--horizon now`에는 안 나옵니다.
리셋은 CLI가 날짜만 주므로 로컬 자정으로 표시합니다. 플랜 크레딧이 다 차도 애드온이 남아 있으면
작업은 계속되므로, 두 풀이 있으면 **합산 한 줄**만 account로 냅니다(각각 내면 "계정 차단"으로 오독합니다).

**kimi도 API가 아니라 CLI를 읽습니다.** scopefuel은 인증 파일이나 endpoint를 직접 읽지 않고
이미 인증된 `kimi` 프로세스의 PTY에 `/usage`를 한 번 보낸 뒤 `Weekly`·`5h`의 `N% left`를
`used_pct`로 변환합니다. 출력이 바뀌거나 rate limit/429가 나오면 추측·재시도하지 않고 error로
보고합니다.

**devin도 API가 아니라 CLI 모델 목록만 읽습니다.** 소스는 `devin models list`의 SWE-2 패밀리 행뿐이며, 그 행에 `Free` 태그가 있을 때만 account 버킷 `used_pct=0`(note `free until ~2026-10-10`)을 냅니다. SWE-2 또는 Free가 없거나, SWE-1.x·GLM 등 다른 모델만 Free이거나, Fusion 이름에 SWE-2가 섞여 있거나, 출력을 해석할 수 없거나, binary/프로세스가 실패하면 사용률을 추정하지 않고 error/unknown으로 fail-closed 합니다. 추천표·gate 등재는 `devin-swe2`·`devin-glm52`·`devin-swe17`(Free)·`devin-ds41`(최저가 유료) 4종과 effort 변형 `devin-swe2-medium`·`devin-swe2-max`(Free)·`devin-ds41-max` 3종(#635; effort 는 모델 id 안에 있고, high 급을 상속하지 않는 C 미측정)이며 모두 같은 devin 계정 풀을 공유합니다. 그 외 유료 Devin 모델(Fusion/Claude/OpenAI/Gemini 등)은 추천표·gate 등재 범위 밖입니다. credential/config 파일은 읽지 않습니다.

**agy 모델별 분해는 불가능합니다.** 클라우드 응답은 모델 이름별 행을 주지만 값이 그룹 공유입니다
(gemini 계열 전부 동일 fraction, claude/gpt-oss 전부 동일 fraction — 로컬 그룹값과 일치).
`GetCommandModelConfigs`는 CLI에서 501, `GetCascadeModelConfigs`는 빈 응답, `RetrieveUserQuota`는 404입니다.
모델별 단가를 알고 싶으면 작업 전후로 빼는 수밖에 없습니다.

## 새 provider 추가

대부분은 **TOML 한 장**으로 끝납니다 — 코드도, 릴리스도 필요 없습니다.

```bash
mkdir -p ~/.config/scopefuel/providers
$EDITOR ~/.config/scopefuel/providers/myplan.toml
scopefuel --only myplan
```

포맷과 예제는 [docs/adding-a-provider.md](docs/adding-a-provider.md)를 보세요. 프로세스 탐색·OAuth
갱신·다단계 호출처럼 스펙의 틀을 벗어나는 provider는 Python entry-point 플러그인으로 붙입니다.
같은 `id`를 정의하면 **선언형 스펙이 내장 provider를 완전 대체**합니다 — 엔드포인트가 깨졌을 때 릴리스를
기다리지 않고 사용자가 직접 고칠 수 있게 한 의도적 설계입니다.

**grok 사용량 소스와 한계.** 웹 화면과 일치하는 주간 게이트는 인증된 `grok` CLI의
대화형 `/usage` 출력에서 `Weekly limit: N%`와 `Next reset: Month D, HH:MM`을 읽는다.
세션을 시작하지 않아도 이 요약이 출력되므로 토큰 비용은 없다. 연도 없는 리셋은 현재 시각을
기준으로 이미 지난 날짜만 다음 해로 추론하고, CLI가 KST로 렌더한 시각을 KST로 해석한다.
출력·PTY·타임아웃·파싱에 실패하면 월간 크레딧으로 조용히 대체하지 않고 degraded로 보고한다.

## provider class — preserve vs spend

provider는 운영 의도에 따라 두 class로 구분됩니다.

- **preserve** (기본): 75%/90% 사용률을 WARN/CRIT로 승격. `claude`, `codex`.
- **spend**: 고사용을 정상으로 본다. reset 전 24시간 미만, 70% 미만 bucket이 있으면
  **WASTE** 권고를 낸다. `kiro`, `clinepass`, `agy`, `grok`, `kimi`, `devin`.

선언형 TOML 스펙에서 `class = "preserve" | "spend"`로 지정할 수 있으며(같은 `id`로 내장 provider를 대체할 때도 전체 대체 스펙에 `class` 지정 가능), Python 플러그인 메타데이터 또는 반환 `ProviderResult.pool_class`로도 설정할 수 있습니다. 자세한 내용은 [docs/adding-a-provider.md](docs/adding-a-provider.md)를 보세요.

### 두 축: `mark`와 `usage_mark`

`mark`는 사람이 먼저 보아야 할 provider 판정입니다. 조회 실패·stale이면 사용률이 높아도 `degraded`가 우선합니다. `usage_mark`는 scope와 provider class를 적용한 별도 사용률 판정(`ok|warn|crit`)입니다. 예를 들어 마지막으로 알려진 preserve 사용률이 97%인 stale 결과는 `status=stale`, `mark=degraded`, `usage_mark=crit`로 두 진실을 함께 냅니다.

`summary.mark`와 `summary.usage_mark`는 각각 두 축의 전체 판정입니다. `--exit-code-on LEVEL`은 어느 축이든 LEVEL 이상이면 종료코드 2를, 그렇지 않은 실제 provider error는 1을, 나머지는 0을 반환합니다. WASTE는 informational이며 종료코드를 올리지 않습니다.

`scopefuel.v1` JSON에는 호환 가능한 필드가 추가될 수 있으므로 소비자는 알 수 없는 필드를 무시해야 합니다. 기존 `mark`의 의미·타입은 유지됩니다. raw `used_pct`, reset, pace는 그대로 보존하며, `--exit-code-on`과 전체 mark는 failure/warning/stale 우선 순위를 유지합니다.

## 주의

여기서 쓰는 엔드포인트 중 일부는 **공식 문서화된 API가 아닙니다**(`wham/usage`,
`RetrieveUserQuotaSummary`, `v1internal:fetchAvailableModels`). 제공사가 예고 없이 바꿀 수 있고,
그때는 해당 provider가 조용히 실패하는 대신 오류와 힌트를 표시합니다. 자격증명은 각 CLI가 이미
로컬에 저장한 파일을 **읽기만** 하며, 어디에도 전송하지 않습니다(해당 제공사 자신의 API 제외).

## 라이선스

MIT. Antigravity 클라우드 경로(`loadCodeAssist` → `fetchAvailableModels`)는
[cclimits](https://github.com/cruzanstx/cclimits)(MIT)의 구현을 참고했습니다.
