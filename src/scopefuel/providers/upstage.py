"""Upstage Solar Pro 4 — Large Trial 무료 기간 동안의 고정값 provider.

Upstage 는 조회 가능한 쿼타 소스가 없다. 콘솔 usage 엔드포인트도, 세션 쿠키도,
로컬 usage 파일도 없으므로 조건 분기 없이 고정값을 낸다. 2026-09-20 까지는
Large Trial 무제한 사용이고, 소진/전환 판단은 운영자가 별도로 정리한다(#211).
"""

from __future__ import annotations

from ..model import Bucket, ProviderResult, Scope

PROVIDER_ID = "upstage"
NOTE = "Large Trial free until 2026-09-20, 무제한 사용, 소진/전환은 운영자 정리(#211)"


def fetch() -> ProviderResult:
    return ProviderResult(
        id=PROVIDER_ID,
        buckets=[
            Bucket(
                label="solar-pro4",
                window="30d",
                used_pct=0.0,
                resets_at=None,
                scope=Scope("account"),
                horizon="week",
                note=NOTE,
            )
        ],
        note=NOTE,
        source="fixed",
        pool_class="spend",
    )
