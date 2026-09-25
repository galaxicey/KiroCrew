"""Memory bindings across the real boot: the 0.7.0.8 seam bugs 11, 12 and 13.

Each test drives the booted gateway over HTTP the way the dashboard, an MCP
server or a second boot would, and asserts the contract at the seam where
the bug report sat: the persistence layer after a restart (11), workflow
admission on a home nothing has opted into (12), and the tool-policy read
when one spec in the agents directory is unreadable (13).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

# A one-step workflow whose result is a literal: admission is the only thing
# that can make it fail before a run exists.
_TRIVIAL_WORKFLOW = (
    'META = {"name": "admission smoke"}\nasync def workflow(ctx):\n    return "admitted"\n'
)


async def _finished(gw, run_id: str, *, secs: float = 60.0) -> dict:
    deadline = time.monotonic() + secs
    run: dict = {}
    while time.monotonic() < deadline:
        run = await gw.get_json(f"/api/workflows/runs/{run_id}")
        if run.get("status") != "running":
            return run
        await asyncio.sleep(0.1)
    pytest.fail(f"workflow {run_id} did not terminate: {json.dumps(run)[:500]}")


# ---------------------------------------------------------------- bug 11 ---


@pytest.mark.asyncio
async def test_chat_slots_are_guarded(gateway_boot) -> None:
    async with gateway_boot() as gw:
        denied = await gw.get("/api/chat/slots", auth=False)
        assert denied.status in (401, 403), await denied.text()
        bad = await gw.post("/api/chat/slots", {"memory_mode": "forever"})
        assert bad.status == 400, await bad.text()
        assert "memory_mode" in (await bad.json())["error"]


@pytest.mark.asyncio
async def test_persistent_chat_survives_a_restart_and_incognito_does_not(gateway_boot) -> None:
    """Documents the CURRENT contract of ``memory_mode`` at the persistence
    seam: a persistent slot comes back after a restart, a non-persistent one
    does not (bug 11). ``open_slots.json`` is the only restart-restore
    set and it lists persistent slots alone.

    If the product decision on bug 11 is Option A (non-persistent affects
    learnings only, chats persist), flip the incognito half of this test to
    ``assert incognito in names_after`` and it becomes the regression pin.
    """
    async with gateway_boot() as gw:
        persistent = (await gw.post_json("/api/chat/slots", {"memory_mode": "persistent"}))["key"]
        incognito = (await gw.post_json("/api/chat/slots", {"memory_mode": "incognito"}))["key"]
        names_before = {s["key"] for s in await gw.get_json("/api/chat/slots")}
        assert {persistent, incognito} <= names_before

        await gw.restart()

        # The restore set is written at shutdown; the first boot's shutdown just
        # wrote it, and it lists the persistent slot alone.
        open_slots = json.loads((gw.home / "open_slots.json").read_text(encoding="utf-8"))
        assert persistent in open_slots["keys"]
        assert incognito not in open_slots["keys"]

        names_after = {s["key"] for s in await gw.get_json("/api/chat/slots")}
        assert persistent in names_after
        assert incognito not in names_after


# ---------------------------------------------------------------- bug 12 ---


@pytest.mark.asyncio
async def test_workflow_run_is_guarded(gateway_boot) -> None:
    async with gateway_boot() as gw:
        denied = await gw.post("/api/workflows/run", {"source": _TRIVIAL_WORKFLOW}, auth=False)
        assert denied.status in (401, 403), await denied.text()
        empty = await gw.post("/api/workflows/run", {"source": ""})
        assert empty.status == 400, await empty.text()
        assert (await empty.json())["error"] == "source is required"


@pytest.mark.asyncio
async def test_workflow_runs_on_a_home_nothing_opted_into_private_memory(gateway_boot) -> None:
    """A fresh home has no member, no V2 store and no bindings beyond the
    defaults. Workflow admission must fall back to Global there, not refuse
    with ``workflow_memory_unavailable`` (bug 12: ``doctor`` called these bindings
    valid while workflow creation said memory was unavailable).
    """
    async with gateway_boot() as gw:
        resp = await gw.post("/api/workflows/run", {"source": _TRIVIAL_WORKFLOW})
        body = await resp.json()
        assert resp.status == 200, body
        assert body.get("code") != "workflow_memory_unavailable", body
        assert "run_id" in body, body
        run = await _finished(gw, body["run_id"])
        assert run["status"] == "finished", run
        assert run["result"] == "admitted", run


# ---------------------------------------------------------------- bug 13 ---


async def _agents_dir(*, secs: float = 30.0) -> Path:
    """The kiro agents directory once boot has written the managed specs.

    The spec rebuild runs after the dashboard is already serving, so a test
    that writes beside the managed specs waits for ``kirocrew.json`` first, or
    it races the writer for the directory itself.
    """
    from kiro_crew.config.paths import kiro_agents_dir

    directory = kiro_agents_dir()
    deadline = time.monotonic() + secs
    while not (directory / "kirocrew.json").is_file():
        if time.monotonic() > deadline:
            pytest.fail(f"boot did not write the managed agent specs under {directory}")
        await asyncio.sleep(0.05)
    return directory


async def _default_session(gw) -> str:
    slot = (await gw.post_json("/api/chat/slots", {}))["key"]
    return f"dashboard:{slot}"


@pytest.mark.asyncio
async def test_tool_policy_is_guarded(gateway_boot) -> None:
    """The route is for managed MCP servers: the loopback secret alone is not
    an identity, and the dashboard cookie is not one either."""
    async with gateway_boot() as gw:
        session = await _default_session(gw)
        no_secret = await gw.get(
            "/api/session-tool-policy", headers={"X-Session-Key": session}, auth=False
        )
        assert no_secret.status == 403, await no_secret.text()
        unattested = await gw.get(
            "/api/session-tool-policy",
            headers={"X-Session-Key": session, "X-Internal-Secret": gw.app["local_secret"]},
            auth=False,
        )
        assert unattested.status == 409, await unattested.text()
        assert (await unattested.json())["code"] == "member_identity_unavailable"


@pytest.mark.asyncio
async def test_a_default_session_reads_an_empty_policy(gateway_boot) -> None:
    async with gateway_boot() as gw:
        session = await _default_session(gw)
        body = await gw.get_json(
            "/api/session-tool-policy", headers=gw.mcp_headers(session), auth=False
        )
        assert body == {}


@pytest.mark.xfail(
    strict=True,
    reason="bug 13 (#13130, #13295): one unreadable spec in the agents directory "
    "makes every session whose agent has no spec of its own answer policy_unreadable, "
    "and the refusal does not name the file",
)
@pytest.mark.asyncio
async def test_an_unreadable_neighbour_spec_is_narrow_and_named(gateway_boot) -> None:
    """One malformed spec beside the managed ones. The default session's
    policy read must either still succeed (the file cannot be this agent's
    spec, so its exclusions are not this session's) or, if the gate keeps
    refusing, the refusal must name the file so the operator can act.

    Today it does neither: 409 ``policy_unreadable`` with a reason that names
    the exception class and the agent, never the path.
    """
    async with gateway_boot() as gw:
        session = await _default_session(gw)
        bad = (await _agents_dir()) / "bad-agent.json"
        bad.write_text('{"name": "bad-agent", "managedToolPolicy": 42', encoding="utf-8")

        resp = await gw.get("/api/session-tool-policy", headers=gw.mcp_headers(session), auth=False)
        body = await resp.json()
        assert resp.status in (200, 409), body
        if resp.status == 409:
            assert body["code"] == "policy_unreadable"
            assert bad.name in body.get("reason", ""), body


@pytest.mark.asyncio
async def test_removing_the_unreadable_spec_clears_the_refusal(gateway_boot) -> None:
    """The refusal is re-derived per request, never memoized: fixing the
    directory is enough, no restart needed (the report said restart did not
    help, which is true only because the file was still there)."""
    async with gateway_boot() as gw:
        session = await _default_session(gw)
        bad = (await _agents_dir()) / "bad-agent.json"
        bad.write_text("{not json", encoding="utf-8")
        headers = gw.mcp_headers(session)

        refused = await gw.get("/api/session-tool-policy", headers=headers, auth=False)
        assert refused.status == 409, await refused.text()
        assert (await refused.json())["code"] == "policy_unreadable"

        bad.unlink()
        body = await gw.get_json("/api/session-tool-policy", headers=headers, auth=False)
        assert body == {}
