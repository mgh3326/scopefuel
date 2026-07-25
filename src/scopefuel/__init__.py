"""scopefuel — AI 코딩 플랜의 스코프별 남은 여유 계측기."""

from .model import SCHEMA, Bucket, ProviderResult, Scope, Verdict, verdict_for

__version__ = "0.1.0"
__all__ = ["SCHEMA", "Bucket", "ProviderResult", "Scope", "Verdict", "verdict_for", "__version__"]
