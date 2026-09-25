"""Run an agent spec's own ``hooks`` from Crew's turn loop, for a harness that cannot.

kiro-cli reads the spec off disk and runs its ``hooks`` itself. KAS takes the agent
over the wire as ``_meta.kiro.customAgents``, whose schema has no slot for them
(``acp.kas_agents.UNSUPPORTED_SPEC_KEYS``), so on KAS nothing runs them. The backends
in ``ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS`` get them from Crew instead: the turn loop
already fires the five Crew hook events for every backend, and this module turns the
spec's field into :class:`kiro_crew.hooks.ScriptHook` records that ride the same
``ScriptHookStore.fire`` call. So a spec hook meets exactly the gates a Hooks-page
hook meets -- ``capabilities.script_hooks`` and its SEL audit, the sandboxed spawn,
the timeout, the PreToolUse fail-closed rule -- and none that a Hooks-page hook does
not.

Membership is the whole double-fire guard. A non-member's harness runs the field
itself, so the turn loop asks :func:`spec_script_hooks` only when the session's
capabilities say Crew owns the job.

Both spec shapes are read:

* the object form kiro-cli uses and Crew materializes, ``{event: [{command,
  matcher?, timeout_ms?}]}`` on the five camelCase event names;
* the array of KAS hook documents, validated by
  :func:`kiro_crew.agent.normalize_spec_hooks`. ``enabled: false`` is skipped, and
  so is ``confirm: true``, because no prompt can be shown from here; an ``agent``
  action and a trigger with no Crew event are skipped too.

The result is cached by the field's content, so a spec that stays the same costs
one conversion and logs its warnings once, not once per turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any

from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHook,
    _normalize_hook_timeout,
)

logger = logging.getLogger(__name__)

#: kiro-cli's object-form event names, onto the events the hook store fires.
_OBJECT_EVENT_TO_HOOK_EVENT = {
    "agentSpawn": HOOK_EVENT_AGENT_SPAWN,
    "userPromptSubmit": HOOK_EVENT_USER_PROMPT_SUBMIT,
    "preToolUse": HOOK_EVENT_PRE_TOOL_USE,
    "postToolUse": HOOK_EVENT_POST_TOOL_USE,
    "stop": HOOK_EVENT_STOP,
}

#: The events whose matcher names a tool. kiro-cli reads a matcher on these only,
#: so a matcher on any other event is dropped rather than applied to the context.
_TOOL_EVENTS = frozenset({HOOK_EVENT_PRE_TOOL_USE, HOOK_EVENT_POST_TOOL_USE})

#: Bound on the hooks taken from one spec, the same total the materialized spec is
#: capped at, so a hand-written spec cannot make every turn spawn without limit.
_MAX_SPEC_HOOKS = 20

#: Bound on a retained command, matching the document validator's payload cap.
_MAX_COMMAND_LEN = 4096

#: Bound on the conversion cache. A spec edit adds an entry, so the cache is
#: cleared rather than grown once it reaches this.
_CACHE_MAX = 64

_cache: dict[tuple[str, str], tuple[ScriptHook, ...]] = {}


def _diagnostic(value: object) -> str:
    """A spec-supplied value for a log line: escaped, then redacted."""
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _hook_diagnostic

    return _hook_diagnostic(value)


def _matcher_ok(matcher: object) -> bool:
    """The object form's matcher rules: a string, length-capped, safe characters."""
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _hook_matcher_ok

    return _hook_matcher_ok(matcher)


def _hook(agent_id: str, event: str, index: int, entry: dict, timeout: int) -> ScriptHook | None:
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > _MAX_COMMAND_LEN:
        logger.warning(
            "agent %r: spec hook %d on %s has no usable command, skipping", agent_id, index, event
        )
        return None
    matcher = entry.get("matcher") if event in _TOOL_EVENTS else None
    if matcher is not None and not _matcher_ok(matcher):
        # Dropped whole, as the materialized spec's merge drops it: running the
        # hook with no matcher would widen it to every tool.
        logger.warning(
            "agent %r: spec hook %d on %s has an invalid matcher, skipping", agent_id, index, event
        )
        return None
    return ScriptHook(
        id=f"spec:{agent_id}:{event}:{index}",
        name=f"{agent_id} spec hook ({event} #{index + 1})",
        event=event,
        matcher=matcher if isinstance(matcher, str) else "",
        command=command,
        timeout=timeout,
    )


