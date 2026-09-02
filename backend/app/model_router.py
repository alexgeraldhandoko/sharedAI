import os

from app.kimi import DEFAULT_KIMI_MODEL
from app.models import ModelRoute, TaskType


DEFAULT_GENERAL_MODEL = "kimi-k2.6"


def configured_gateway_name() -> str:
    gateway = os.getenv("MODEL_GATEWAY")
    resolved_gateway = (
        gateway.lower()
        if gateway
        else "tokenrouter"
        if os.getenv("TOKENROUTER_API_KEY")
        else "kimi"
    )
    return "Kimi" if resolved_gateway in {"kimi", "kimiai", "moonshot"} else "TokenRouter"


def configured_model_for_task(task_type: TaskType) -> str:
    if task_type == TaskType.web:
        return os.getenv("AI_WORKSPACE_WEB_MODEL", DEFAULT_KIMI_MODEL)
    if task_type == TaskType.coding:
        return os.getenv("AI_WORKSPACE_CODING_MODEL", DEFAULT_KIMI_MODEL)
    return os.getenv("AI_WORKSPACE_GENERAL_MODEL", DEFAULT_GENERAL_MODEL)


def route_model(task_type: TaskType, prompt: str) -> ModelRoute:
    """Route requests to the provider that best matches the task type."""
    normalized_prompt = prompt.lower()
    gateway = configured_gateway_name()

    if task_type == TaskType.ml:
        return ModelRoute(
            gateway=gateway,
            provider="Nosana",
            model="gpu-job",
            reason="GPU-backed route for model training or heavy ML workloads.",
        )

    if task_type == TaskType.image:
        return ModelRoute(
            gateway=gateway,
            provider="SenseNova",
            model="sensenova-u1",
            reason="Multimodal route for image or document generation tasks.",
        )

    if task_type == TaskType.web or "scrape" in normalized_prompt or "latest docs" in normalized_prompt:
        return ModelRoute(
            gateway=gateway,
            provider="Bright Data + KimiAI",
            model=configured_model_for_task(TaskType.web),
            reason="Live web context is gathered before passing the task to the coding model.",
        )

    if task_type == TaskType.coding:
        return ModelRoute(
            gateway=gateway,
            provider="KimiAI",
            model=configured_model_for_task(TaskType.coding),
            reason="Primary coding route with workspace context and lock awareness.",
        )

    return ModelRoute(
        gateway=gateway,
        provider="KimiAI",
        model=configured_model_for_task(TaskType.general),
        reason="Default route for general workspace reasoning.",
    )
