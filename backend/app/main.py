import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from app.brightdata import (
    BrightDataAPIError,
    BrightDataConfigurationError,
    WebResearcher,
    should_use_web_research,
)
from app.kimi import KimiAPIError, KimiConfigurationError, KimiProvider
from app.models import (
    ActionEvent,
    AgentSession,
    CompleteSessionRequest,
    PromptRequest,
    PromptResponse,
    RunSessionRequest,
    WorkspaceCreate,
    WorkspaceResponse,
    WorkspaceState,
)
from app.session_runner import build_model_messages, parse_model_result
from app.settings import (
    build_fallback_model_client_from_env,
    build_model_client_from_env,
    build_store_from_env,
    build_web_researcher_from_env,
)
from app.store import StoreMutexTimeoutError, WorkspaceStore
from app.tokenrouter import TokenRouterAPIError, TokenRouterConfigurationError


class WebSocketHub:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._connection_members: dict[WebSocket, str] = {}
        self._members: dict[str, dict[str, int]] = {}

    async def connect(self, workspace_id: str, websocket: WebSocket, member_id: str) -> None:
        await websocket.accept()
        self._connections.setdefault(workspace_id, set()).add(websocket)
        self._connection_members[websocket] = member_id
        members = self._members.setdefault(workspace_id, {})
        members[member_id] = members.get(member_id, 0) + 1

    def disconnect(self, workspace_id: str, websocket: WebSocket, member_id: str) -> None:
        connections = self._connections.get(workspace_id, set())
        if websocket not in connections:
            return

        connections.discard(websocket)
        registered_member = self._connection_members.pop(websocket, member_id)
        members = self._members.get(workspace_id, {})
        if registered_member in members:
            members[registered_member] -= 1
            if members[registered_member] <= 0:
                members.pop(registered_member, None)
        if not connections:
            self._connections.pop(workspace_id, None)
        if not members:
            self._members.pop(workspace_id, None)

    def members(self, workspace_id: str) -> list[str]:
        return sorted(self._members.get(workspace_id, {}))

    async def broadcast(self, workspace_id: str, payload: dict) -> None:
        stale_connections: list[WebSocket] = []
        for websocket in self._connections.get(workspace_id, set()):
            try:
                await websocket.send_json(payload)
            except (RuntimeError, WebSocketDisconnect):
                stale_connections.append(websocket)

        for websocket in stale_connections:
            member_id = self._connection_members.get(websocket, "Member")
            self.disconnect(workspace_id, websocket, member_id)


