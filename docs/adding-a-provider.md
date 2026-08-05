# 새 provider 추가하기

새 AI 코딩 플랜이 생겼을 때 scopefuel에 붙이는 방법은 두 가지입니다. **먼저 1번을 시도하세요** —
대부분의 provider가 여기에 들어맞고, 코드도 릴리스도 필요 없습니다.

## 1. 선언형 TOML 스펙 (권장)

패턴: *로컬 파일에서 토큰을 읽어 → HTTP 한 번 → 응답 JSON의 특정 경로에서 사용률과 리셋시각을 꺼낸다.*

파일을 `~/.config/scopefuel/providers/<id>.toml`에 두면 자동으로 인식됩니다
(`SCOPEFUEL_SPEC_DIR`로 디렉터리를 추가할 수 있습니다). `scopefuel --list-providers`로 확인하세요.

```toml
id = "myplan"                    # --only myplan 으로 지정하는 이름
plan_path = ["plan_type"]        # (선택) 응답에서 플랜 이름을 꺼낼 경로
class = "preserve"               # (선택) "preserve" | "spend". 미지정 시 "preserve"

[credentials]
file = "~/.myagent/auth.json"          # 또는 token_env = "MYPLAN_TOKEN"
token_path = ["tokens", "access_token"]  # 중첩 키 경로

[request]
method = "GET"
url = "https://api.example.com/v1/usage"
timeout = 20
[request.headers]
Authorization = "Bearer {token}"       # {token} 자리에 위에서 읽은 토큰이 들어간다
Accept = "application/json"

# 계정 전체를 막는 한도
[[buckets]]
label = "7d"
window = "7d"                # 5h / 1d / 7d / 30d → horizon(now|week) 자동 추론
scope = "account"            # account | model | group
used_pct_path = ["rate_limit", "primary_window", "used_percent"]
resets_at_path = ["rate_limit", "primary_window", "reset_at"]
resets_at_kind = "epoch"     # epoch | iso (기본 iso)

# 리스트를 순회해 버킷 여러 개 만들기 (모델 전용 한도 등)
[[buckets]]
for_each = ["additional_rate_limits"]
label = "{item[limit_name]} 7d"
window = "7d"
scope = "model"
scope_name = "{item[limit_name]}"
used_pct_path = ["rate_limit", "primary_window", "used_percent"]   # item 기준 상대 경로
resets_at_path = ["rate_limit", "primary_window", "reset_at"]
resets_at_kind = "epoch"
```

사용률 대신 **남은 비율**을 주는 API라면:

```toml
remaining_fraction_path = ["quotaInfo", "remainingFraction"]   # 0.42 → used 58%
```

### pool class — 이 풀을 어떻게 다룰 것인가

| class | 의미 | 예 |
|---|---|---|
| `preserve` | 75%/90% 사용률을 WARN/CRIT로 승격. 계정/모델/그룹 차단 판정에 그대로 사용. | claude, codex |
| `spend` | 고사용을 정상으로 본다. 리셋 전 24시간 미만, 70% 미만 bucket이 있으면 WASTE 권고. | kiro, clinepass, agy, grok, kimi |

`class`는 provider 전체(선언형 TOML 스펙 및 Python 플러그인)에 적용된다. 내장 provider를 TOML 스펙으로 완전 교체할 때 같은 `id` 스펙에 `class`를 지정할 수 있다. (단, `class`만 단독으로 덮어쓰는 것은 지원하지 않으며, 같은 `id`의 스펙은 엔드포인트/자격증명/버킷 설정을 모두 포함하는 완전한 대체 스펙이어야 한다.)

### 스코프를 고르는 기준 (이 도구의 핵심)

| scope | 의미 | 예 |
|---|---|---|
| `account` | 이 한도가 차면 **전부** 막힌다 | Claude 5h/7d, Codex primary window |
| `model` | **그 모델만** 막힌다. 다른 모델은 계정 한도 안에서 계속 쓸 수 있다 | Claude weekly_scoped(Fable), Codex Spark |
| `group` | **그 모델 그룹만** 막힌다. 다른 그룹은 독립 | Antigravity Gemini 그룹 / Claude·GPT 그룹 |

잘못 고르면 판정이 뒤집힙니다. 모델 한정 한도를 `account`로 넣으면 "계정이 막혔다"고 오독하게 되고,
반대로 계정 한도를 `model`로 넣으면 실제로 막혔는데 초록불이 뜹니다. 확실하지 않으면 그 한도가 찼을 때
**다른 모델로 작업이 되는지** 한 번 실험해 보고 정하세요.

### 스펙으로 내장 provider 덮어쓰기

같은 `id`를 쓰면 내장 Python provider보다 스펙이 우선합니다. 제공사가 엔드포인트를 바꿔서 내장
provider가 깨졌을 때, 릴리스를 기다리지 않고 `~/.config/scopefuel/providers/claude.toml`로 임시 수정할
수 있습니다. (고쳤다면 이슈나 PR로 알려주시면 반영합니다.)

## 2. Python entry-point 플러그인

Kimi처럼 quota가 interactive CLI의 PTY 출력에만 있는 경우에는 TOML HTTP 스펙을 억지로
만들지 말고, `src/scopefuel/providers/kimi.py`처럼 읽기 전용 subprocess probe를 둡니다.
CLI가 rate-limit/429를 반환하면 재시도하지 않고 `ProviderResult.error`로 중단해야 합니다.

스펙의 틀을 벗어나는 경우 — 다단계 호출, OAuth 토큰 갱신, 로컬 프로세스/포트 탐색, gRPC-web 등:

```python
# myscopefuel_plugin.py
from scopefuel.model import Bucket, ProviderResult, Scope


def fetch() -> ProviderResult:
    return ProviderResult(
        id="myplan",
        plan="pro",
        buckets=[
            Bucket(
                label="5h",
                window="5h",
                used_pct=12.5,
                resets_at="2026-07-25T06:00:00Z",
                scope=Scope("account"),
                horizon="now",
            ),
        ],
        source="my-api",
        raw={},
    )
```

```toml
# 플러그인 패키지의 pyproject.toml
[project.entry-points."scopefuel.providers"]
myplan = "myscopefuel_plugin:fetch"
```

규칙 몇 가지:

- **모르는 값은 `None`으로 두세요.** 0으로 채우면 "여유 있다"는 거짓 신호가 됩니다.
- pool class는 callable 객체의 `pool_class` 속성(예: `fetch.pool_class = "spend"`)을 부여하거나 반환 `ProviderResult(..., pool_class="spend")`로 지정할 수 있습니다. (우선순위: callable `pool_class` 메타데이터 > 반환 `ProviderResult.pool_class` > 기본값 `preserve`)
- 실패는 예외를 던지거나 `ProviderResult(id=..., error=..., hint=...)`로 반환하세요. 캐시 폴백과
  종료코드 처리는 코어가 합니다.
- 토큰을 갱신하거나 파일에 쓰지 마세요. scopefuel은 읽기 전용 계측기입니다.
- 내장 provider로 편입할 만한 것은 `src/scopefuel/providers/`에 PR 해주세요. 테스트는 실제 응답을
  **리댁션한** 픽스처(`tests/fixtures/`)로 작성합니다 — 계정 식별자·이메일·토큰은 절대 커밋하지 않습니다.
