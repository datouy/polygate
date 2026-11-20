"""请求模型（OpenAI 兼容格式，其余字段透传）。"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: Any = None
    tools: Any = None
    tool_choice: Any = None


class EmbeddingsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    input: Any


class ReplayLabRequest(BaseModel):
    """管理员发起的多模型请求重放。"""

    request_id: str = Field(min_length=1, max_length=128)
    models: list[str] = Field(default_factory=list, max_length=20)
    include_response: bool = True


class CompatibilityProbeRequest(BaseModel):
    models: list[str] = Field(default_factory=list, max_length=200)
    probes: list[str] = Field(default_factory=lambda: ["chat", "stream_usage"])


class ClientKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    models: list[str] = Field(default_factory=list, max_length=200)
    rpm: float = Field(default=0, ge=0)
    rpm_burst: float = Field(default=0, ge=0)
    daily_calls: int = Field(default=0, ge=0)
    monthly_calls: int = Field(default=0, ge=0)
    monthly_budget: float = Field(default=0, ge=0)
    expires_at: float | None = Field(default=None, gt=0)
    note: str = Field(default="", max_length=500)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name is required")
        return value

    @field_validator("models")
    @classmethod
    def _clean_models(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


class ClientKeyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    models: list[str] | None = Field(default=None, max_length=200)
    rpm: float | None = Field(default=None, ge=0)
    rpm_burst: float | None = Field(default=None, ge=0)
    daily_calls: int | None = Field(default=None, ge=0)
    monthly_calls: int | None = Field(default=None, ge=0)
    monthly_budget: float | None = Field(default=None, ge=0)
    expires_at: float | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def _clean_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name is required")
        return value

    @field_validator("models")
    @classmethod
    def _clean_optional_models(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


class RateLimitPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rate: float = Field(gt=0)
    burst: float = Field(default=0, ge=0)


class ProviderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    timeout: float | None = Field(default=None, gt=0, le=600)
    rate_limit: RateLimitPatch | None = None

    @field_validator("base_url")
    @classmethod
    def _clean_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an http(s) URL")
        return value

    @field_validator("api_key")
    @classmethod
    def _clean_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("api_key cannot be empty")
        return value


class ModelRoutePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    weight: float = Field(default=1, gt=0, le=1_000_000)
    capabilities: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("provider", "model")
    @classmethod
    def _clean_route_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("provider and model are required")
        return value

    @field_validator("capabilities")
    @classmethod
    def _clean_capabilities(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip().lower() for value in values if str(value).strip()))


class ModelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    kind: Literal["chat", "embeddings", "audio"] = "chat"
    providers: list[ModelRoutePatch] = Field(min_length=1, max_length=100)
    capabilities: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("name")
    @classmethod
    def _clean_model_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model name is required")
        return value

    @field_validator("capabilities")
    @classmethod
    def _clean_model_capabilities(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip().lower() for value in values if str(value).strip()))


class BudgetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_max_output_tokens: int | None = Field(default=None, gt=0)
    bytes_per_token: float | None = Field(default=None, gt=0)
    reservation_ttl: float | None = Field(default=None, gt=0)


class ConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str | None = Field(default=None, max_length=128)
    providers: dict[str, ProviderPatch] = Field(default_factory=dict)
    models: list[ModelPatch] = Field(default_factory=list, max_length=1000)
    budget: BudgetPatch | None = None

    @model_validator(mode="after")
    def _has_change(self):
        if not self.providers and not self.models and self.budget is None:
            raise ValueError("at least one config change is required")
        names = [model.name for model in self.models]
        if len(names) != len(set(names)):
            raise ValueError("model names must be unique")
        return self


class ProviderTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)
    timeout: float = Field(default=8, ge=1, le=30)

    @field_validator("base_url")
    @classmethod
    def _clean_test_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an http(s) URL")
        return value

    @field_validator("api_key")
    @classmethod
    def _clean_test_key(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        return value or None