def create_app(
    workspace_store: WorkspaceStore | None = None,
    model_client: KimiProvider | None = None,
    fallback_model_client: KimiProvider | None = None,
    kimi_client: KimiProvider | None = None,
    web_researcher: WebResearcher | None = None,
) -> FastAPI:
    store = workspace_store or build_store_from_env()
    model_gateway = model_client or kimi_client or build_model_client_from_env()
    fallback_gateway = fallback_model_client or build_fallback_model_client_from_env(model_gateway.name)
    research_gateway = web_researcher or build_web_researcher_from_env()
    hub = WebSocketHub()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await store.close()

    app = FastAPI(title="AI Workspace Backend", version="0.1.0", lifespan=lifespan)
    configured_origins = os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:3000,http://localhost:3000,http://127.0.0.1:5173,http://localhost:5173",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in configured_origins.split(",") if origin.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    async def run_with_model_fallback(
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        try:
            return await model_gateway.chat(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except TokenRouterConfigurationError as exc:
            if fallback_gateway is None:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        except TokenRouterAPIError as exc:
            if fallback_gateway is None or not exc.is_fallback_eligible:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        except KimiConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        except KimiAPIError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        else:
            raise AssertionError("Unreachable")

        try:
            return await fallback_gateway.chat(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except KimiConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        except KimiAPIError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"TokenRouter failed and direct Kimi fallback also failed: {exc}",
            ) from exc

    @app.exception_handler(RedisError)
    async def redis_exception_handler(_: object, exc: RedisError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": (
                    f"{store.name} store unavailable. Start Redis or run with "
                    "AI_WORKSPACE_STORE=memory for local testing."
                ),
                "error": exc.__class__.__name__,
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        try:
            await store.ping()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"{store.name} store unavailable.",
            ) from exc

        return {
            "status": "ok",
            "store": store.name,
            "model_gateway": model_gateway.name,
            "web_research": research_gateway.name,
        }

    @app.post("/workspaces", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
    async def create_workspace(request: WorkspaceCreate) -> WorkspaceResponse:
        return await store.create_workspace(request)

    @app.get("/workspaces/{workspace_id}/state", response_model=WorkspaceState)
    async def get_workspace_state(workspace_id: str) -> WorkspaceState:
        state = await store.get_state(workspace_id)
        if state is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
        return state

    @app.post("/workspaces/{workspace_id}/prompts", response_model=PromptResponse)
    async def submit_prompt(workspace_id: str, request: PromptRequest) -> JSONResponse:
        try:
            result = await store.submit_prompt(workspace_id, request)
        except StoreMutexTimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Workspace is busy. Retry the request shortly.",
            ) from exc

        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")

        response_status = status.HTTP_409_CONFLICT if result.status == "conflict" else status.HTTP_202_ACCEPTED
        await hub.broadcast(workspace_id, {"type": "prompt.updated", "payload": jsonable_encoder(result)})
        return JSONResponse(status_code=response_status, content=jsonable_encoder(result))

    @app.post(
        "/workspaces/{workspace_id}/sessions/{session_id}/complete",
        response_model=AgentSession,
    )
    async def complete_session(
        workspace_id: str,
        session_id: str,
        request: CompleteSessionRequest,
    ) -> AgentSession:
        try:
            session = await store.complete_session(workspace_id, session_id, request)
        except StoreMutexTimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Workspace is busy. Retry the request shortly.",
            ) from exc

        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace or session not found.")

        await hub.broadcast(workspace_id, {"type": "session.completed", "payload": jsonable_encoder(session)})
        return session

    @app.post(
        "/workspaces/{workspace_id}/sessions/{session_id}/run",
        response_model=AgentSession,
    )
    async def run_session(
        workspace_id: str,
        session_id: str,
        request: RunSessionRequest,
    ) -> AgentSession:
        session = await store.get_session(workspace_id, session_id)
        state = await store.get_state(workspace_id)
        if session is None or state is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace or session not found.")

        if "KimiAI" not in session.route.provider:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is routed to {session.route.provider}, not KimiAI.",
            )

        web_research = None
        if should_use_web_research(session.task_type, session.prompt, request.use_web_research):
            try:
                web_research = await research_gateway.research(
                    prompt=session.prompt,
                    max_results=request.max_web_results,
                    fetch_pages=request.fetch_web_pages,
                    query_override=request.web_query,
                )
            except BrightDataConfigurationError as exc:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
            except BrightDataAPIError as exc:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        messages = build_model_messages(session, state, request, web_research=web_research)
        raw_result = await run_with_model_fallback(
            messages=messages,
            model=session.route.model,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )
        result_summary, files, patch = parse_model_result(raw_result)

        completed = await store.complete_session(
            workspace_id,
            session_id,
            CompleteSessionRequest(result_summary=result_summary, patch=patch, files=files),
        )
        if completed is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace or session not found.")

        await hub.broadcast(workspace_id, {"type": "session.completed", "payload": jsonable_encoder(completed)})
        return completed

    @app.get("/workspaces/{workspace_id}/events", response_model=list[ActionEvent])
    async def list_events(workspace_id: str) -> list[ActionEvent]:
        events = await store.list_events(workspace_id)
        if events is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
        return events

    @app.websocket("/workspaces/{workspace_id}/ws")
    async def workspace_websocket(
        workspace_id: str,
        websocket: WebSocket,
        member_id: str = "Member",
    ) -> None:
        workspace = await store.get_workspace(workspace_id)
        if workspace is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        clean_member_id = member_id.strip() or "Member"
        await hub.connect(workspace_id, websocket, clean_member_id)
        try:
            await websocket.send_json(
                {
                    "type": "connected",
                    "workspace_id": workspace_id,
                    "members": hub.members(workspace_id),
                }
            )
            await hub.broadcast(
                workspace_id,
                {"type": "presence.updated", "members": hub.members(workspace_id)},
            )
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.disconnect(workspace_id, websocket, clean_member_id)
            await hub.broadcast(
                workspace_id,
                {"type": "presence.updated", "members": hub.members(workspace_id)},
            )

    return app


app = create_app()
