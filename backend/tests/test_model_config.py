"""用户模型目录与会话选择的关键行为。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio

from fastapi.testclient import TestClient
from agents import ModelSettings

from storyweaver.api.app import create_app
from storyweaver.model_config import ModelCatalog
from storyweaver.model_config.store import ProviderConfig, RoutedModel, bind_model
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
