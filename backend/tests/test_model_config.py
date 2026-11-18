"""用户模型目录与会话选择的关键行为。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from pydantic import ValidationError

from fastapi.testclient import TestClient
from agents import ModelSettings

from storyweaver.api.app import create_app
from storyweaver.model_config import ModelCatalog
from storyweaver.model_config.store import EmbeddingConfig, EmbeddingModelConfig, ModelConfigurationError, ProviderConfig, RoutedModel, bind_model
from storyweaver.novel_creation.application import NovelApplicationSettings


def test_catalog_keeps_secret_out_of_public_data() -> None:
    with TemporaryDirectory() as directory:
        catalog = ModelCatalog(Path(directory) / "home")
        catalog.upsert(ProviderConfig(id="remote", name="测试", base_url="https://example.com/v1", models=["m1", "m2"]), api_key="secret-123")
        assert catalog.public()["providers"][0]["has_api_key"] is True
        assert "secret-123" not in str(catalog.public())
        assert catalog.credentials_path.stat().st_mode & 0o777 == 0o600
        assert catalog.selection("remote", "m2")[2] == "secret-123"
        openai = ProviderConfig(id="openai", name="OpenAI", base_url="https://api.openai.com/v1", models=["gpt-4o", "gpt-5", "gpt-5-pro"])
        assert catalog.reasoning_levels(openai, "gpt-4o") == ("default",)
        assert catalog.reasoning_levels(openai, "gpt-5") == ("default", "low", "medium", "high")
        assert catalog.reasoning_levels(openai, "gpt-5-pro") == ("default", "high")


def test_model_api_and_empty_session_reuse() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        catalog = ModelCatalog(root / "home")
        app = create_app(
            settings=NovelApplicationSettings(base_url="http://127.0.0.1:11434/v1", model="unconfigured"),
            database_url=f"sqlite+pysqlite:///{root / 'storyweaver.db'}",
            model_catalog=catalog,
        )
        with TestClient(app) as client:
            response = client.put("/api/v1/models/providers/local", json={
                "id": "local", "name": "本机", "base_url": "http://127.0.0.1:11434/v1",
                "models": ["demo"], "api_key": "test-key",
            })
            assert response.status_code == 200, response.text
            assert "test-key" not in response.text
            created = client.post("/api/v1/sessions", json={}).json()
            session_id = created["session_id"]
            assert created["selected_model"] is None
            assert client.get("/api/v1/bootstrap").json()["model"] == "demo"
            selected = client.put(f"/api/v1/sessions/{session_id}/model", json={"provider_id": "local", "model_id": "demo"})
            assert selected.status_code == 200, selected.text
            assert client.get(f"/api/v1/sessions/{session_id}").json()["selected_model"]["model_id"] == "demo"
            assert client.post("/api/v1/sessions", json={}).json()["session_id"] == session_id
            client.put("/api/v1/models/providers/backup", json={
                "id": "backup", "name": "备用", "base_url": "http://127.0.0.1:11434/v1", "models": ["fallback"],
            })
            client.delete("/api/v1/models/providers/local")
            assert client.get(f"/api/v1/sessions/{session_id}").json()["selected_model"] is None
            assert client.get("/api/v1/bootstrap").json()["model"] == "fallback"


def test_running_task_keeps_bound_model_after_switch() -> None:
    class FakeModel:
        def __init__(self, name: str) -> None:
            self.name = name

        async def get_response(self, *args: object, **kwargs: object) -> str:
            await asyncio.sleep(0)
            return self.name

    async def check() -> None:
        with TemporaryDirectory() as directory:
            routed = RoutedModel(ModelCatalog(Path(directory)))
            with bind_model(FakeModel("first")):
                task = asyncio.create_task(routed.get_response())
            with bind_model(FakeModel("second")):
                assert await routed.get_response() == "second"
            assert await task == "first"

    asyncio.run(check())


def test_reasoning_level_is_bound_to_model_calls() -> None:
    class FakeModel:
        async def get_response(self, *args: object, **kwargs: object) -> ModelSettings:
            return kwargs["model_settings"]  # type: ignore[return-value]

    async def check() -> None:
        with TemporaryDirectory() as directory:
            routed = RoutedModel(ModelCatalog(Path(directory)))
            with bind_model(FakeModel(), reasoning_level="high", reasoning_family="deepseek"):
                task = asyncio.create_task(routed.get_response(model_settings=ModelSettings()))
            result = await task
            assert result.extra_body == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
            with bind_model(FakeModel(), reasoning_level="off", reasoning_family="deepseek"):
                off = await routed.get_response(model_settings=ModelSettings(extra_body={"reasoning_effort": "low"}))
            assert off.extra_body == {"thinking": {"type": "disabled"}}
            with bind_model(FakeModel(), reasoning_level="medium", reasoning_family="openai"):
                openai = await routed.get_response(model_settings=ModelSettings(extra_body={"reasoning_effort": "low"}))
            assert openai.reasoning.effort == "medium"
            assert openai.extra_body is None

    asyncio.run(check())


def test_reasoning_selection_validates_provider_and_survives_session_reload() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        catalog = ModelCatalog(root / "home")
        catalog.upsert(ProviderConfig(id="deepseek", name="DeepSeek", base_url="https://api.deepseek.com", models=["deepseek-chat"]), api_key="test-key")
        app = create_app(
            settings=NovelApplicationSettings(base_url="http://127.0.0.1:11434/v1", model="unconfigured"),
            database_url=f"sqlite+pysqlite:///{root / 'storyweaver.db'}",
            model_catalog=catalog,
        )
        with TestClient(app) as client:
            assert client.get("/api/v1/models/config").json()["providers"][0]["reasoning_levels_by_model"]["deepseek-chat"] == ["default", "off", "low", "high", "max"]
            session_id = client.post("/api/v1/sessions", json={}).json()["session_id"]
            result = client.put(f"/api/v1/sessions/{session_id}/reasoning", json={"level": "high"})
            assert result.status_code == 200, result.text
            assert client.get(f"/api/v1/sessions/{session_id}").json()["reasoning_level"] == "high"
            assert client.put(f"/api/v1/sessions/{session_id}/reasoning", json={"level": "medium"}).status_code == 422
            assert client.post("/api/v1/sessions", json={}).json()["session_id"] == session_id


def test_legacy_model_config_needs_no_migration() -> None:
    with TemporaryDirectory() as directory:
        catalog = ModelCatalog(Path(directory))
        catalog._write_yaml(catalog.config_path, {
            "version": 1, "providers": [{"id": "old", "name": "旧配置", "base_url": "http://localhost/v1", "models": ["chat"]}],
            "default_provider": "old", "default_model": "chat",
        })
        assert catalog.public()["embedding"] is None
        assert catalog.public()["providers"][0]["embedding_models"] == []
        assert catalog.selection()[1] == "chat"


def test_independent_embedding_credentials_survive_reload() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        catalog = ModelCatalog(root)
        catalog.upsert(ProviderConfig(id="remote", name="统一接口", base_url="https://example.invalid/v1",
                                     models=["chat"], embedding_models=["embed"]), api_key="shared-secret")
        selection = EmbeddingConfig(name="独立向量模型", base_url="https://vectors.invalid/v1", model_id="embed", dimensions=1024,
                                    timeout_seconds=3, minimum_similarity=0.5)
        public = catalog.set_embedding(selection, api_key="embedding-secret")
        assert public["default_model"] == "chat"
        assert "shared-secret" not in str(public)
        assert "embedding-secret" not in str(public)
        settings = ModelCatalog(root).embedding_settings()
        assert settings.model == "embed"
        assert settings.dimensions == 1024
        assert settings.base_url == "https://vectors.invalid/v1"
        assert settings.api_key == "embedding-secret"
        assert settings.timeout_seconds == 3
        assert settings.minimum_similarity == 0.5
        assert yaml.safe_load(catalog.credentials_path.read_text())["api_keys"] == {"remote": "shared-secret"}
        assert yaml.safe_load(catalog.credentials_path.read_text())["embedding_api_key"] == "embedding-secret"
        assert "shared-secret" not in catalog.config_path.read_text()
        assert "embedding-secret" not in catalog.config_path.read_text()


def test_embedding_can_be_configured_without_any_chat_provider() -> None:
    with TemporaryDirectory() as directory:
        catalog = ModelCatalog(Path(directory))
        catalog.set_embedding(EmbeddingConfig(base_url="http://localhost/v1", model_id="embed"))
        assert catalog.public()["default_provider"] is None
        assert catalog.public()["providers"] == []
        with pytest.raises(ModelConfigurationError):
            catalog.selection()
        catalog.upsert(ProviderConfig(id="chat", name="对话", base_url="http://localhost/v1", models=["chat"]))
        assert catalog.selection()[1] == "chat"
        catalog.delete("chat")
        assert catalog.public()["default_model"] is None
        assert catalog.public()["embedding"]["model_id"] == "embed"


def test_chat_edits_key_rotation_and_deletion_do_not_change_embedding() -> None:
    with TemporaryDirectory() as directory:
        catalog = ModelCatalog(Path(directory))
        provider = ProviderConfig(id="both", name="统一接口", base_url="http://localhost/v1", models=["chat"], embedding_models=["a", "b"])
        catalog.upsert(provider, api_key="secret")
        selection = EmbeddingConfig(base_url="https://vectors.invalid/v1", model_id="independent")
        catalog.set_embedding(selection, api_key="embedding-secret")
        catalog.upsert(provider.model_copy(update={"embedding_models": ["b"]}))
        assert catalog.public()["embedding"]["model_id"] == "independent"
        assert catalog.public()["default_model"] == "chat"
        catalog.upsert(provider, api_key="rotated-chat-secret")
        assert catalog.embedding_settings().api_key == "embedding-secret"
        catalog.set_embedding(selection, api_key="rotated-embedding-secret")
        assert catalog.selection()[2] == "rotated-chat-secret"
        catalog.delete("both")
        assert catalog.embedding_settings().model == "independent"
        assert catalog.embedding_settings().api_key == "rotated-embedding-secret"
        assert catalog._credentials() == {}


def test_embedding_validates_own_url_and_never_uses_chat_credentials() -> None:
    with TemporaryDirectory() as directory:
        catalog = ModelCatalog(Path(directory))
        catalog.upsert(ProviderConfig(id="remote", name="对话模型", base_url="https://example.invalid/v1", models=["chat"]), api_key="chat-secret")
        with pytest.raises(ValidationError):
            EmbeddingConfig(base_url="file:///tmp/model", model_id="embed")
        selection = EmbeddingConfig(base_url="https://example.invalid/v1", model_id="embed")
        catalog.set_embedding(selection)
        with pytest.raises(ModelConfigurationError):
            catalog.embedding_settings()
        catalog.set_embedding(selection, api_key="embedding-secret")
        assert catalog.public()["embedding"]["has_api_key"] is True
        catalog.set_embedding(selection, clear_key=True)
        with pytest.raises(ModelConfigurationError):
            catalog.embedding_settings()
        assert catalog.selection()[2] == "chat-secret"


def test_embedding_api_only_accepts_and_returns_model_configuration() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        catalog = ModelCatalog(root / "home")
        app = create_app(
            settings=NovelApplicationSettings(base_url="http://localhost/v1", model="unconfigured"),
            database_url=f"sqlite+pysqlite:///{root / 'storyweaver.db'}", model_catalog=catalog,
        )
        with TestClient(app) as client:
            selection = {"name": "独立向量模型", "base_url": "https://vectors.invalid/v1", "model_id": "embed", "api_key": "private"}
            saved = client.put("/api/v1/models/embedding", json=selection)
            assert saved.status_code == 200, saved.text
            assert "private" not in saved.text
            assert set(saved.json()["embedding"]) == {"name", "base_url", "model_id", "has_api_key"}
            assert client.get("/api/v1/models/config").json()["embedding"]["model_id"] == "embed"
            for values in ({"dimensions": 1024}, {"timeout_seconds": 5}, {"minimum_similarity": 0.35}, {"enabled": True}):
                assert client.put("/api/v1/models/embedding", json={**selection, **values}).status_code == 422
                assert client.post("/api/v1/models/embedding/test", json={**selection, **values}).status_code == 422
            assert client.put("/api/v1/models/embedding", json={**selection, "model_id": ""}).status_code == 422
            with patch("storyweaver.api.app.ApiEmbedder.embed", new=AsyncMock(return_value=[[1, 0]])) as embed:
                checked = client.post("/api/v1/models/embedding/test", json=selection)
                assert checked.status_code == 200, checked.text
                assert checked.json() == {"ok": True}
                assert embed.await_count == 1
            with patch("storyweaver.api.app.ApiEmbedder") as embedder:
                embedder.return_value.embed = AsyncMock(return_value=[[1, 0]])
                checked = client.post("/api/v1/models/embedding/test", json={
                    **selection, "base_url": "https://draft.invalid/v1", "api_key": "draft-secret",
                })
                assert checked.status_code == 200, checked.text
                settings = embedder.call_args.args[0]
                assert settings.base_url == "https://draft.invalid/v1"
                assert settings.api_key == "draft-secret"
                assert catalog.embedding_settings().api_key == "private"
            with patch("storyweaver.api.app.ApiEmbedder.embed", new=AsyncMock(side_effect=RuntimeError("private"))):
                failed = client.post("/api/v1/models/embedding/test", json=selection)
                assert failed.status_code == 400
                assert "private" not in failed.text
            disabled = client.put("/api/v1/models/embedding", content="null", headers={"Content-Type": "application/json"})
            assert disabled.status_code == 200, disabled.text
            assert disabled.json()["embedding"] is None
            assert "embedding_api_key" not in yaml.safe_load(catalog.credentials_path.read_text())


def test_previous_bound_embedding_config_is_expanded_once_and_detached() -> None:
    with TemporaryDirectory() as directory:
        catalog = ModelCatalog(Path(directory))
        catalog._write_yaml(catalog.config_path, {
            "version": 1, "providers": [{"id": "old", "name": "原供应商", "base_url": "https://old.invalid/v1", "models": ["chat"], "embedding_models": ["embed"]}],
            "default_provider": "old", "default_model": "chat",
            "embedding": {"provider_id": "old", "model_id": "embed", "dimensions": 2},
        })
        catalog._write_yaml(catalog.credentials_path, {"version": 1, "api_keys": {"old": "original-secret"}})
        public = catalog.public()
        assert public["embedding"]["base_url"] == "https://old.invalid/v1"
        assert "provider_id" not in public["embedding"]
        assert "original-secret" not in str(public)
        settings = catalog.embedding_settings()
        assert settings.api_key == "original-secret"
        stored = yaml.safe_load(catalog.config_path.read_text())
        assert stored["embedding"]["base_url"] == "https://old.invalid/v1"
        catalog.delete("old")
        assert ModelCatalog(Path(directory)).embedding_settings() == settings


def test_model_edits_preserve_internal_policy_and_reset_dimensions_for_new_models() -> None:
    with TemporaryDirectory() as directory:
        catalog = ModelCatalog(Path(directory))
        internal = EmbeddingConfig(base_url="http://localhost/v1", model_id="embed", dimensions=1024,
                                   timeout_seconds=3, minimum_similarity=0.5)
        catalog.set_embedding(internal)
        model = EmbeddingModelConfig(name="新显示名", base_url=internal.base_url, model_id=internal.model_id)
        public = catalog.set_embedding(model)
        settings = catalog.embedding_settings()
        assert (settings.dimensions, settings.timeout_seconds, settings.minimum_similarity) == (1024, 3, 0.5)
        assert set(public["embedding"]) == {"name", "base_url", "model_id", "has_api_key"}
        catalog.set_embedding(model.model_copy(update={"model_id": "new-embed"}))
        settings = catalog.embedding_settings()
        assert settings.dimensions is None
        assert settings.timeout_seconds == 5


def test_old_page_toggle_does_not_disable_a_configured_model() -> None:
    with TemporaryDirectory() as directory:
        catalog = ModelCatalog(Path(directory))
        catalog._write_yaml(catalog.config_path, {
            "version": 1, "embedding": {"enabled": False, "base_url": "http://localhost/v1", "model_id": "embed"},
        })
        assert catalog.embedding_settings().model == "embed"
        assert "enabled" not in catalog.public()["embedding"]
