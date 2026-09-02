import asyncio
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel
from redis.asyncio import Redis

from app.model_router import route_model
from app.models import (
    ActionEvent,
    AgentSession,
    CompleteSessionRequest,
    ConflictInfo,
    LockRecord,
    PromptRequest,
    PromptResponse,
    SessionStatus,
    Target,
    WorkspaceCreate,
    WorkspaceResponse,
    WorkspaceState,
)


LEASE_SECONDS = 15 * 60
MUTEX_TTL_SECONDS = 30
MUTEX_BLOCK_SECONDS = 5

TModel = TypeVar("TModel", bound=BaseModel)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def target_key(target: Target) -> str:
    return f"{target.scope_type.value}:{target.scope_key}"


def dump_model(model: BaseModel) -> str:
    return model.model_dump_json()


def load_model(model_type: type[TModel], raw: str | bytes) -> TModel:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return model_type.model_validate_json(raw)


def new_workspace_code() -> str:
    return str(secrets.randbelow(900_000) + 100_000)


def normalize_workspace_files(files: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_path, content in files.items():
        path = raw_path.strip().replace("\\", "/").lstrip("/")
        if not path or any(part in {"", ".", ".."} for part in path.split("/")):
            continue
        normalized[path] = content
    return normalized


class WorkspaceStore(Protocol):
    name: str

    async def ping(self) -> bool:
        ...

    async def close(self) -> None:
        ...

    async def create_workspace(self, request: WorkspaceCreate) -> WorkspaceResponse:
        ...

    async def get_workspace(self, workspace_id: str) -> WorkspaceResponse | None:
        ...

    async def get_state(self, workspace_id: str) -> WorkspaceState | None:
        ...

    async def get_session(self, workspace_id: str, session_id: str) -> AgentSession | None:
        ...

    async def submit_prompt(self, workspace_id: str, request: PromptRequest) -> PromptResponse | None:
        ...

    async def complete_session(
        self,
        workspace_id: str,
        session_id: str,
        request: CompleteSessionRequest,
    ) -> AgentSession | None:
        ...

    async def list_events(self, workspace_id: str) -> list[ActionEvent] | None:
        ...


class StoreMutexTimeoutError(RuntimeError):
    pass


class InMemoryWorkspaceStore:
    name = "memory"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._workspaces: dict[str, WorkspaceResponse] = {}
        self._locks: dict[str, dict[str, LockRecord]] = {}
        self._sessions: dict[str, dict[str, AgentSession]] = {}
        self._events: dict[str, list[ActionEvent]] = {}
        self._files: dict[str, dict[str, str]] = {}

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def create_workspace(self, request: WorkspaceCreate) -> WorkspaceResponse:
        async with self._lock:
            workspace_id = new_workspace_code()
            while workspace_id in self._workspaces:
                workspace_id = new_workspace_code()
            workspace = WorkspaceResponse(id=workspace_id, name=request.name, created_at=now_utc())
            self._workspaces[workspace.id] = workspace
            self._locks[workspace.id] = {}
            self._sessions[workspace.id] = {}
            self._events[workspace.id] = []
            self._files[workspace.id] = {}
            self._append_event(
                workspace.id,
                event_type="workspace.created",
                message=f"Workspace '{workspace.name}' created.",
            )
            return workspace

    async def get_workspace(self, workspace_id: str) -> WorkspaceResponse | None:
        async with self._lock:
            return self._workspaces.get(workspace_id)

    async def get_state(self, workspace_id: str) -> WorkspaceState | None:
        async with self._lock:
            if workspace_id not in self._workspaces:
                return None

            self._expire_locks(workspace_id)
            return WorkspaceState(
                workspace=self._workspaces[workspace_id],
                locks=list(self._locks[workspace_id].values()),
                sessions=list(self._sessions[workspace_id].values()),
                events=self._events[workspace_id][-100:],
                files=dict(self._files[workspace_id]),
            )

    async def get_session(self, workspace_id: str, session_id: str) -> AgentSession | None:
        async with self._lock:
            if workspace_id not in self._workspaces:
                return None
            return self._sessions[workspace_id].get(session_id)

    async def submit_prompt(self, workspace_id: str, request: PromptRequest) -> PromptResponse | None:
        async with self._lock:
            if workspace_id not in self._workspaces:
                return None

            self._expire_locks(workspace_id)
            conflicts = self._find_conflicts(workspace_id, request.targets)
            if conflicts and not request.override_conflicts:
                self._append_event(
                    workspace_id,
                    event_type="prompt.conflict",
                    message=f"{request.member_id} was warned about {len(conflicts)} locked target(s).",
                    member_id=request.member_id,
                )
                return PromptResponse(
                    status="conflict",
                    workspace_id=workspace_id,
                    conflicts=conflicts,
                    message="One or more targets are currently locked. Wait or retry with override_conflicts=true.",
                )

            if conflicts and request.override_conflicts:
                for conflict in conflicts:
                    self._release_target(workspace_id, conflict.target)
                    self._append_event(
                        workspace_id,
                        event_type="lock.overridden",
                        message=(
                            f"{request.member_id} overrode {conflict.owner_member_id}'s lock "
                            f"on {conflict.target.scope_key}."
                        ),
                        member_id=request.member_id,
                        target=conflict.target,
                    )

            session_id = str(uuid4())
            route = route_model(request.task_type, request.prompt)
            session = AgentSession(
                id=session_id,
                workspace_id=workspace_id,
                member_id=request.member_id,
                prompt=request.prompt,
                task_type=request.task_type,
                route=route,
                targets=request.targets,
                status=SessionStatus.running,
                created_at=now_utc(),
            )
            self._sessions[workspace_id][session_id] = session

            acquired_locks = [self._acquire_lock(workspace_id, target, session) for target in request.targets]
            self._append_event(
                workspace_id,
                event_type="session.started",
                message=f"{request.member_id} started a {request.task_type.value} session routed to {route.provider}.",
                session_id=session_id,
                member_id=request.member_id,
            )

            return PromptResponse(
                status="accepted",
                workspace_id=workspace_id,
                session_id=session_id,
                route=route,
                locks=acquired_locks,
                message="Prompt accepted and session started.",
            )

    async def complete_session(
        self,
        workspace_id: str,
        session_id: str,
        request: CompleteSessionRequest,
    ) -> AgentSession | None:
        async with self._lock:
            if workspace_id not in self._workspaces:
                return None

            session = self._sessions[workspace_id].get(session_id)
            if session is None:
                return None

            session.status = SessionStatus.completed
            session.completed_at = now_utc()
            session.result_summary = request.result_summary
            session.patch = request.patch
            session.files = normalize_workspace_files(request.files)
            self._files[workspace_id].update(session.files)

            for target in session.targets:
                self._release_target(workspace_id, target)

            self._append_event(
                workspace_id,
                event_type="session.completed",
                message=f"{session.member_id} completed session {session.id}.",
                session_id=session.id,
                member_id=session.member_id,
            )
            return session

    async def list_events(self, workspace_id: str) -> list[ActionEvent] | None:
        async with self._lock:
            if workspace_id not in self._workspaces:
                return None
            return self._events[workspace_id][-100:]

    def _find_conflicts(self, workspace_id: str, targets: list[Target]) -> list[ConflictInfo]:
        conflicts: list[ConflictInfo] = []
        locks = self._locks[workspace_id]
        for target in targets:
            active_lock = locks.get(target_key(target))
            if active_lock is None:
                continue
            conflicts.append(
                ConflictInfo(
                    target=active_lock.target,
                    owner_session_id=active_lock.owner_session_id,
                    owner_member_id=active_lock.owner_member_id,
                    lease_expires_at=active_lock.lease_expires_at,
                )
            )
        return conflicts

    def _acquire_lock(self, workspace_id: str, target: Target, session: AgentSession) -> LockRecord:
        lock = LockRecord(
            workspace_id=workspace_id,
            target=target,
            owner_session_id=session.id,
            owner_member_id=session.member_id,
            acquired_at=now_utc(),
            lease_expires_at=now_utc() + timedelta(seconds=LEASE_SECONDS),
        )
        self._locks[workspace_id][target_key(target)] = lock
        self._append_event(
            workspace_id,
            event_type="lock.acquired",
            message=f"{session.member_id} locked {target.scope_key}.",
            session_id=session.id,
            member_id=session.member_id,
            target=target,
        )
        return lock

    def _release_target(self, workspace_id: str, target: Target) -> None:
        released = self._locks[workspace_id].pop(target_key(target), None)
        if released is None:
            return
        self._append_event(
            workspace_id,
            event_type="lock.released",
            message=f"{released.owner_member_id} released {target.scope_key}.",
            session_id=released.owner_session_id,
            member_id=released.owner_member_id,
            target=target,
        )

    def _expire_locks(self, workspace_id: str) -> None:
        cutoff = now_utc()
        expired_targets = [
            key for key, lock in self._locks[workspace_id].items() if lock.lease_expires_at <= cutoff
        ]
        for key in expired_targets:
            expired = self._locks[workspace_id].pop(key)
            self._append_event(
                workspace_id,
                event_type="lock.expired",
                message=f"Lock on {expired.target.scope_key} expired.",
                session_id=expired.owner_session_id,
                member_id=expired.owner_member_id,
                target=expired.target,
            )

    def _append_event(
        self,
        workspace_id: str,
        event_type: str,
        message: str,
        session_id: str | None = None,
        member_id: str | None = None,
        target: Target | None = None,
    ) -> None:
        self._events[workspace_id].append(
            ActionEvent(
                id=str(uuid4()),
                workspace_id=workspace_id,
                type=event_type,
                message=message,
                created_at=now_utc(),
                session_id=session_id,
                member_id=member_id,
                target=target,
            )
        )


class RedisWorkspaceStore:
    name = "redis"

    def __init__(self, redis: Redis, namespace: str = "ai-workspace") -> None:
        self._redis = redis
        self._namespace = namespace

    async def ping(self) -> bool:
        return bool(await self._redis.ping())

    async def close(self) -> None:
        await self._redis.aclose()

    async def create_workspace(self, request: WorkspaceCreate) -> WorkspaceResponse:
        workspace: WorkspaceResponse | None = None
        for _ in range(100):
            candidate = WorkspaceResponse(id=new_workspace_code(), name=request.name, created_at=now_utc())
            created = await self._redis.set(self._workspace_key(candidate.id), dump_model(candidate), nx=True)
            if created:
                workspace = candidate
                break
        if workspace is None:
            raise RuntimeError("Unable to allocate a unique workspace code.")
        await self._redis.sadd(self._workspaces_key(), workspace.id)
        await self._append_event(
            workspace.id,
            event_type="workspace.created",
            message=f"Workspace '{workspace.name}' created.",
        )
        return workspace

    async def get_workspace(self, workspace_id: str) -> WorkspaceResponse | None:
        raw = await self._redis.get(self._workspace_key(workspace_id))
        if raw is None:
            return None
        return load_model(WorkspaceResponse, raw)

    async def get_state(self, workspace_id: str) -> WorkspaceState | None:
        workspace = await self.get_workspace(workspace_id)
        if workspace is None:
            return None

        return WorkspaceState(
            workspace=workspace,
            locks=await self._active_locks(workspace_id),
            sessions=await self._list_sessions(workspace_id),
            events=await self._list_events(workspace_id),
            files=await self._list_files(workspace_id),
        )

    async def get_session(self, workspace_id: str, session_id: str) -> AgentSession | None:
        if await self.get_workspace(workspace_id) is None:
            return None

        raw_session = await self._redis.hget(self._sessions_key(workspace_id), session_id)
        if raw_session is None:
            return None
        return load_model(AgentSession, raw_session)

    async def submit_prompt(self, workspace_id: str, request: PromptRequest) -> PromptResponse | None:
        if await self.get_workspace(workspace_id) is None:
            return None

        token = await self._acquire_mutex(workspace_id)
        try:
            if await self.get_workspace(workspace_id) is None:
                return None

            conflicts = await self._find_conflicts(workspace_id, request.targets)
            if conflicts and not request.override_conflicts:
                await self._append_event(
                    workspace_id,
                    event_type="prompt.conflict",
                    message=f"{request.member_id} was warned about {len(conflicts)} locked target(s).",
                    member_id=request.member_id,
                )
                return PromptResponse(
                    status="conflict",
                    workspace_id=workspace_id,
                    conflicts=conflicts,
                    message="One or more targets are currently locked. Wait or retry with override_conflicts=true.",
                )

            if conflicts and request.override_conflicts:
                for conflict in conflicts:
                    await self._release_target(workspace_id, conflict.target)
                    await self._append_event(
                        workspace_id,
                        event_type="lock.overridden",
                        message=(
                            f"{request.member_id} overrode {conflict.owner_member_id}'s lock "
                            f"on {conflict.target.scope_key}."
                        ),
                        member_id=request.member_id,
                        target=conflict.target,
                    )

            session_id = str(uuid4())
            route = route_model(request.task_type, request.prompt)
            session = AgentSession(
                id=session_id,
                workspace_id=workspace_id,
                member_id=request.member_id,
                prompt=request.prompt,
                task_type=request.task_type,
                route=route,
                targets=request.targets,
                status=SessionStatus.running,
                created_at=now_utc(),
            )
            await self._redis.hset(self._sessions_key(workspace_id), session_id, dump_model(session))

            acquired_locks = []
            for target in request.targets:
                acquired_locks.append(await self._acquire_lock(workspace_id, target, session))

            await self._append_event(
                workspace_id,
                event_type="session.started",
                message=f"{request.member_id} started a {request.task_type.value} session routed to {route.provider}.",
                session_id=session_id,
                member_id=request.member_id,
            )

            return PromptResponse(
                status="accepted",
                workspace_id=workspace_id,
                session_id=session_id,
                route=route,
                locks=acquired_locks,
                message="Prompt accepted and session started.",
            )
        finally:
            await self._release_mutex(workspace_id, token)

    async def complete_session(
        self,
        workspace_id: str,
        session_id: str,
        request: CompleteSessionRequest,
    ) -> AgentSession | None:
        if await self.get_workspace(workspace_id) is None:
            return None

        token = await self._acquire_mutex(workspace_id)
        try:
            raw_session = await self._redis.hget(self._sessions_key(workspace_id), session_id)
            if raw_session is None:
                return None

            session = load_model(AgentSession, raw_session)
            session.status = SessionStatus.completed
            session.completed_at = now_utc()
            session.result_summary = request.result_summary
            session.patch = request.patch
            session.files = normalize_workspace_files(request.files)
            await self._redis.hset(self._sessions_key(workspace_id), session.id, dump_model(session))
            if session.files:
                await self._redis.hset(self._files_key(workspace_id), mapping=session.files)

            for target in session.targets:
                await self._release_target(workspace_id, target)

            await self._append_event(
                workspace_id,
                event_type="session.completed",
                message=f"{session.member_id} completed session {session.id}.",
                session_id=session.id,
                member_id=session.member_id,
            )
            return session
        finally:
            await self._release_mutex(workspace_id, token)

    async def list_events(self, workspace_id: str) -> list[ActionEvent] | None:
        if await self.get_workspace(workspace_id) is None:
            return None
        return await self._list_events(workspace_id)

    async def _find_conflicts(self, workspace_id: str, targets: list[Target]) -> list[ConflictInfo]:
        conflicts: list[ConflictInfo] = []
        for target in targets:
            raw_lock = await self._redis.get(self._lock_key(workspace_id, target_key(target)))
            if raw_lock is None:
                await self._redis.srem(self._lock_index_key(workspace_id), target_key(target))
                continue

            active_lock = load_model(LockRecord, raw_lock)
            conflicts.append(
                ConflictInfo(
                    target=active_lock.target,
                    owner_session_id=active_lock.owner_session_id,
                    owner_member_id=active_lock.owner_member_id,
                    lease_expires_at=active_lock.lease_expires_at,
                )
            )
        return conflicts

    async def _acquire_lock(self, workspace_id: str, target: Target, session: AgentSession) -> LockRecord:
        lock = LockRecord(
            workspace_id=workspace_id,
            target=target,
            owner_session_id=session.id,
            owner_member_id=session.member_id,
            acquired_at=now_utc(),
            lease_expires_at=now_utc() + timedelta(seconds=LEASE_SECONDS),
        )
        key = target_key(target)
        await self._redis.set(self._lock_key(workspace_id, key), dump_model(lock), ex=LEASE_SECONDS)
        await self._redis.sadd(self._lock_index_key(workspace_id), key)
        await self._append_event(
            workspace_id,
            event_type="lock.acquired",
            message=f"{session.member_id} locked {target.scope_key}.",
            session_id=session.id,
            member_id=session.member_id,
            target=target,
        )
        return lock

    async def _release_target(self, workspace_id: str, target: Target) -> None:
        key = target_key(target)
        raw_lock = await self._redis.get(self._lock_key(workspace_id, key))
        await self._redis.delete(self._lock_key(workspace_id, key))
        await self._redis.srem(self._lock_index_key(workspace_id), key)
        if raw_lock is None:
            return

        released = load_model(LockRecord, raw_lock)
        await self._append_event(
            workspace_id,
            event_type="lock.released",
            message=f"{released.owner_member_id} released {target.scope_key}.",
            session_id=released.owner_session_id,
            member_id=released.owner_member_id,
            target=target,
        )

    async def _active_locks(self, workspace_id: str) -> list[LockRecord]:
        lock_keys = await self._redis.smembers(self._lock_index_key(workspace_id))
        locks: list[LockRecord] = []
        stale_keys: list[str] = []

        for key in lock_keys:
            raw_lock = await self._redis.get(self._lock_key(workspace_id, key))
            if raw_lock is None:
                stale_keys.append(key)
                continue
            locks.append(load_model(LockRecord, raw_lock))

        if stale_keys:
            await self._redis.srem(self._lock_index_key(workspace_id), *stale_keys)

        return locks

    async def _list_sessions(self, workspace_id: str) -> list[AgentSession]:
        raw_sessions = await self._redis.hvals(self._sessions_key(workspace_id))
        return [load_model(AgentSession, raw_session) for raw_session in raw_sessions]

    async def _list_events(self, workspace_id: str) -> list[ActionEvent]:
        raw_events = await self._redis.lrange(self._events_key(workspace_id), -100, -1)
        return [load_model(ActionEvent, raw_event) for raw_event in raw_events]

    async def _list_files(self, workspace_id: str) -> dict[str, str]:
        files = await self._redis.hgetall(self._files_key(workspace_id))
        return {str(path): str(content) for path, content in files.items()}

    async def _append_event(
        self,
        workspace_id: str,
        event_type: str,
        message: str,
        session_id: str | None = None,
        member_id: str | None = None,
        target: Target | None = None,
    ) -> None:
        event = ActionEvent(
            id=str(uuid4()),
            workspace_id=workspace_id,
            type=event_type,
            message=message,
            created_at=now_utc(),
            session_id=session_id,
            member_id=member_id,
            target=target,
        )
        await self._redis.rpush(self._events_key(workspace_id), dump_model(event))
        await self._redis.ltrim(self._events_key(workspace_id), -100, -1)

    async def _acquire_mutex(self, workspace_id: str) -> str:
        key = self._mutex_key(workspace_id)
        token = str(uuid4())
        deadline = time.monotonic() + MUTEX_BLOCK_SECONDS

        while time.monotonic() < deadline:
            acquired = await self._redis.set(key, token, nx=True, ex=MUTEX_TTL_SECONDS)
            if acquired:
                return token
            await asyncio.sleep(0.05)

        raise StoreMutexTimeoutError(f"Timed out waiting for Redis mutex for workspace {workspace_id}.")

    async def _release_mutex(self, workspace_id: str, token: str) -> None:
        key = self._mutex_key(workspace_id)
        current_token = await self._redis.get(key)
        if current_token == token:
            await self._redis.delete(key)

    def _key(self, *parts: str) -> str:
        return ":".join((self._namespace, *parts))

    def _workspaces_key(self) -> str:
        return self._key("workspaces")

    def _workspace_key(self, workspace_id: str) -> str:
        return self._key("workspace", workspace_id)

    def _sessions_key(self, workspace_id: str) -> str:
        return self._key("workspace", workspace_id, "sessions")

    def _events_key(self, workspace_id: str) -> str:
        return self._key("workspace", workspace_id, "events")

    def _files_key(self, workspace_id: str) -> str:
        return self._key("workspace", workspace_id, "files")

    def _lock_index_key(self, workspace_id: str) -> str:
        return self._key("workspace", workspace_id, "lock-index")

    def _lock_key(self, workspace_id: str, lock_target_key: str) -> str:
        return self._key("workspace", workspace_id, "lock", lock_target_key)

    def _mutex_key(self, workspace_id: str) -> str:
        return self._key("workspace", workspace_id, "mutex")
