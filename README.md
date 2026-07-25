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

## herdr 통합

`herdr-plugin.toml`이 포함되어 있어 그대로 설치할 수 있습니다.

```bash
herdr plugin install mgh3326/scopefuel     # 또는 로컬 개발 시
herdr plugin link ~/work/scopefuel
```

- 액션 `scopefuel.check` — 한 줄 요약 출력
- 페인 `scopefuel.gauge` — `--watch 60`으로 상시 계기판 (overlay)

## 지원 provider

| id | 경로 | 얻는 것 | 제약 |
|---|---|---|---|
| `claude` | `~/.claude/.credentials.json` → `api.anthropic.com/api/oauth/usage` | 5h·7d 계정 한도 + 모델별(weekly_scoped) | 토큰 만료 시 claude 세션이 갱신해야 함 |
| `codex` | `~/.codex/auth.json` → `chatgpt.com/backend-api/wham/usage` | primary/secondary 창 + 모델 전용 버킷 | — |
| `agy` | 실행 중 `agy`의 로컬 language server → 실패 시 cloudcode-pa | 로컬=weekly+5h, 클라우드=5h만 | 모델별 분해 불가(아래) |

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
같은 id를 정의하면 **스펙이 내장 provider를 덮어씁니다** — 엔드포인트가 깨졌을 때 릴리스를 기다리지
않고 사용자가 직접 고칠 수 있게 한 의도적 설계입니다.

## 주의

여기서 쓰는 엔드포인트 중 일부는 **공식 문서화된 API가 아닙니다**(`wham/usage`,
`RetrieveUserQuotaSummary`, `v1internal:fetchAvailableModels`). 제공사가 예고 없이 바꿀 수 있고,
그때는 해당 provider가 조용히 실패하는 대신 오류와 힌트를 표시합니다. 자격증명은 각 CLI가 이미
로컬에 저장한 파일을 **읽기만** 하며, 어디에도 전송하지 않습니다(해당 제공사 자신의 API 제외).

## 라이선스

MIT. Antigravity 클라우드 경로(`loadCodeAssist` → `fetchAvailableModels`)는
[cclimits](https://github.com/cruzanstx/cclimits)(MIT)의 구현을 참고했습니다.
