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
from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    models: list[str] = Field(min_length=1)

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

    @field_validator("models")
    @classmethod
    def validate_models(cls, value: list[str]) -> list[str]:
        result = [item.strip() for item in value]
        if not all(result) or len(result) != len(set(result)):
            raise ValueError("模型 ID 不能为空或重复")
        return result

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        from urllib.parse import urlsplit
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("API 地址必须是无账号、查询参数和片段的 HTTP(S) URL")
        return value.rstrip("/")


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    providers: list[ProviderConfig] = Field(default_factory=list)
    default_provider: str | None = None
    default_model: str | None = None


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
            config = ModelConfig.model_validate(self._read_yaml(self.config_path))
        except Exception as exc:
            raise ModelConfigurationError(f"模型配置文件无效：{exc}") from exc
        if config.version != 1 or len({item.id for item in config.providers}) != len(config.providers):
            raise ModelConfigurationError("模型配置版本不支持或供应商 ID 重复")
        return config

    def _credentials(self) -> dict[str, str]:
        data = self._read_yaml(self.credentials_path)
        if data.get("version", 1) != 1 or not isinstance(data.get("api_keys", {}), dict):
            raise ModelConfigurationError("密钥文件格式无效")
        return {str(key): str(value) for key, value in data.get("api_keys", {}).items() if value}

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
            }

    def upsert(self, provider: ProviderConfig, *, api_key: str | None = None, clear_key: bool = False) -> dict[str, Any]:
        with self._lock:
            config = self._config()
            providers = [item for item in config.providers if item.id != provider.id]
            providers.append(provider)
            config.providers = providers
            if config.default_provider is None:
                config.default_provider = provider.id
                config.default_model = provider.models[0]
            elif config.default_provider == provider.id and config.default_model not in provider.models:
                config.default_model = provider.models[0]
            if api_key is not None or clear_key:
                keys = self._credentials()
                if clear_key:
                    keys.pop(provider.id, None)
                elif api_key and api_key.strip():
                    keys[provider.id] = api_key.strip()
                self._write_yaml(self.credentials_path, {"version": 1, "api_keys": keys})
            self._write_yaml(self.config_path, config.model_dump())
            return self.public()

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
            if config.default_provider == provider_id:
                first = config.providers[0] if config.providers else None
                config.default_provider = first.id if first else None
                config.default_model = first.models[0] if first else None
            self._write_yaml(self.config_path, config.model_dump())
            keys = self._credentials()
            keys.pop(provider_id, None)
            self._write_yaml(self.credentials_path, {"version": 1, "api_keys": keys})
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
