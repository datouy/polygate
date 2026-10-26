"""上游 Provider 抽象（统一走 OpenAI 兼容协议）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Provider:
    name: str
    base_url: str
    api_keys: list[str] = field(default_factory=list)
    timeout: float = 120.0

    @property
    def chat_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    @property
    def embeddings_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/embeddings"):
            return base
        return f"{base}/embeddings"

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"

    def headers(self, api_key: str) -> dict:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers
