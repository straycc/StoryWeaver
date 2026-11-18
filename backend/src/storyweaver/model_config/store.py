"""用户目录中的模型配置与密钥，及每个任务的模型快照。"""

from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

import yaml
from agents import ModelSettings
from agents.models.interface import Model
from openai.types.shared.reasoning import Reasoning
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..llm import OpenAICompatibleProviderSettings

_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_CURRENT_MODEL: ContextVar[Any | None] = ContextVar("storyweaver_task_model", default=None)
_CURRENT_REASONING: ContextVar[tuple[str, str]] = ContextVar("storyweaver_task_reasoning", default=("default", "other"))


class ModelConfigurationError(ValueError):
    """模型配置不存在或无法用于当前任务。"""


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    base_url: str
    models: list[str] = Field(default_factory=list)
    # 仅保留上一版目录字段以读取旧文件，新 Embedding 配置不再使用它。
    embedding_models: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _PROVIDER_ID.fullmatch(value):
            raise ValueError("供应商 ID 只能使用小写字母、数字、下划线和连字符")
        return value

    @field_validator("name", "base_url")
    @classmethod
    def validate_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("名称和 API 地址不能为空")
        return value

    @field_validator("models", "embedding_models")
    @classmethod
    def validate_models(cls, value: list[str]) -> list[str]:
        result = [item.strip() for item in value]
        if not all(result) or len(result) != len(set(result)):
            raise ValueError("模型 ID 不能为空或重复")
        return result

    @model_validator(mode="after")
    def validate_capabilities(self) -> ProviderConfig:
        if not self.models and not self.embedding_models:
            raise ValueError("请至少配置一个对话模型或 Embedding 模型")
        return self

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        from urllib.parse import urlsplit
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("API 地址必须是无账号、查询参数和片段的 HTTP(S) URL")
        return value.rstrip("/")


