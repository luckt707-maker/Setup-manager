"""What to do next, from the project's real steps.

The recommendation is chosen here, before any model sees it. A model may
pick one of these steps and rewrite the sentence. It cannot add a step.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

ACTIVITIES = ("browse", "project", "running", "finished", "failed")
_KEEP = ("id", "label", "desc", "action", "endpoint", "param", "cmd", "project_type")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _norm(text: str) -> str:
    return "".join(ch.lower() for ch in text if ch.isalnum())


def _public_step(step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not step.get("id") or not step.get("label"):
        return None
    public: Dict[str, Any] = {}
    for key in _KEEP:
        value = step.get(key)
        if value not in (None, ""):
            public[key] = value
    return public


def _same_step(task_label: str, step_label: str) -> bool:
    left, right = _norm(task_label), _norm(step_label)
    return bool(left) and (left in right or right in left)


def _following(choices: List[Dict[str, Any]], action: Optional[Dict[str, Any]], limit: int = 2) -> List[str]:
    if action is None:
        return [str(step["label"]) for step in choices[:limit]]
    ids = [step.get("id") for step in choices]
    try:
        index = ids.index(action.get("id"))
    except ValueError:
        return []
    return [str(step["label"]) for step in choices[index + 1 : index + 1 + limit]]


def clean_why(text: Any) -> str:
    """One short sentence. Backticks are removed so a model cannot sneak in a command."""
    sentence = " ".join(str(text or "").replace("`", " ").split())
    return sentence[:280]


def advise(
    *,
    activity: str,
    task_label: str = "",
    detected: Optional[List[str]] = None,
    steps: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The built-in recommendation for what the user is doing right now."""
    if activity not in ACTIVITIES:
        activity = "browse"
    label = (task_label or "").strip()[:200]
    kinds = [str(item) for item in (detected or []) if item]
    choices = [step for step in (_public_step(item) for item in (steps or [])) if step]

    def pack(headline: str, detail: str, action: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "activity": activity,
            "headline": headline,
            "detail": detail,
            "source": "rules",
            "action": action,
            "after": _following(choices, action),
            "choices": choices,
        }

    if activity == "browse":
        return pack(
            "Pick a project",
            "Choose a folder on the left. The guide will recommend the first thing to do for it.",
            None,
        )
    if activity == "running":
        name = label or "this step"
        return pack(
            f"Running {name}",
            "Watch the output below. The next recommendation shows up when it finishes. Cancel is there if you need to stop it.",
            None,
        )
    if not choices:
        return pack(
            "Nothing to run automatically",
            "This folder does not look like a project Setup Manager knows. You can still open it in Explorer, or ask the AI what the files are.",
            None,
        )

    done_index = -1
    if label:
        for index, step in enumerate(choices):
            if _same_step(label, str(step["label"])):
                done_index = index
                break

    if activity == "failed":
        failed = choices[done_index] if done_index >= 0 else choices[0]
        return pack(
            f"{failed['label']} did not finish",
            "Read the output below, then run this step again. If it says a tool is missing, use System check in the header.",
            failed,
        )

    if activity == "finished" and done_index >= 0 and done_index + 1 < len(choices):
        nxt = choices[done_index + 1]
        return pack(
            "That finished. Do this next",
            nxt.get("desc") or "This is the next step for this project.",
            nxt,
        )
    if activity == "finished" and done_index == len(choices) - 1:
        return pack(
            "The recommended steps are done",
            "You can run the project from the Run button, or open Advanced options for checks and extra tools.",
            None,
        )

    first = choices[0]
    kind = ", ".join(kinds) if kinds else "this"
    return pack(
        "Start here",
        f"This looks like a {kind} project. {first.get('desc') or 'Run this before the later steps.'}",
        first,
    )


def _parse_object(reply: str) -> Optional[Dict[str, Any]]:
    text = (reply or "").strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def apply_model_reply(advice: Dict[str, Any], reply: str) -> Dict[str, Any]:
    """Accept a model's choice only when it names one of the real steps.

    A sentence is kept even when the choice is discarded. ``none`` hides the
    button only after a failure, where reading the output is a real answer.
    On a fresh project the built-in step stays, so there is always something
    to press.
    """
    data = _parse_object(reply)
    if data is None:
        return advice
    why = clean_why(data.get("why"))
    action_id = str(data.get("action_id") or "").strip()
    choices = list(advice.get("choices") or [])
    match = next((step for step in choices if step.get("id") == action_id), None)
    invented = action_id not in {"", "none"} and match is None
    if invented and not why:
        return advice

    updated = dict(advice)
    updated["source"] = "ollama"
    if why:
        updated["detail"] = why
    if invented:
        updated["after"] = _following(choices, updated.get("action"))
        return updated
    if action_id == "none":
        if advice.get("activity") == "failed":
            updated["action"] = None
        updated["after"] = _following(choices, updated.get("action"))
        return updated
    if match is not None:
        updated["action"] = match
        updated["headline"] = "Suggested next step"
    updated["after"] = _following(choices, updated.get("action"))
    return updated


def present(advice: Dict[str, Any]) -> Dict[str, Any]:
    """The fields the dashboard renders. The choice list stays on the server."""
    return {
        "activity": advice.get("activity"),
        "headline": advice.get("headline") or "",
        "detail": advice.get("detail") or "",
        "source": advice.get("source") or "rules",
        "action": advice.get("action"),
        "after": list(advice.get("after") or []),
    }
