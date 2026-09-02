import json
import re

from app.brightdata import WebResearchContext
from app.models import AgentSession, RunSessionRequest, WorkspaceState


KIMI_SYSTEM_PROMPT = """You are KimiAI inside a collaborative AI coding workspace.
You help implement code changes while respecting shared locks and active teammates.
Use the provided workspace context. Be concise, specific, and implementation-focused.
When web_research is provided, treat it as the source of truth for current web information and cite URLs from it.
For coding requests, return a JSON object with assistant_message, files, and optional patch fields.
The files field must be an object mapping safe relative paths to complete file contents.
For non-coding requests, files may be an empty object.
Do not wrap the JSON in Markdown fences.
If you cannot safely infer a change, explain what information is missing in assistant_message."""


def parse_model_result(raw_result: str) -> tuple[str, dict[str, str], str | None]:
    """Parse structured agent output while preserving compatibility with plain-text providers."""
    candidate = raw_result.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return raw_result, {}, None

    if not isinstance(payload, dict):
        return raw_result, {}, None

    message = payload.get("assistant_message") or payload.get("explanation") or payload.get("message")
    files_payload = payload.get("files", {})
    files = {}
    if isinstance(files_payload, dict):
        files = {
            path: content
            for path, content in files_payload.items()
            if isinstance(path, str) and isinstance(content, str)
        }
    patch = payload.get("patch")
    return str(message or "Session completed."), files, str(patch) if patch is not None else None


def build_model_messages(
    session: AgentSession,
    state: WorkspaceState,
    request: RunSessionRequest,
    web_research: WebResearchContext | None = None,
) -> list[dict[str, str]]:
    context = {
        "workspace": state.workspace.model_dump(mode="json"),
        "current_session": session.model_dump(mode="json"),
        "active_locks": [lock.model_dump(mode="json") for lock in state.locks],
        "recent_events": [event.model_dump(mode="json") for event in state.events[-20:]],
        "web_research": web_research.model_dump(mode="json") if web_research else None,
    }

    user_content = {
        "member_prompt": session.prompt,
        "task_type": session.task_type.value,
        "targets": [target.model_dump(mode="json") for target in session.targets],
        "extra_instructions": request.instructions,
        "workspace_context": context,
    }

    return [
        {"role": "system", "content": KIMI_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_content, indent=2)},
    ]


build_kimi_messages = build_model_messages