class EmbeddingModelConfig(BaseModel):
    """设置接口只暴露模型信息，检索策略由后端管理。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(default="Embedding", min_length=1)
    base_url: str = Field(min_length=1)
    model_id: str = Field(min_length=1)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return ProviderConfig.validate_base_url(value)


class EmbeddingConfig(EmbeddingModelConfig):
    """持久化内部检索策略，不进入模型设置接口。"""

    dimensions: int | None = Field(default=None, gt=0)
    timeout_seconds: float = Field(default=5.0, gt=0, le=60, allow_inf_nan=False)
    minimum_similarity: float = Field(default=0.35, ge=-1, le=1, allow_inf_nan=False)


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    providers: list[ProviderConfig] = Field(default_factory=list)
    default_provider: str | None = None
    default_model: str | None = None
    embedding: EmbeddingConfig | None = None


class ModelCatalog:
    """本地单人模型目录；每次读取文件以支持手动编辑后立即生效。"""

    def __init__(self, home: Path | None = None) -> None:
        self.home = (home or Path.home() / ".storyweaver").expanduser().resolve()
        self.config_path = self.home / "models.yaml"
        self.credentials_path = self.home / ".credentials.yaml"
        self._lock = RLock()

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ModelConfigurationError(f"{path} 必须是 YAML 对象")
        return value

    @staticmethod
    def _write_yaml(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            path.parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(dict(value), handle, allow_unicode=True, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _config(self) -> ModelConfig:
        try:
            raw = self._read_yaml(self.config_path)
            embedding = raw.get("embedding")
            legacy_provider = None
            if isinstance(embedding, dict) and "provider_id" in embedding:
                # 将上一版供应商引用展开为独立地址，并复制密钥以解除后续绑定。
                legacy_provider = next((item for item in raw.get("providers", []) if item.get("id") == embedding["provider_id"]), None)
                if legacy_provider is None:
                    raise ValueError("旧 Embedding 配置引用的供应商不存在")
                raw["embedding"] = {
                    **{key: value for key, value in embedding.items() if key != "provider_id"},
                    "base_url": legacy_provider["base_url"],
                    "name": f"{legacy_provider['name']} Embedding",
                }
            if isinstance(raw.get("embedding"), dict):
                # 兼容旧的页面开关；是否使用模型只取决于有没有有效配置。
                raw["embedding"].pop("enabled", None)
            config = ModelConfig.model_validate(raw)
        except Exception as exc:
            raise ModelConfigurationError(f"模型配置文件无效：{exc}") from exc
        if config.version != 1 or len({item.id for item in config.providers}) != len(config.providers):
            raise ModelConfigurationError("模型配置版本不支持或供应商 ID 重复")
        if legacy_provider is not None:
            data = self._credential_data()
            if "embedding_api_key" not in data:
                key = data.get("api_keys", {}).get(legacy_provider["id"])
                if key:
                    self._set_embedding_key(str(key))
            self._write_yaml(self.config_path, config.model_dump())
        return config

    def _credential_data(self) -> dict[str, Any]:
        data = self._read_yaml(self.credentials_path)
        if data.get("version", 1) != 1 or not isinstance(data.get("api_keys", {}), dict):
            raise ModelConfigurationError("密钥文件格式无效")
        if not isinstance(data.get("embedding_api_key", ""), str):
            raise ModelConfigurationError("Embedding 密钥必须为字符串")
        return data

    def _credentials(self) -> dict[str, str]:
        return {str(key): str(value) for key, value in self._credential_data().get("api_keys", {}).items() if value}

    def _set_chat_keys(self, keys: Mapping[str, str]) -> None:
        data = self._credential_data()
        self._write_yaml(self.credentials_path, {**data, "version": 1, "api_keys": dict(keys)})

    def _set_embedding_key(self, key: str | None) -> None:
        data = self._credential_data()
        if key:
            data["embedding_api_key"] = key
        else:
            data.pop("embedding_api_key", None)
        self._write_yaml(self.credentials_path, {**data, "version": 1})

    def public(self) -> dict[str, Any]:
        with self._lock:
            config = self._config()
            keys = self._credentials()
            return {
                "providers": [
                    {**item.model_dump(), "has_api_key": bool(keys.get(item.id)),
                     "reasoning_levels_by_model": {model: list(self.reasoning_levels(item, model)) for model in item.models}}
                    for item in config.providers
                ],
                "default_provider": config.default_provider,
                "default_model": config.default_model,
                "embedding": {**config.embedding.model_dump(include=set(EmbeddingModelConfig.model_fields)), "has_api_key": bool(self._credential_data().get("embedding_api_key"))} if config.embedding else None,
            }

    def upsert(self, provider: ProviderConfig, *, api_key: str | None = None, clear_key: bool = False) -> dict[str, Any]:
        with self._lock:
            config = self._config()
            providers = [item for item in config.providers if item.id != provider.id]
            providers.append(provider)
            config.providers = providers
            self._repair_selections(config)
            if api_key is not None or clear_key:
                keys = self._credentials()
                if clear_key:
                    keys.pop(provider.id, None)
                elif api_key and api_key.strip():
                    keys[provider.id] = api_key.strip()
                self._set_chat_keys(keys)
            self._write_yaml(self.config_path, config.model_dump())
            return self.public()

    @staticmethod
    def _repair_selections(config: ModelConfig) -> None:
        """仅修复对话默认模型，Embedding 配置独立保存。"""
        chat_providers = [item for item in config.providers if item.models]
        current = next((item for item in chat_providers if item.id == config.default_provider), None)
        if current is None or config.default_model not in current.models:
            selected = current or next(iter(chat_providers), None)
            config.default_provider = selected.id if selected else None
            config.default_model = selected.models[0] if selected else None

    @staticmethod
    def _embedding_configuration(model: EmbeddingModelConfig, previous: EmbeddingConfig | None) -> EmbeddingConfig:
        """编辑同一模型时保留内部策略；更换模型后使用默认策略与维度。"""
        if previous and previous.base_url == model.base_url and previous.model_id == model.model_id:
            return EmbeddingConfig.model_validate({**previous.model_dump(), **model.model_dump()})
        return EmbeddingConfig.model_validate(model.model_dump())

    def set_embedding(self, selection: EmbeddingModelConfig | None, *, api_key: str | None = None,
                      clear_key: bool = False) -> dict[str, Any]:
        with self._lock:
            config = self._config()
            if selection is not None and not isinstance(selection, EmbeddingConfig):
                selection = self._embedding_configuration(selection, config.embedding)
            if selection is None or clear_key:
                self._set_embedding_key(None)
            elif api_key and api_key.strip():
                self._set_embedding_key(api_key.strip())
            config.embedding = selection
            self._write_yaml(self.config_path, config.model_dump())
            return self.public()

    def embedding_settings(self, selection: EmbeddingModelConfig | None = None, *, api_key: str | None = None,
                           clear_key: bool = False):
        """每次检索读取当前配置；调用方持有返回值直到本次检索完成。"""
        from ..retrieval.embeddings import EmbeddingSettings

        with self._lock:
            config = self._config()
            selection = selection or config.embedding
            if selection is None:
                return None
            if not isinstance(selection, EmbeddingConfig):
                selection = self._embedding_configuration(selection, config.embedding)
            key = "" if clear_key else (api_key.strip() if api_key and api_key.strip() else self._credential_data().get("embedding_api_key", ""))
            if not key and urlsplit(selection.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ModelConfigurationError("Embedding 尚未配置独立 API Key")
            return EmbeddingSettings(
                base_url=selection.base_url, model=selection.model_id, api_key=key,
                dimensions=selection.dimensions, timeout_seconds=selection.timeout_seconds,
                minimum_similarity=selection.minimum_similarity,
            )

    def set_default(self, provider_id: str, model_id: str) -> dict[str, Any]:
        with self._lock:
            config = self._config()
            self._find(config, provider_id, model_id)
            config.default_provider, config.default_model = provider_id, model_id
            self._write_yaml(self.config_path, config.model_dump())
            return self.public()

    def delete(self, provider_id: str) -> dict[str, Any]:
        with self._lock:
            config = self._config()
            if not any(item.id == provider_id for item in config.providers):
                raise KeyError(provider_id)
            config.providers = [item for item in config.providers if item.id != provider_id]
            self._repair_selections(config)
            self._write_yaml(self.config_path, config.model_dump())
            keys = self._credentials()
            keys.pop(provider_id, None)
            self._set_chat_keys(keys)
            return self.public()

    @staticmethod
    def _find(config: ModelConfig, provider_id: str, model_id: str) -> ProviderConfig:
        for provider in config.providers:
            if provider.id == provider_id and model_id in provider.models:
                return provider
        raise ModelConfigurationError("所选模型不存在，请在设置中重新选择")

    def selection(self, provider_id: str | None = None, model_id: str | None = None) -> tuple[ProviderConfig, str, str | None]:
        with self._lock:
            config = self._config()
            provider_id = provider_id or config.default_provider
            model_id = model_id or config.default_model
            if not provider_id or not model_id:
                raise ModelConfigurationError("尚未配置模型，请先打开模型设置")
            provider = self._find(config, provider_id, model_id)
            key = self._credentials().get(provider_id)
            if not key and provider.base_url.split("/")[2].split(":")[0] not in {"localhost", "127.0.0.1", "::1"}:
                raise ModelConfigurationError(f"{provider.name} 尚未配置 API Key")
            return provider, model_id, key

    def create_model(self, provider_id: str | None = None, model_id: str | None = None) -> Any:
        provider, model_id, key = self.selection(provider_id, model_id)
        return OpenAICompatibleProviderSettings(
            base_url=provider.base_url, model_name=model_id, api_key=key or "local",
        ).create_provider().get_model(model_id)

    @staticmethod
    def reasoning_family(provider: ProviderConfig, model_id: str) -> str:
        host = urlsplit(provider.base_url).hostname
        if host == "api.deepseek.com":
            return "deepseek"
        if host == "api.openai.com" and model_id.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6")):
            return "openai"
        return "other"

    @classmethod
    def reasoning_levels(cls, provider: ProviderConfig, model_id: str) -> tuple[str, ...]:
        family = cls.reasoning_family(provider, model_id)
        if family == "deepseek":
            return ("default", "off", "low", "high", "max")
        if family == "openai":
            if model_id.endswith("-pro"):
                return ("default", "high")
            return ("default", "low", "medium", "high")
        return ("default",)

class RoutedModel(Model):
    """把 SDK 模型调用路由到当前任务冻结的模型实例。"""

    def __init__(self, catalog: ModelCatalog) -> None:
        self.catalog = catalog

    def _model(self) -> Any:
        return _CURRENT_MODEL.get() or self.catalog.create_model()

    def get_retry_advice(self, request: Any) -> Any:
        return self._model().get_retry_advice(request)

    @staticmethod
    def _settings(model_settings: ModelSettings) -> ModelSettings:
        level, family = _CURRENT_REASONING.get()
        if level == "default" or family == "other":
            return model_settings
        extra_body = dict(model_settings.extra_body or {})
        # 结构化结果修复阶段刻意关闭思考，不能被会话偏好重新开启。
        if extra_body.get("thinking") == {"type": "disabled"} and level != "off":
            return model_settings
        if family == "deepseek":
            extra_body["thinking"] = {"type": "disabled" if level == "off" else "enabled"}
            if level == "off":
                extra_body.pop("reasoning_effort", None)
            else:
                extra_body["reasoning_effort"] = level
            return replace(model_settings, reasoning=None, extra_body=extra_body)
        extra_body.pop("reasoning_effort", None)
        return replace(model_settings, reasoning=Reasoning(effort=level), extra_body=extra_body or None)

    @classmethod
    def _with_settings(cls, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if "model_settings" in kwargs:
            return args, {**kwargs, "model_settings": cls._settings(kwargs["model_settings"])}
        if len(args) > 2:
            updated = list(args)
            updated[2] = cls._settings(updated[2])
            return tuple(updated), kwargs
        return args, kwargs

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        args, kwargs = self._with_settings(args, kwargs)
        return await self._model().get_response(*args, **kwargs)

    async def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        args, kwargs = self._with_settings(args, kwargs)
        async for event in self._model().stream_response(*args, **kwargs):
            yield event


@contextmanager
def bind_model(model: Any, *, reasoning_level: str = "default", reasoning_family: str = "other") -> Iterator[None]:
    token = _CURRENT_MODEL.set(model)
    reasoning_token = _CURRENT_REASONING.set((reasoning_level, reasoning_family))
    try:
        yield
    finally:
        _CURRENT_REASONING.reset(reasoning_token)
        _CURRENT_MODEL.reset(token)