def _from_object_form(agent_id: str, hooks: dict) -> list[ScriptHook]:
    out: list[ScriptHook] = []
    for key, entries in hooks.items():
        if len(out) > _MAX_SPEC_HOOKS:
            break
        event = _OBJECT_EVENT_TO_HOOK_EVENT.get(key) if isinstance(key, str) else None
        if event is None or not isinstance(entries, list):
            logger.warning(
                "agent %r: spec hooks key %s is not a hook event list, skipping",
                agent_id,
                _diagnostic(key),
            )
            continue
        for index, entry in enumerate(entries):
            if len(out) > _MAX_SPEC_HOOKS:
                break
            if not isinstance(entry, dict):
                continue
            timeout_ms = entry.get("timeout_ms")
            timeout = (
                _normalize_hook_timeout(math.ceil(timeout_ms / 1000))
                if isinstance(timeout_ms, (int, float)) and not isinstance(timeout_ms, bool)
                else _normalize_hook_timeout(None)
            )
            hook = _hook(agent_id, event, index, entry, timeout)
            if hook is not None:
                out.append(hook)
    return out


def _from_documents(agent_id: str, hooks: list) -> list[ScriptHook]:
    # circular import: agent imports hooks, which this module imports at load time.
    from kiro_crew.agent import _event_for_hook_trigger, normalize_spec_hooks

    out: list[ScriptHook] = []
    for index, doc in enumerate(normalize_spec_hooks(hooks)):
        if len(out) > _MAX_SPEC_HOOKS:
            break
        name = doc.get("name")
        if doc.get("enabled") is False:
            continue
        if doc.get("confirm") is True:
            logger.warning(
                "agent %r: spec hook %s asks to be confirmed, which Crew cannot prompt "
                "for on this backend, so it does not run",
                agent_id,
                _diagnostic(name),
            )
            continue
        raw_action = doc.get("action")
        action: dict = raw_action if isinstance(raw_action, dict) else {}
        event = _OBJECT_EVENT_TO_HOOK_EVENT.get(_event_for_hook_trigger(doc.get("trigger")) or "")
        if event is None or action.get("type") != "command":
            logger.warning(
                "agent %r: spec hook %s has a trigger or action Crew cannot run, skipping",
                agent_id,
                _diagnostic(name),
            )
            continue
        # The document's timeout is in seconds, clamped like a Hooks-page hook's.
        hook = _hook(
            agent_id,
            event,
            index,
            {"command": action.get("command"), "matcher": doc.get("matcher")},
            _normalize_hook_timeout(doc.get("timeout")),
        )
        if hook is not None:
            out.append(hook)
    return out


def spec_script_hooks(agent_id: str, spec: dict[str, Any]) -> list[ScriptHook]:
    """The spec's own ``hooks`` as script hooks the hook store can fire.

    Empty when the spec carries none. Either shape is read (see the module
    docstring); anything else is warned about once and yields nothing.
    """
    value = spec.get("hooks")
    if not value:
        return []
    try:
        digest = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
    except (TypeError, ValueError):
        digest = ""
    key = (agent_id, digest)
    if digest and key in _cache:
        return list(_cache[key])
    if isinstance(value, dict):
        hooks = _from_object_form(agent_id, value)
    elif isinstance(value, list):
        hooks = _from_documents(agent_id, value)
    else:
        logger.warning("agent %r: spec hooks is neither an object nor an array", agent_id)
        hooks = []
    if len(hooks) > _MAX_SPEC_HOOKS:
        logger.warning(
            "agent %r: %d spec hooks exceed the limit of %d, ignoring the remainder",
            agent_id,
            len(hooks),
            _MAX_SPEC_HOOKS,
        )
        hooks = hooks[:_MAX_SPEC_HOOKS]
    if digest:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = tuple(hooks)
    return list(hooks)


def crew_fired_spec_hooks(agent_id: str) -> tuple[list[ScriptHook], list[str]]:
    """The active agent spec's hooks as script hooks, and the keys nothing carries.

    Reads the spec the KAS projection reads, through the same reader
    (:func:`kiro_crew.acp.kas_agents.load_agent_spec`). The second value names the
    spec keys a KAS session runs without, for the session-start notice. Raises when
    the spec cannot be read; the caller fails PreToolUse closed on that.
    """
    # circular import: the ACP layer imports the config loader, which sits below
    # this module; resolved at call time like the other driver seams here.
    from kiro_crew.acp.kas_agents import load_agent_spec, spec_keys_without_carrier
    from kiro_crew.config.paths import kiro_agents_dir

    spec = load_agent_spec(kiro_agents_dir(), agent_id)
    return spec_script_hooks(agent_id, spec), spec_keys_without_carrier(spec)
