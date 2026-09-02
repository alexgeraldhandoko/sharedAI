import json
import re

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app.brightdata import SearchResult, WebResearchContext
from app.main import create_app
from app.store import InMemoryWorkspaceStore
from app.tokenrouter import TokenRouterAPIError


class FakeKimiClient:
    name = "kimi"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 1200,
        temperature: float = 1.0,
    ) -> str:
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return "Kimi generated a patch plan for function_login()."


class FakeStructuredKimiClient(FakeKimiClient):
    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 1200,
        temperature: float = 1.0,
    ) -> str:
        await super().chat(messages, model, max_tokens, temperature)
        return json.dumps(
            {
                "assistant_message": "Created the calculator module.",
                "files": {"src/calculator.py": "def add(a, b):\n    return a + b\n"},
            }
        )


class BrokenStore(InMemoryWorkspaceStore):
    name = "redis"

    async def create_workspace(self, request):  # type: ignore[no-untyped-def]
        raise ConnectionError("Redis is down")


class FakeWebResearcher:
    name = "brightdata"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def research(
        self,
        prompt: str,
        max_results: int = 5,
        fetch_pages: bool = True,
        query_override: str | None = None,
    ) -> WebResearchContext:
        self.calls.append(
            {
                "prompt": prompt,
                "max_results": max_results,
                "fetch_pages": fetch_pages,
                "query_override": query_override,
            }
        )
        return WebResearchContext(
            query=query_override or prompt,
            results=[
                SearchResult(
                    title="Current Kimi model docs",
                    url="https://example.com/kimi-models",
                    description="Kimi has a current code model.",
                    rank=1,
                    content="Kimi current model evidence from Bright Data.",
                )
            ],
        )


class FakeFailingTokenRouter:
    name = "tokenrouter"

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 1200,
        temperature: float = 1.0,
    ) -> str:
        raise TokenRouterAPIError(
            "TokenRouter returned HTTP 503: model_not_found",
            status_code=503,
            response_text='{"error":{"code":"model_not_found","message":"No available channel for model kimi-k2.7-code"}}',
        )


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app(InMemoryWorkspaceStore())) as test_client:
        yield test_client


@pytest.fixture
def kimi_client() -> tuple[TestClient, FakeKimiClient]:
    fake_kimi = FakeKimiClient()
    with TestClient(create_app(InMemoryWorkspaceStore(), kimi_client=fake_kimi)) as test_client:
        yield test_client, fake_kimi


@pytest.fixture
def web_client() -> tuple[TestClient, FakeKimiClient, FakeWebResearcher]:
    fake_kimi = FakeKimiClient()
    fake_web = FakeWebResearcher()
    with TestClient(
        create_app(InMemoryWorkspaceStore(), model_client=fake_kimi, web_researcher=fake_web)
    ) as test_client:
        yield test_client, fake_kimi, fake_web


@pytest.fixture
def fallback_client() -> tuple[TestClient, FakeKimiClient]:
    fallback_kimi = FakeKimiClient()
    fake_web = FakeWebResearcher()
    with TestClient(
        create_app(
            InMemoryWorkspaceStore(),
            model_client=FakeFailingTokenRouter(),
            fallback_model_client=fallback_kimi,
            web_researcher=fake_web,
        )
    ) as test_client:
        yield test_client, fallback_kimi


def create_workspace(client: TestClient) -> str:
    response = client.post("/workspaces", json={"name": "Hackathon Workspace"})
    assert response.status_code == 201
    return response.json()["id"]


def test_workspace_uses_shareable_six_digit_code(client: TestClient) -> None:
    workspace_id = create_workspace(client)

    assert re.fullmatch(r"\d{6}", workspace_id)


