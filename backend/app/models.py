from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ScopeType(str, Enum):
    file = "file"
    symbol = "symbol"


class TaskType(str, Enum):
    coding = "coding"
    image = "image"
    ml = "ml"
    web = "web"
    general = "general"


class SessionStatus(str, Enum):
    running = "running"
    completed = "completed"


class Target(BaseModel):
    scope_type: ScopeType = ScopeType.file
    scope_key: str = Field(min_length=1, examples=["src/auth.py", "src/auth.py::login"])


class WorkspaceCreate(BaseModel):
    name: str = Field(default="AI Workspace", min_length=1)


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    created_at: datetime


class PromptRequest(BaseModel):
    member_id: str = Field(min_length=1, examples=["Member A"])
    prompt: str = Field(min_length=1, examples=["Modify login() to use email verification"])
    task_type: TaskType = TaskType.coding
    targets: list[Target] = Field(default_factory=list)
    override_conflicts: bool = False


class ModelRoute(BaseModel):
    gateway: str = "TokenRouter"
    provider: str
    model: str
    reason: str


class LockRecord(BaseModel):
    workspace_id: str
    target: Target
    owner_session_id: str
    owner_member_id: str
    acquired_at: datetime
    lease_expires_at: datetime


class ConflictInfo(BaseModel):
    target: Target
    owner_session_id: str
    owner_member_id: str
    lease_expires_at: datetime


class AgentSession(BaseModel):
    id: str
    workspace_id: str
    member_id: str
    prompt: str
    task_type: TaskType
    route: ModelRoute
    targets: list[Target]
    status: SessionStatus
    created_at: datetime
    completed_at: datetime | None = None
    result_summary: str | None = None
    patch: str | None = None
    files: dict[str, str] = Field(default_factory=dict)


class ActionEvent(BaseModel):
    id: str
    workspace_id: str
    type: str
    message: str
    created_at: datetime
    session_id: str | None = None
    member_id: str | None = None
    target: Target | None = None


class PromptResponse(BaseModel):
    status: Literal["accepted", "conflict"]
    workspace_id: str
    session_id: str | None = None
    route: ModelRoute | None = None
    locks: list[LockRecord] = Field(default_factory=list)
    conflicts: list[ConflictInfo] = Field(default_factory=list)
    message: str


class CompleteSessionRequest(BaseModel):
    result_summary: str = "Session completed"
    patch: str | None = None
    files: dict[str, str] = Field(default_factory=dict)


class RunSessionRequest(BaseModel):
    instructions: str | None = Field(
        default=None,
        examples=["Return a concise implementation plan and a patch-style summary."],
    )
    max_tokens: int = Field(default=1200, ge=1, le=8192)
    temperature: float = Field(default=1.0, ge=0, le=2)
    use_web_research: bool | None = Field(
        default=None,
        description="Override automatic web/current-information detection.",
    )
    web_query: str | None = Field(
        default=None,
        description="Optional search query override for Bright Data research.",
    )
    max_web_results: int = Field(default=5, ge=1, le=10)
    fetch_web_pages: bool = Field(
        default=True,
        description="Fetch top result pages when BRIGHTDATA_UNLOCKER_ZONE is configured.",
    )


class WorkspaceState(BaseModel):
    workspace: WorkspaceResponse
    locks: list[LockRecord]
    sessions: list[AgentSession]
    events: list[ActionEvent]
    files: dict[str, str] = Field(default_factory=dict)
