"""An agent spec's own ``hooks`` run from Crew's turn loop on KAS, and only there.

KAS takes the agent over a wire schema with no slot for ``hooks``, so the turn loop
fires them through the hook store; kiro-cli runs the field itself, so a kiro-cli
session must not get them from Crew too. ``toolsSettings`` and ``slashCommand``
still reach no KAS session, and the user is told once per session.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    ScriptHook,
    ScriptHookResult,
    ScriptHookStore,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


def _client(backend: str) -> SimpleNamespace:
    return SimpleNamespace(capabilities=capabilities_for(backend))


def _write_spec(agents_dir: Path, name: str, **fields) -> None:
    spec = {"name": name, "prompt": "p", **fields}
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


def _prepare(client, agent, *, is_new=False):
    return asyncio.run(
        chat_runner._prepare_spec_hooks(
            SimpleNamespace(), SimpleNamespace(), client, agent, is_new=is_new
        )
    )


@pytest.fixture
def notices(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(
        chat_runner, "append_and_surface", lambda state, slot, role, text, cls: seen.append(text)
    )
    return seen


_OBJECT_HOOKS = {"preToolUse": [{"matcher": "shell", "command": "guard.sh"}]}


def test_membership_is_kas_only():
    assert capabilities_for(ACP_BACKEND_KAS).crew_fires_spec_hooks is True
    assert capabilities_for(ACP_BACKEND_KIRO).crew_fires_spec_hooks is False


def test_a_kiro_cli_session_gets_no_spec_hooks_from_crew(agents_dir, notices):
    _write_spec(agents_dir, "a1", hooks=_OBJECT_HOOKS, toolsSettings={"x": 1})
    assert _prepare(_client(ACP_BACKEND_KIRO), "a1", is_new=True) == ([], False)
    assert notices == []


def test_a_kas_session_gets_the_spec_hooks(agents_dir, notices):
    _write_spec(agents_dir, "a1", hooks=_OBJECT_HOOKS)
    hooks, unreadable = _prepare(_client(ACP_BACKEND_KAS), "a1")
    assert unreadable is False
    assert [(h.event, h.matcher, h.command) for h in hooks] == [
        (HOOK_EVENT_PRE_TOOL_USE, "shell", "guard.sh")
    ]


def test_an_unreadable_spec_fails_closed(agents_dir, notices):
    (agents_dir / "a1.json").write_text("{not json", encoding="utf-8")
    assert _prepare(_client(ACP_BACKEND_KAS), "a1") == ([], True)


def test_tools_settings_notice_fires_once_per_session(agents_dir, notices):
    _write_spec(agents_dir, "a1", hooks=_OBJECT_HOOKS, toolsSettings={"shell": {}})
    _prepare(_client(ACP_BACKEND_KAS), "a1", is_new=True)
    _prepare(_client(ACP_BACKEND_KAS), "a1", is_new=False)
    assert len(notices) == 1
    assert "toolsSettings" in notices[0]
    assert "hooks" not in notices[0]


def test_disabled_and_confirm_documents_do_not_run():
    docs = [
        {"name": "on", "trigger": "PreToolUse", "action": {"type": "command", "command": "a"}},
        {
            "name": "off",
            "trigger": "PreToolUse",
            "enabled": False,
            "action": {"type": "command", "command": "b"},
        },
        {
            "name": "ask",
            "trigger": "PreToolUse",
            "confirm": True,
            "action": {"type": "command", "command": "c"},
        },
    ]
    hooks = spec_hooks.spec_script_hooks("a1", {"hooks": docs})
    assert [h.command for h in hooks] == ["a"]


def test_object_form_maps_events_timeout_and_drops_matcher_off_tool_events():
    hooks = spec_hooks.spec_script_hooks(
        "a1",
        {"hooks": {"stop": [{"matcher": "x", "command": "s", "timeout_ms": 2500}]}},
    )
    assert [(h.event, h.matcher, h.timeout) for h in hooks] == [(HOOK_EVENT_STOP, "", 3)]


def test_an_oversized_or_unsafe_matcher_drops_the_hook_and_retains_nothing():
    long = "a" * 10_000
    hooks = spec_hooks.spec_script_hooks(
        "a1",
        {
            "hooks": {
                "preToolUse": [
                    {"matcher": long, "command": "x"},
                    {"matcher": "sh;rm", "command": "y"},
                    {"matcher": "sh*", "command": "z"},
                ]
            }
        },
    )
    assert [(h.command, h.matcher) for h in hooks] == [("z", "sh*")]


def test_conversion_stops_at_the_hook_cap():
    entries = [{"command": f"c{i}"} for i in range(500)]
    hooks = spec_hooks.spec_script_hooks("a1", {"hooks": {"stop": entries}})
    assert len(hooks) == spec_hooks._MAX_SPEC_HOOKS


def test_extra_hooks_fire_but_are_never_persisted(tmp_path, monkeypatch):
    store = ScriptHookStore(tmp_path)
    ran: list = []

    async def fake_run(hook, context="", hook_event=None):
        ran.append((hook.id, hook_event.get("tool_name")))
        return ScriptHookResult(hook_id=hook.id, hook_name=hook.name, event=hook.event)

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    extra = [ScriptHook(id="spec:a1:x", event=HOOK_EVENT_PRE_TOOL_USE, matcher="sh*", command="c")]
    asyncio.run(store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="shell", extra_hooks=extra))
    asyncio.run(store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="read", extra_hooks=extra))
    assert ran == [("spec:a1:x", "shell")]
    persisted = tmp_path / "hooks.json"
    assert not persisted.exists() or "spec:a1:x" not in persisted.read_text(encoding="utf-8")


def _passthrough_sandbox(monkeypatch):
    monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))


def test_a_spec_pre_tool_use_hook_exit_2_blocks(tmp_path, agents_dir, monkeypatch, notices):
    _passthrough_sandbox(monkeypatch)
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)
    script = tmp_path / "deny.py"
    script.write_text("import sys\nsys.stderr.write('no')\nsys.exit(2)\n", encoding="utf-8")
    command = f'"{sys.executable}" "{script}"'
    _write_spec(agents_dir, "a1", hooks={"preToolUse": [{"command": command}]})
    hooks, _ = _prepare(_client(ACP_BACKEND_KAS), "a1")
    results = asyncio.run(
        ScriptHookStore(tmp_path).fire(
            HOOK_EVENT_PRE_TOOL_USE, tool_name="shell", tool_input={}, extra_hooks=hooks
        )
    )
    assert [(r.exit_code, r.blocked, r.stderr) for r in results] == [(2, True, "no")]


def test_a_governance_denied_spec_hook_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": "off")

    async def no_spawn(*a, **k):
        raise AssertionError("a denied hook must not spawn")

    monkeypatch.setattr("kiro_crew.sandbox.create_subprocess_limited", no_spawn)
    hooks = spec_hooks.spec_script_hooks("a1", {"hooks": _OBJECT_HOOKS})
    results = asyncio.run(
        ScriptHookStore(tmp_path).fire(
            HOOK_EVENT_PRE_TOOL_USE,
            tool_name="shell",
            parent_session_key="s1",
            extra_hooks=hooks,
        )
    )
    assert [r.blocked for r in results] == [True]
    assert "governance" in results[0].error
