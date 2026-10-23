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

codex는 이 규칙의 유일한 plan 분기입니다 (hk 1769). `manual.CODEX_PLAN_REQUIRED_WINDOWS`가
provider가 저장한 plan_type 별 필수 창을 정합니다 — 2026-10-20 wham usage API 증거(hk 1767
리포트)로 plus는 `{5h, 7d}`, prolite는 `{7d}`만, free는 30일 창 하나(model.window_label의
`30d` 밴드, ~2592000초)입니다. 표에 있는 plan이 필수 창 하나라도 유효 bucket으로 커버하지
못하면 측정된 나머지 창으로 통과하지 않고 `codex_required_window_missing:<창> plan=<plan>`
사유로 측정 불가(gate rc 4, recommend 제외)입니다 — plus 계정의 5h 부재를 7d 사용률로 흘리지
않기 위해서입니다. 표에 없는 plan(None·pro·team 등)은 기존과 바이트 동일하게 `{5h, 7d}` +
미측정 태그로 판정하고, 표를 확장하도록 한 번 경고합니다. 다른 provider는 모두 기존 집합
그대로입니다.

### E6 측정 런그와 arm 표식 (task #692, grok·sol-medium 런그 #737)

E6(#594, `hk:doc plan/2026-09-25/e6-effort-ladder`)는 한 모델을 여러 effort로 실과제에서
비교합니다. 그 런그 중 표가 몰랐던 것은 이제 **카탈로그 행**으로 존재합니다 — carrier 급(sonnet@max 는 A+, 나머지는 C)·점수 없음
(`미측정`), `--recommend` 후보가 아니고 런처 기본값도 아닙니다. 배치가 아니라 측정 행이므로
`launch.snapshot_entries()`(배치 스냅샷)에는 들어가지 않고 `bench catalog list`/정본 시드에는
들어갑니다.

| profile | model | effort | pool | carrier 급 |
|---|---|---|---|---|
| `codex-sol` | gpt-6.1-sol | high | codex | C |
| `codex-sol` | gpt-6.1-sol | medium | codex | C |
| `codex-sol` | gpt-6.1-sol | low | codex | C |
| `kimi-k3` | kimi-k3 | high | kimi | C |
| `kimi-k3` | kimi-k3 | max | kimi | C |
| `grok-hi` | grok-4.7 | low | grok | C |
| `grok-hi` | grok-4.7 | medium | grok | C |
| `haiku` | claude-haiku-5-5 | xhigh | claude | C |
| `haiku` | claude-haiku-5-5 | max | claude | C |
| `sonnet` | claude-sonnet-5-5 | max | claude | A+ |

carrier 급은 그 프로필의 effort 사다리를 handoffkeep 카탈로그 단조성 검사에 맞게 유지하는 가장
약한 급입니다. 그래서 `sonnet@xhigh`가 A+이므로 `sonnet@max`는 A+를 싣고, 나머지 런그는 C를 싣습니다.

이 런그는 **명시적 arm 표식** 하나로만 열립니다 — 스포너가 스폰 명령에 붙이는 환경변수입니다.
wrk는 자신의 환경을 자식 `scopefuel` 호출에 그대로 넘기므로 카탈로그 조회와 쿼타 게이트가
같은 선언을 봅니다.

```bash
SCOPEFUEL_E6_ARM=codex-sol@high wrk spawn -m codex-sol --effort high ...   # E6 arm 스폰
scopefuel gate -m codex-sol --effort high                              # 표식 없음 → exit 3, 런그 이름을 댄 사유
SCOPEFUEL_E6_ARM=codex-sol@high scopefuel gate -m codex-sol --effort high  # → exit 0, allow 라인에 [E6 arm, unmeasured C: codex-sol@high]
# sonnet@max 런그의 태그는 carrier 급을 댑니다                          → [E6 arm, unmeasured A+: sonnet@max]
```

- 표식 값은 `<profile>@<effort>`이며 effort 어휘는 소문자 폐쇄 집합입니다(`--effort`와 같은
  정규화). 별칭도 받습니다(`codex-max@high` = `codex-sol@high`).
- 표식이 그 런그를 가리키지 않으면 **아무것도 넓히지 못합니다** — 다른 프로필·다른 런그·
  형식 오류는 무시되고, carrier 급 E6 런그는 계속 거부됩니다(exit 3).
- 표식은 carrier 급 런그의 admission key이면서 **게이트가 판정할 런그를 지명하는 스포너 경로**입니다
  (wrk 는 `gate` 에 `--effort` 를 넘기지 않습니다). 그래서 `sonnet@xhigh` 같은 escalation
  런그는 표식으로 런그를 지명한 뒤 기존 `--operator-request` 경로로 엽니다 — 표식 자체가
  escalation 을 우회하지는 않습니다. 배치된 런그를 지명하면 판정이 그 런그로 **좁아질 뿐**
  넓어지지는 않습니다(예: `SCOPEFUEL_E6_ARM=sonnet@xhigh gate -m sonnet` 는 xhigh 런그의
  escalation 판정).
- 캐논이 런그를 retire 하면 표식으로도 열리지 않습니다 — 닫는 수단은 retire 또는 carrier 밖 배치입니다.
- 표식 없이 `policy launch`는 기존 폴백을 그대로 씁니다(`wrk -m codex`는 codex-sol@high를,
  `-m builder-grok`은 grok-hi@xhigh를 pin하므로 이 스펠링들은 변하지 않습니다). 캐논이 E6
  행을 싣고 있어도 표식 없는 기본값 계산은 carrier 행을 고르지 않습니다.
- `--recommend`는 어느 급에서도 E6 런그를 나열하지 않습니다. 캐논이 그 런그를 carrier 밖으로
  측정하면 제한은 자동으로 풀립니다(그때는 평범한 런그).
- 게이트의 `--effort`는 판정 대상을 런그로 좁힙니다 — `gate -m opus --effort low`는 low 런그
  (S)의 판정입니다. #716: **배치 행을 가리키는 `--effort`는 그 런그를 배치로 판정합니다**
  — 측정불가·제외·cutoff 등 `--effort` 없는 경로와 같은 쿼타 규칙이며, escalation 의
  "같은 grade 정상 대안 거부"는 적용하지 않습니다(그 거부는 기본 배치 해석과 `--recommend`
  사다리의 개념이라, 호출자가 지명한 런그에는 답이 아닙니다). escalation 행임은 사유 줄의
  `[rung <profile>@<effort>, escalation-gated]` 태그에 남습니다. 표가 모르는 런그는 프로필
  기본 배치로 답합니다(기존 동작). 별칭은 순수한 이름 바꿔쓰기입니다 — `gate -m codex-max
  --effort xhigh`는 `codex-sol@xhigh`를 판정하고, `gate -m codex-max --effort high`는
  미측정 C 런그 `codex-sol@high`에 걸려 런그와 remedy 를 댄 거부(exit 3)입니다.
  ambient 표식과 달리 `--effort`는 명령 단위의 명시 지명이므로 이 계약이 성립합니다 —
  표식만으로 골라진 배치 런그는 위 bullet 대로 그 행 자체의 게이트를 따릅니다.
- 아직 wrk에 effort 경로가 없는 런그(`kimi-k3`)는 카탈로그 행이 먼저 서 있습니다. 표식은
  ambient 환경변수이므로 자식 스폰에 상속되면 그 스폰의 게이트 태그도 표식 런그를 가리킵니다 —
  arm 단위로만 설정하세요.

### 구독 해지 — subscribed 플래그 (task #742)

구독이 끝난 플랜·프로필은 행을 지우지 않고 플래그로 표시합니다 — 카탈로그 행·reps·급
이력은 그대로 남고, 추천과 게이트만 닫힙니다. 플래그를 되돌리면 남아 있던 이력 그대로
복원됩니다. 플래그는 **config**(정본 카탈로그가 아님)에 둡니다:

```toml
[pools.kiro]
subscribed = false        # 그 풀의 모든 프로필이 구독 해지

[profiles.kiro-opus]
subscribed = true         # 프로필 수준 오버라이드 — 풀 플래그보다 우선
```

우선순위(구체적인 문장이 이깁니다): `profiles.<canonical>` > `profiles.<요청 철자>` >
같은 엔터티의 다른 별칭 키 > `pools.<pool>` > 기본값(구독). `true`/`false` 둘 다 명시
오버라이드입니다 — 풀이 꺼진 상태에서 프로필을 `true`로 켜거나, 풀이 켜진 상태에서
프로필을 `false`로 끌 수 있습니다. 키가 없으면 의견 없음(한 수준 아래로 폴백).
bool이 아닌 값은 무효로 무시하고 다음 수준이 판정하며 `policy list`에 사유가 남습니다.
설정을 아무것도 안 하면 기본값 그대로입니다 — 배포 기본값은 아무것도 플래그하지 않습니다.

CLI 표면:

```bash
scopefuel policy set kiro --subscribed off          # [pools.kiro] subscribed=false
scopefuel policy set kiro --subscribed on           # true
scopefuel policy set kiro --subscribed none         # 키만 제거
scopefuel policy profile kiro-opus off              # [profiles.kiro-opus] subscribed=false
scopefuel policy profile kiro-opus on               # 풀이 꺼져도 이 프로필만 복원
scopefuel policy profile kiro-opus clear            # 프로필 키 제거 → 풀 수준 폴백
scopefuel policy set codex --fusion-billing-fp <fp> # hk 1753: Fusion GPT 청구 계정 지문 선언
scopefuel policy list                               # 풀 행의 [unsubscribed] + profiles: 절
```

행동 계약:

- `--recommend`는 구독 해지 프로필을 후보·승급 후보·비상 후보(policy_excluded)·교차급
  실측 대안 어느 경로로도 나열하지 않습니다. 행 자체는 급표에 남아 있으므로
  `✗ <pool> 풀 구독 해지 — 이 급에서 N개(...)` fold 줄로 표시합니다.
- `scopefuel gate -m <p>`는 구독 해지를 **전용 exit 6**으로 거부합니다 — 쿼타 차단(3)·
  측정불가(4)·역할 거부(5)와 다른 코드이며, 쿼타보다 먼저 판정하므로 "reset 을 기다리면
  풀리는" 답으로 섞이지 않습니다. 사유는 결정한 config 키(`[pools.x]`/`[profiles.x]`)를
  이름붙이고, `--gate-output` 레코드에 `"unsubscribed": true`가 남습니다.
- `policy launch`는 같은 플래그로 거부합니다(rc 3, "canon says no" 경로) —
  별칭·effort 런그·E6 arm·`--operator-request` 어떤 경로도 구독 해지를 열지 못합니다.
- 상태 표·`--list-recommend-profiles`·`bench catalog list`는 행을 유지한 채
  `[unsubscribed]`/`unsubscribed` 표식을 붙여 보입니다.
- `grades propose`/`grades apply`·`reps`는 구독 해지 런그를 평소대로 평가·갱신합니다
  (제안·보류·런그 상세 줄에 `[unsubscribed]` 태그만 추가). 플래그는 이력을 지우거나
  평가를 건너뛰지 않습니다 — 되돌리면 끊기지 않은 기록으로 이어집니다.

### in-flight 예약 — 공유 쿼타 풀의 진행 중 잡 합산 (hk 1787)

arbiter의 `quota_pool` 행은 예약이 아니라 **기록**입니다. hk 1767에서 게이트가 스냅샷 한
장과 프로필 하나만 보고 codex 잡 3개를 연달아 통과시켰고, 그 셋이 약 42분 동안 겹쳐 돌았습니다.
같은 계정을 쓰는 호스트는 각자 자기 arbiter `state.db`를 갖기 때문에, 로컬 카운트만으로는 다른
호스트에서 도는 잡을 볼 수 없습니다. 합산은 **account_fp 단위, 호스트 횡단**입니다.

```toml
[pools.codex.inflight_reserve_pct]
5h = 12    # in-flight 잡 1개당 5h 창에 12%p 예약
7d = 2
```

- **판정 규칙**: `[pools.<pool>] inflight_reserve_pct`가 설정돼 있으면 게이트는
  n = (로컬 arbiter live 레코드 수) + (같은 account_fp 아래 타 호스트의 집계 잡 수)를 구하고,
  예약이 걸린 필수 창 w마다 `used_pct(w)` 대신 `used_pct(w) + reserve(w) * n`으로 cutoff를
  비교합니다. 보정값이 cutoff를 넘고 원값이 넘지 않으면 rc 3으로 거부합니다:
  `codex inflight_headroom window=5h used=41 n=3 reserve=12 cutoff=90`. 예약이 없는 창은
  기존 비교 그대로입니다. 값을 잘못 쓰면(비수치·음수·범위 밖) 그 창만 미설정으로 폴백합니다.
- **비활성 계약**: 키가 없거나 모든 값이 0이면 arbiter 호출·hk 요청·게시가 전부 0이고 출력은
  base와 바이트 단위로 동일합니다.
- **로컬 집합**: `arbiter status --json`(`ARBITER_BIN` → PATH 순서, 5초 상한)의
  `quota_pool_records`에서 판정 풀의 행만 셉니다. 로컬 행도 원격 문서와 같은 8시간
  (`INFLIGHT_MAX_AGE_S`) 나이 규칙을 따릅니다 — release되지 않은 채 남는 arbiter 행이
  합계를 영구히 부풀리지 않도록(hk 1797). 지금 판정 중인 잡은 아직 레코드가 없어
  자기 자신을 세지 않습니다.
- **타 호스트 집계**: 로컬 집합을 읽은 게이트는 `quota/<pool>/<account_fp>/inflight/<host>`에
  자기 live 목록을 게시합니다(비어 있어도 게시 — release가 다음 게이트에서 지워지게).
  읽을 때는 자기 호스트 문서를 빼고, 고정 스키마·pool/account_fp 일치·`published_at`이
  `MAX_FUTURE_SKEW_S`(60s) 이내 미래·각 잡의 `started_at`이 `INFLIGHT_MAX_AGE_S`(8h) 이내인
  잡만 세며, 잡 id는 문서 간 중복 제거합니다. 문서 자체의 나이로는 버리지 않습니다 —
  release 후 게이트를 안 돌린 호스트는 그 잡을 계속 세게 두는 쪽(과대계산)이 안전하기 때문입니다.
- **fail-open 태그**: 로컬 읽기나 원격 목록이 실패하면(arbiter 없음·타임아웃·JSON
  깨짐·hk 불통) 판정은 측정된 값 그대로입니다. 사유에 `inflight=unknown(local)` 또는
  `inflight=unknown(remote)`을 붙이고, 결정 줄 뒤 hk 1769 unknown-plan 경고 다음 위치에
  경고 한 줄을 인쇄합니다. 카운트가 미확정이라는 이유만으로 거부하지 않으며, 부분 결과
  (로컬 성공·원격 실패)는 로컬 n을 그대로 적용합니다. 통과 사유에는 `inflight=N`만 붙습니다.
- **관측 명령**: `scopefuel inflight show --pool P`가 로컬 집합과 각 원격 문서
  (호스트·published_at·counted/dropped 잡과 사유), account_fp별 n을 보여줍니다.
  읽기 전용 — 절대 게시하지 않습니다.
- **활성화**: 설정은 unset으로 배포됩니다. 켜는 명령은
  `scopefuel policy set codex --inflight-reserve 5h=12 --inflight-reserve 7d=2`
  (`none`으로 테이블 전체 제거)입니다.

알려진 한계(수정하지 않음):

- 두 호스트가 같은 순간 게이트를 돌리면 둘 다 통과할 수 있습니다 — 예약이 아니라 합산이므로.
- `quota_pool` 레코드가 release됐는데 pane이 살아 있는 잡은 세지 않습니다. hk 1767이 본
  sentinel lost-grace 경로의 release가 그 경우이며, hk 1770이 그 경로를 좁혔지만 없애지는
  않았습니다.
- m1의 prolite는 account_fp가 다르므로 자기만의 합계를 갖습니다 — 한계가 아니라 의도된
  분리입니다.



`--recommend G`는 G 행만이 아니라, **reps 실측으로 정확히 한 급 위(G 바로 위)에 배치된
행**도 함께 나열합니다. 급의 근거가 측정된 reps(또는 reps 근거의 운영자 급 결정)인
행만 해당합니다 — `급 실측(<grade>; …reps…)` 주석이 그 표식입니다. 추정만 있는 행,
AA-agent 점수만 있는 행, 미측정 행(E6 측정 런그 포함)은 아래로 내려오지 않습니다.

- 표식: 순위 줄 끝에 `[one-up <배치 급>]` 태그. `--json`/`recommend_dict`는 각 행에
  `placed_grade`와 `one_up: true` 필드를 실어 보냅니다.
- 순서: `--recommend` 순위는 정급·one-up 행을 가리지 않는 하나의 정렬 키
  (`_recommend_rank_key`)를 따릅니다 — 미측정 행이 마지막, 그다음 쿼타·부스트
  차원(소진 임박 → 부스트 유무·값 → 점수), 그다음 같은 풀 안의 과금 차원(paid
  행은 free·unknown 행 아래로 가라앉습니다, task #1340 — `billing`은 per-row
  datum이고 `unknown`은 `free`와 같은 중립값이라 표식 없는 행은 움직이지
  않습니다), 그다음 가성비 슬롯, 풀 내 순위입니다. 정급(exact) 행이 one-up 행보다
  앞서는 것은 이 차원 전부가 동률인 **완전 동률에서만** 적용되는 마지막
  타이브레이크이고, 키의 최후 요소는 호출 측 입력 순서의 안정 슬롯입니다 —
  쿼타나 가성비가 실제로 더 나은 상위급 행을 급 내 하위 행 아래로 숨기지
  않는다는 의도입니다.
- 제외 규칙은 정급 행과 동일합니다 — 구독 해지·정책 제외·소진된 one-up 행은 같은
  fold 줄로 표시되고 이름에 태그가 붙습니다. 후보가 하나도 없을 때만 보이는
  비상 후보 블록에서도 태그를 유지합니다.
- `S+` 위에는 급이 없으므로 `S+`에는 one-up 행이 없습니다. `S`는 S+ 행 중
  reps-실측만 받습니다(현재 번들 카탈로그 기준 해당 없음). Sol 프로필은 어떤
  경로로도 S+ 밖에 나타나지 않는 규칙이 그대로 적용됩니다.
- `gate`는 이 목록을 읽지 않습니다 — 급 배치·풀 쿼타로 스스로 판정합니다.
  추천 출력을 읽는 유일한 소비자는 gate의 동일-급 대안 나열이며, one-up 행은
  그 목록에서도 걸러집니다.

### borrowed 행 — 운영자 지정 런그의 한 급 아래 표시 (d1786ap, hk 1795)

`recommend.BORROWED_RUNGS`는 운영자 지시(d1786ap, hk:task/1786)로 지정된 두
런그 — `opus@low`(A+에 표시)와 `codex-sol@xhigh`(S에 표시) — 를 배치 급의
정확히 한 급 아래에도 나열합니다. 한 행이 두 급에 앉는 것(d1786dup)은 카탈로그가
(profile, effort) 당 급 하나만 담을 수 있어서 표기 불가하므로, 배치가 아니라
**표시 나열**로 표현합니다. 표 형식·순서 규칙은 one-up 행과 같습니다(순위 줄
끝 `[borrowed <배치 급>]` 태그, JSON은 `borrowed: true` + `placed_grade`) —
정급 행이 borrowed 행보다 앞서는 것도 마지막 완전 동률 타이브레이크뿐입니다.

- 활성 조건(규칙 2): 런타임 표(canon 또는 스냅샷)가 그 런그를 나열 급의 정확히
  한 급 위에 둘 때만 표시합니다. 다른 곳에 두거나 아예 싣지 않으면(이동·은퇴·
  carrier 의 E6 행처럼 표에서 빠진 경우) 그 항목은 inert — 아무것도 나열하지
  않습니다. 이는 나중의 canon 이동 뒤에도 stale 지시가 후보를 지어내지 못하게
  합니다.
- 게이트 유지(규칙 4): borrowed 행은 자기 게이트를 그대로 가져갑니다.
  `codex-sol@xhigh`는 escalation 태그를 달고 승급 후보 섹션에 표시되며, 실행은
  계속 `--operator-request` 가 필요합니다(desk 가 그 행의 게이트를 바꾸기
  전까지). one-up 의 Sol 배제·`validate_grade_table` 은 *배치* 규칙이라 표시
  나열에는 적용되지 않습니다 — BORROWED_RUNGS 는 그 둘의 명시적 운영자 예외이며,
  같은 행이 one-up 과 borrowed 양쪽 자격이면 borrowed 로 한 번만 나열합니다.

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

handoffkeep URL이 `http`이고 `localhost`가 아닐 때(예: 사설 터널 위 Tailscale
엔드포인트) 베어러 토큰은 용도별 옵트인을 켠 경로로만 나갑니다 — 각각 기본값
`false`이고, https는 이 플래그와 무관하게 항상 허용됩니다.

```toml
[bench]
allow_plaintext_catalog = true      # scores/grades/catalog 정본 읽기·쓰기
allow_plaintext_quota_share = true  # 쿼터 스냅샷 publish/read (/v1/documents)
allow_plaintext_reps = true         # reps add/list
```

세 옵트인은 독립적입니다 — quota share·reps만 켜도 카탈로그 출처는 로컬에
머뭅니다(서버 카탈로그로의 의도치 않은 전환, 즉 #667 회귀가 불가능). 예전 단일
플래그 `allow_plaintext_url`은 세 용도 전부를 켜는 deprecated 별칭으로 계속
동작하며, 켜져 있으면 경고 한 줄을 출력합니다.

### Cloudflare Access 뒤의 handoffkeep (task #1280)

handoffkeep 엔드포인트가 Cloudflare Access 뒤에 있으면 서비스 토큰 두 키를 설정합니다.
둘 다 있으면 모든 handoffkeep 요청(catalog·scores·grades·reps·quota share·push-catalog)에
`CF-Access-Client-Id`/`CF-Access-Client-Secret` 헤더가 실립니다. provider 호출(claude·codex·agy·
clinepass·Artificial Analysis 등)에는 절대 실리지 않습니다.

```sh
# 환경변수, 또는 ~/.config/handoffkeep/config.env (HANDOFFKEEP_CONFIG 로 경로 지정)
HANDOFFKEEP_CF_ACCESS_CLIENT_ID=<service token client id>
HANDOFFKEEP_CF_ACCESS_CLIENT_SECRET=<service token client secret>
```

- 읽는 순서는 URL/TOKEN과 같습니다: 키마다 환경변수가 먼저, 다음이 같은 config.env.
- 값의 앞뒤 공백은 잘라내고, 공백뿐인 값은 없는 것으로 봅니다. 둘 중 하나만 있으면 빠진 키 이름을
  말하는 설정 오류이며(값은 출력하지 않음), 요청은 보내지 않습니다. 값은 HTTP 헤더에 실을 수 있는
  보이는 ASCII(0x21–0x7E, 공백·제어문자·비ASCII 없음)여야 하며, 아니면 키 이름만 말하는 설정
  오류입니다.
- 키가 없으면 동작은 이전과 같고, 모든 handoffkeep 요청에 `User-Agent: scopefuel/<version>`이
  붙는 것만 다릅니다.
- redirect는 같은 origin(scheme·host·port; host는 대소문자 무시·끝 점 무시) 안에서만 따라갑니다.
  scheme 변경은 http→https 승격까지 포함해 거부합니다. `*.cloudflareaccess.com` 로그인으로의
  redirect는 `cf_access_login_redirect`, JSON 호출에 온 2xx HTML은 `unexpected_html_response`
  오류가 되며, 둘 다 "서비스 토큰이 없거나 허용되지 않음"을 알립니다.
- 토큰·CF id·secret·Location 값은 어떤 오류 메시지·경고·상태 출력에도 나오지 않습니다.
  `scopefuel bench catalog status`는 `cf_access=configured|absent`만 보여 줍니다(아래 격리 규칙의
  안내 문구 포함).

**격리 규칙.** `HANDOFFKEEP_URL` 또는 `HANDOFFKEEP_TOKEN`이 환경변수에서 오는 동안(공백뿐인 값은
설정 안 된 것으로 봄)에는 config.env에 저장된 CF 키를 쓰지 않습니다 — 저장된 토큰을 쓰지 않는 것과
같은 규칙으로, 환경변수로 다른 호스트를 가리켜도 저장된 CF secret이 그쪽으로 가지 않습니다(CWE-522).
이 경우 CF 키는 환경변수로만 줄 수 있고, 환경변수에 id만 있고 secret은 config.env에만 있으면 빠진
키를 말하는 설정 오류입니다. 규칙이 config.env의 CF 키를 건너뛰면 조용히 넘어가지 않고
`bench catalog status`와 첫 handoffkeep 요청(stderr, 한 번)이
`cf_access=absent (config.env CF keys ignored because HANDOFFKEEP_URL/TOKEN come from the environment)`
라고 알립니다(값은 출력하지 않음).

로컬 SQLite는 캐시가 되며, 읽기 실패는 캐시와 나이를 표시해 계속 동작하고, 쓰기 실패는
종료코드 2로 끝납니다. `scopefuel bench push-local`은 기존 로컬 점수·reps를 지우지 않고 한 번
이관할 때 사용합니다. 급 배치는 `scopefuel bench grades set`에서 deviation reference를 반드시
함께 남겨야 합니다.

### reps id와 쓰기 규칙 (task #1384)

handoffkeep의 rep upsert 키는 `(created_by, origin_id)`이고 `created_by`는 토큰에서 오므로,
토큰을 공유하는 호스트들은 같은 키 공간을 씁니다. 그래서 `reps add`의 `origin_id`는
`2^62 + secrets.randbits(62)` — 예약된 밴드 `[2^62, 2^63)`의 난수 — 입니다(레거시 작은 id·
migrate 밴드 `[2^40, 2^40+2^48)`와 겹치지 않음). `reps list`의 `id=`는 항상 저장소 네임스페이스를
붙입니다.

- `srv:<id>` — 서버가 확정한 행의 기본키.
- `origin:<id>` — 서버 pk가 아직 확인되지 않은 로컬 rep의 클라이언트 측 멱등 키.
- `lost:<id>` — 서버가 더 이상 인정하지 않는 로컬 사본(아래 shadow 규칙). `shadowed-by=srv:N`이
  함께 표시됩니다.
- `local:<id>` — local backend의 로컬 `reps` rowid.

쓰기는 write-ahead입니다: PUT 전에 대기 rep(origin_id + 전체 페이로드, 서버 id 없음)이 로컬
`bench_pending_reps`에 남고, 실패·타임아웃 후 재시도는 **같은 origin_id와 같은 페이로드**를 다시
보냅니다(새 id를 만들지 않음) — 서버의 identical-resend 규칙이 같은 서버 id를 돌려줍니다.
`reps list`는 대기 rep을 `origin:<id> [pending]`과 reason으로 보여 주고 srv id는 붙이지 않습니다.
서버가 확인하지 못한 쓰기는 새로고침 후에도 대기 행으로 남습니다. `reps compare`는 handoffkeep
모드에서 서버가 확인한 행만 집계하고, pending/refused/lost 및 익명 캐시 행은 집계하지 않습니다.

새 서버는 PUT 응답에 `ids`(입력 순서)를 실어 보내므로 그 id로 확인합니다. 이후 GET이 그 id의
내용을 부인하면 pending으로 남깁니다. `ids`를 안 보내는
옛 서버에서만 쓰기 후 GET으로 바인딩하며, 그 바인딩은 fail-closed입니다 — `origin_id`를 유일하게,
그리고 동일한 내용으로 쥔 서버 행이 증명될 때만 `srv:`를 붙이고, 아니면 `origin:`로 두고 경고합니다.
남의 srv id는 절대 출력하지 않습니다.

충돌 — 새 서버가 같은 `(created_by, origin_id)` 슬롯에 다른 rep이 있음을 발견하면 PUT 전체가
409 `bench_rep_conflict`로 거부되고, 클라이언트는 origin id와 `srv:<conflict_server_id>`를 함께
출력하며(서버 행을 읽을 수 있으면 다른 필드명까지) 종료코드 2로 끝납니다. 새 id로의 자동 재시도는
없습니다. 거부된 write-ahead 행은 `[refused]`와 충돌 서버 id(알려진 경우)로 남습니다.
동일한 add는 새 origin을 만들거나 전송하지 않고 refused 상태와 삭제 힌트를 출력하며 종료코드 2로
끝납니다. `scopefuel reps pending list`로 origin, 상태, profile, recorded_at, 충돌 id와 reason을
확인하고, 검토 후 `scopefuel reps pending drop ORIGIN`으로 해당 로컬 행을 삭제할 수 있습니다.
drop은 서버에 요청하지 않고 다른 대기 행이나 확인된 행을 건드리지 않습니다. 삭제 후에만 동일한
add를 새 쓰기로 시작할 수 있습니다. 옛 서버는 충돌 시 조용히 덮어쓰므로 fail-closed 바인딩과 아래
shadow 규칙이 로컬 정직성을 지킵니다.

`bench push-local`도 같은 밴드를 씁니다: DB에 한 번 저장한 무작위 128-bit install id,
로컬 rowid와 전체 canonical rep 내용의 SHA-256을 밴드에 접습니다. hostname 변경은 키를 바꾸지
않으며, 새 DB 또는 내용이 바뀐 재사용 rowid는 새 키를 얻습니다. DB를 복사하면 install id도
공유하므로 같은 복사본의 동일 행은 멱등 처리되고 내용이 달라진 행은 별개로 기록됩니다.
각 PUT 직전에 migrate와 같은 GET pre-check로 다른 내용이 차지한 origin을 거부합니다.
기존 DB는 install 메타 테이블 생성과 pending 상태 열 추가로 업그레이드되고 기존 페이로드는
보존됩니다. `reps migrate`는 기존 migrate 밴드를 그대로 씁니다.
fleet의 push-local 중단과 mbp 레인 전용 reps add 운영 규칙은 계속 유지됩니다.

**shadow 규칙(캐시가 서버와 모순될 때).** refetch한 서버 행 `srv:S`의 내용이 같은 `server_id`의
캐시 행과 다르면 그 캐시 사본은 덮어쓰인 rep의 증거입니다 — 서버 행이 `srv:S`로 표시되고 로컬
사본은 `bench_lost_reps`로 옮겨져 `lost:<origin>`(+ 재등록 힌트)로 남으며, 경고는 한 번만 납니다.
서버가 부인하는 srv id 아래에 캐시 내용을 보여 주지 않고, 자동으로 다시 쓰지도 않습니다.
복구는 일회성·운영자 검토 매니페스트(re-add 대상 페이로드 목록)로만 하며 자동 복구 경로는
없습니다.

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

호스트가 푸시된 정본을 검증하지 못하면 오류 없이 번들 스냅샷 표로 폴백합니다. 그래서 구버전 빌드가
검증하지 못하는 행을 더하는 카탈로그 파일(예: `docs/catalog/hk1788.json`)은 모든 호스트가 새 빌드를
돌린 뒤에만 푸시합니다. 푸시한 뒤 각 호스트에서는 `catalog_ttl_s`(기본 3600초)가 지난 뒤에 `scopefuel bench catalog status --check`를 실행해, 출력이 `catalog=server`이거나 푸시 이후에 가져온 cache일 때만 푸시가 반영됐다고 봅니다. 그 전에는 호스트가 푸시 이전 cache를 그대로 답할 수 있습니다.

서버 불가·stale은 "마지막으로 확인된 정본의 재현"까지만 허용합니다: `consult_only`는 절대 완화되지
않고, stale 상태의 비-`default` gate는 `--operator-request`를 요구합니다("서버 다운 ≠ 자유 배정") —
단, consult_only 검사가 이미 통과시킨 `consult_only` row는 stale이 다시 거부하지 않습니다(#1444,
가장 좁은 gate는 stale이 더 넓힐 수 없음).

정본이 아닌 출처는 네 가지로 구분해 표시하며, `stale`은 그중 일부입니다 — 전부를 뭉뚱그리면
"서버가 죽었다"와 "이 호스트는 원래 서버를 안 본다"가 같은 경고가 되어 둘 다 무시됩니다.

| `catalog.source` | 뜻 | `stale` |
|---|---|---|
| `cache` | TTL 내 캐시, 또는 서버 불가·거부지만 `catalog_stale_max_s` 이내 — 정본의 사본 | false |
| `cache-stale` | 서버 불가·거부가 `catalog_stale_max_s` 초과 — 마지막 정본 캐시를 계속 서빙 | **true** |
| `unsupported` | 엔드포인트가 404 — 그 배포본에 카탈로그 라우트가 없다. `/v1/bench/grades` 투영으로 계속 | false |
| `snapshot` (backend=local) | 이 호스트는 정본을 읽도록 설정돼 있지 않다 | false |
| `snapshot` (backend=handoffkeep) | 읽어야 할 정본을 잃었고 캐시도 없다 | **true** |

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
| `devin` | 인증된 `devin` CLI를 PTY로 실행해 기동 배너 + `/usage` 출력 파싱 + `devin models list`의 SWE-2 `Free` 태그 | daily·weekly account 한도(used_pct/reset) + SWE-2 Free 모델 태그 | 출력 형식 의존; `/usage`를 못 읽으면 읽힌 축만 쓰고 나머지는 unknown fail-closed(0/100 추정 없음). 추천표·gate 등재는 `devin-swe2`·`devin-glm52`·`devin-swe17`(Free)·`devin-ds41`(최저가 유료) 4종과 effort 변형 `devin-swe2-medium`·`devin-swe2-max`(Free)·`devin-ds41-max` 3종(#635; effort 는 모델 id 안에 있고, high 급을 상속하지 않는 C 미측정), 그리고 #1380 유료 fusion 레인 `devin-fusion-opus55`·`devin-fusion-sonnet55` 2종(유료·Claude 패밀리·미측정·B 등재)이며 모두 같은 devin 계정 풀을 공유 |

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

**devin도 API가 아니라 CLI를 PTY로 읽습니다.** 한 PTY 세션에서 기동 배너 상태줄을 읽고 `/usage`만 보내 `Daily`·`Weekly` 사용률과 리셋을 파싱한 뒤 `/exit`로 끝냅니다 — 쿼타를 쓰는 프롬프트는 보내지 않습니다. 실측(2026-10-10, v3000.11.3)으로 확인한 매핑: 배너의 `Pro · N% remaining`은 `/usage`의 **Weekly** 축입니다(배너 리셋이 `1d 2h`처럼 24시간을 넘어 그려지고, 같은 회차 `/usage` Weekly 사용률이 `100 - N`과 일치합니다). 일일 창은 `/usage`의 `Daily` 축만이 증명합니다. `devin models list`의 SWE-2 패밀리 행에 `Free` 태그가 있으면 model-scope 버킷(note `free until ~2026-10-10`)을 냅니다 — 계정 쿼타 창이 아니므로 `월` 축으로 렌더되지 않습니다. `/usage`를 못 읽으면 배너가 증명한 weekly만 쓰고 daily는 `used_pct=None`으로 둡니다(0/100 추정 없음); 배너와 `/usage`가 같은 창에서 어긋나면 `/usage`가 이기고 note에 남깁니다. 추천표·gate 등재는 `devin-swe2`·`devin-glm52`·`devin-swe17`(Free)·`devin-ds41`(최저가 유료) 4종과 effort 변형 `devin-swe2-medium`·`devin-swe2-max`(Free)·`devin-ds41-max` 3종(#635; effort 는 모델 id 안에 있고, high 급을 상속하지 않는 C 미측정), 그리고 #1380 유료 fusion 레인 `devin-fusion-opus55`·`devin-fusion-sonnet55` 2종(유료·Claude 패밀리·미측정·B 등재 — tester 분리는 claude 값 사용, 쿼타 풀은 devin)이며 모두 같은 devin 계정 풀을 공유합니다. 이 둘을 제외한 그 외 유료 Devin 모델(Fable·Astra 계열 fusion 및 Claude/OpenAI/Gemini 등)은 추천표·gate 등재 범위 밖입니다. credential/config 파일은 읽지 않습니다.

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
