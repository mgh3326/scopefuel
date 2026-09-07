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

이 도구는 **읽기 전용**입니다. 토큰을 갱신하거나 한도를 조작하지 않습니다.

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

## Benchmark backend

벤치 점수와 대표 실행 기록은 기본적으로 로컬 SQLite를 사용합니다. 여러 노드가 같은 정본을 읽어야
할 때만 XDG config의 `scopefuel/config.toml`에 다음을 설정합니다.

```toml
[bench]
backend = "handoffkeep"
cache_ttl_s = 21600
```

이 모드에서는 `HANDOFFKEEP_URL`과 `HANDOFFKEEP_TOKEN`을 사용합니다. 로컬 SQLite는 6시간 TTL의
캐시가 되며, 읽기 실패는 캐시와 나이를 표시해 계속 동작하고, 쓰기 실패는 종료코드 2로 끝납니다.
`scopefuel bench push-local`은 기존 로컬 점수·reps를 지우지 않고 한 번 이관할 때 사용합니다.
급 배치는 `scopefuel bench grades set`에서 deviation reference를 반드시 함께 남겨야 합니다.

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
  **WASTE** 권고를 낸다. `kiro`, `clinepass`, `agy`, `grok`, `kimi`.

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
