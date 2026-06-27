"""Role-based model registry for Parallel Swarm OpenRouter tasks."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_ROLE_MODEL_REGISTRY: dict[str, str] = {
    "cheap_coder": "deepseek/deepseek-chat",
    "long_context_worker": "google/gemini-2.5-flash",
    "coding_lead": "anthropic/claude-sonnet-4",
    "review_gate": "google/gemini-2.5-pro",
    "architect_arbiter": "anthropic/claude-opus-4.1",
}


@dataclass(frozen=True)
class ModelResolution:
    model: str
    source: str
    role: str
    known_role: bool


def resolve_model_for_role(role: str = "", explicit_model: str = "", registry: dict[str, str] | None = None) -> ModelResolution:
    """Resolve an explicit model or a role to a pinned model id.

    Explicit per-task models always win. Unknown/empty roles resolve to an empty
    model so the caller can fail closed for OpenRouter tasks that require exact
    model IDs.
    """
    if explicit_model:
        return ModelResolution(model=explicit_model, source="explicit", role=role or "", known_role=bool(role in (registry or DEFAULT_ROLE_MODEL_REGISTRY)))
    active = registry or DEFAULT_ROLE_MODEL_REGISTRY
    if role and role in active:
        return ModelResolution(model=active[role], source="role_registry", role=role, known_role=True)
    return ModelResolution(model="", source="unresolved", role=role or "", known_role=False)