def test_local_frontend_origin_is_allowed(client: TestClient) -> None:
    response = client.options(
        "/workspaces",
        headers={
            "Origin": "http://127.0.0.1:3000",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"


def test_websocket_reports_connected_member(client: TestClient) -> None:
    workspace_id = create_workspace(client)

    with client.websocket_connect(f"/workspaces/{workspace_id}/ws?member_id=Alex") as websocket:
        connected = websocket.receive_json()
        presence = websocket.receive_json()

    assert connected == {"type": "connected", "workspace_id": workspace_id, "members": ["Alex"]}
    assert presence == {"type": "presence.updated", "members": ["Alex"]}


def test_health_check(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "store": "memory",
        "model_gateway": "kimi",
        "web_research": "brightdata",
    }


def test_redis_errors_return_clear_503() -> None:
    with TestClient(create_app(BrokenStore())) as test_client:
        response = test_client.post("/workspaces", json={"name": "Kimi Test"})

    assert response.status_code == 503
    assert response.json()["error"] == "ConnectionError"
    assert "Start Redis" in response.json()["detail"]


def test_prompt_acquires_lock_and_updates_workspace_state(client: TestClient) -> None:
    workspace_id = create_workspace(client)

    response = client.post(
        f"/workspaces/{workspace_id}/prompts",
        json={
            "member_id": "Member A",
            "prompt": "modify function_login()",
            "task_type": "coding",
            "targets": [{"scope_type": "symbol", "scope_key": "src/auth.py::function_login"}],
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["route"]["provider"] == "KimiAI"
    assert body["route"]["gateway"] == "Kimi"
    assert body["route"]["model"] == "kimi-k2.7-code"
    assert body["locks"][0]["owner_member_id"] == "Member A"

    state = client.get(f"/workspaces/{workspace_id}/state").json()
    assert len(state["locks"]) == 1
    assert state["locks"][0]["target"]["scope_key"] == "src/auth.py::function_login"
    assert state["sessions"][0]["status"] == "running"


def test_conflicting_prompt_returns_warning_before_proceeding(client: TestClient) -> None:
    workspace_id = create_workspace(client)
    target = {"scope_type": "symbol", "scope_key": "src/auth.py::function_login"}

    first = client.post(
        f"/workspaces/{workspace_id}/prompts",
        json={"member_id": "Member A", "prompt": "modify login", "targets": [target]},
    )
    assert first.status_code == 202

    conflict = client.post(
        f"/workspaces/{workspace_id}/prompts",
        json={"member_id": "Member B", "prompt": "also modify login", "targets": [target]},
    )

    assert conflict.status_code == 409
    body = conflict.json()
    assert body["status"] == "conflict"
    assert body["conflicts"][0]["owner_member_id"] == "Member A"
    assert body["conflicts"][0]["target"] == target


def test_override_replaces_existing_lock(client: TestClient) -> None:
    workspace_id = create_workspace(client)
    target = {"scope_type": "file", "scope_key": "src/auth.py"}

    first = client.post(
        f"/workspaces/{workspace_id}/prompts",
        json={"member_id": "Member A", "prompt": "modify auth file", "targets": [target]},
    )
    assert first.status_code == 202

    override = client.post(
        f"/workspaces/{workspace_id}/prompts",
        json={
            "member_id": "Member B",
            "prompt": "urgent auth file update",
            "targets": [target],
            "override_conflicts": True,
        },
    )

    assert override.status_code == 202
    state = client.get(f"/workspaces/{workspace_id}/state").json()
    assert len(state["locks"]) == 1
    assert state["locks"][0]["owner_member_id"] == "Member B"


def test_completing_session_releases_locks(client: TestClient) -> None:
    workspace_id = create_workspace(client)
    target = {"scope_type": "file", "scope_key": "classifier.py"}

    prompt = client.post(
        f"/workspaces/{workspace_id}/prompts",
        json={
            "member_id": "Member C",
            "prompt": "train classifier",
            "task_type": "ml",
            "targets": [target],
        },
    )
    assert prompt.status_code == 202
    session_id = prompt.json()["session_id"]
    assert prompt.json()["route"]["provider"] == "Nosana"

    complete = client.post(
        f"/workspaces/{workspace_id}/sessions/{session_id}/complete",
        json={"result_summary": "Classifier trained in sandbox.", "patch": "diff --git ..."},
    )

    assert complete.status_code == 200
    assert complete.json()["status"] == "completed"

    state = client.get(f"/workspaces/{workspace_id}/state").json()
    assert state["locks"] == []
    assert state["sessions"][0]["result_summary"] == "Classifier trained in sandbox."


def test_running_kimi_session_calls_provider_and_completes_session(
    kimi_client: tuple[TestClient, FakeKimiClient],
) -> None:
    client, fake_kimi = kimi_client
    workspace_id = create_workspace(client)
    target = {"scope_type": "symbol", "scope_key": "src/auth.py::function_login"}

    prompt = client.post(
        f"/workspaces/{workspace_id}/prompts",
        json={
            "member_id": "Member A",
            "prompt": "modify function_login() to require MFA",
            "task_type": "coding",
            "targets": [target],
        },
    )
    assert prompt.status_code == 202
    session_id = prompt.json()["session_id"]

    run = client.post(
        f"/workspaces/{workspace_id}/sessions/{session_id}/run",
        json={"instructions": "Return a concise patch plan.", "max_tokens": 500, "temperature": 0.1},
    )

    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "completed"
    assert body["result_summary"] == "Kimi generated a patch plan for function_login()."

    assert fake_kimi.calls[0]["model"] == "kimi-k2.7-code"
    assert fake_kimi.calls[0]["max_tokens"] == 500
    assert "modify function_login()" in fake_kimi.calls[0]["messages"][1]["content"]

    state = client.get(f"/workspaces/{workspace_id}/state").json()
    assert state["locks"] == []


def test_structured_model_output_updates_shared_files() -> None:
    fake_kimi = FakeStructuredKimiClient()
    with TestClient(create_app(InMemoryWorkspaceStore(), model_client=fake_kimi)) as client:
        workspace_id = create_workspace(client)
        prompt = client.post(
            f"/workspaces/{workspace_id}/prompts",
            json={
                "member_id": "Alex",
                "prompt": "Create src/calculator.py",
                "task_type": "coding",
                "targets": [{"scope_type": "file", "scope_key": "src/calculator.py"}],
            },
        )
        session_id = prompt.json()["session_id"]
        run = client.post(f"/workspaces/{workspace_id}/sessions/{session_id}/run", json={})
        state = client.get(f"/workspaces/{workspace_id}/state").json()

    assert run.status_code == 200
    assert run.json()["files"] == {"src/calculator.py": "def add(a, b):\n    return a + b\n"}
    assert state["files"] == run.json()["files"]


def test_web_prompt_uses_brightdata_context_before_model_call(
    web_client: tuple[TestClient, FakeKimiClient, FakeWebResearcher],
) -> None:
    client, fake_kimi, fake_web = web_client
    workspace_id = create_workspace(client)

    prompt = client.post(
        f"/workspaces/{workspace_id}/prompts",
        json={
            "member_id": "Member A",
            "prompt": "Research the latest Kimi code model and summarize it.",
            "task_type": "web",
            "targets": [],
        },
    )
    assert prompt.status_code == 202
    session_id = prompt.json()["session_id"]

    run = client.post(
        f"/workspaces/{workspace_id}/sessions/{session_id}/run",
        json={
            "instructions": "Use web evidence.",
            "max_tokens": 500,
            "temperature": 1,
            "max_web_results": 3,
            "web_query": "latest Kimi code model",
        },
    )

    assert run.status_code == 200
    assert fake_web.calls[0]["query_override"] == "latest Kimi code model"
    assert fake_web.calls[0]["max_results"] == 3
    assert "web_research" in fake_kimi.calls[0]["messages"][1]["content"]
    assert "https://example.com/kimi-models" in fake_kimi.calls[0]["messages"][1]["content"]


def test_run_session_falls_back_to_direct_kimi_when_tokenrouter_model_is_unavailable(
    fallback_client: tuple[TestClient, FakeKimiClient],
) -> None:
    client, fallback_kimi = fallback_client
    workspace_id = create_workspace(client)

    prompt = client.post(
        f"/workspaces/{workspace_id}/prompts",
        json={
            "member_id": "Member A",
            "prompt": "Research the latest Kimi code model and summarize it.",
            "task_type": "web",
            "targets": [],
        },
    )
    assert prompt.status_code == 202

    session_id = prompt.json()["session_id"]
    run = client.post(
        f"/workspaces/{workspace_id}/sessions/{session_id}/run",
        json={"instructions": "Use web evidence.", "max_tokens": 500, "temperature": 1},
    )

    assert run.status_code == 200
    assert run.json()["status"] == "completed"
    assert run.json()["result_summary"] == "Kimi generated a patch plan for function_login()."
    assert fallback_kimi.calls[0]["model"] == "kimi-k2.7-code"
