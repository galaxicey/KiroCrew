"""Identity-checked handles for killing a session's process after its teardown.

The shape one supervisor of a ``kiro-cli`` session needs when the graceful reset
of that session hangs, fails, or completes without proof of death: take a
:class:`ProcessHandle` off the live client BEFORE the reset (the reset pops the
session out of the manager's map before the awaits that can hang, so nothing names
the process afterwards), ask :func:`process_survived` whether the process the
handle names is still standing, and hand the handle to
:func:`kill_verified_process`, which verifies the root by its recorded start id
before and after the child walk, signals the group, sweeps the recorded children,
and RETURNS what stopped the kill instead of swallowing it.

Used by the cron reaper and ``cancel()`` (:mod:`kiro_crew.cron`), and written as the
one home for the sub-agent manager's twin of that path -- which today still
carries its own copy of the older shape -- so that once both import it, a rename
or a rule change cannot regress one caller to a silent "reaped".

The tree. A ``kiro-cli`` process is spawned with ``start_new_session=True`` on
POSIX, so its pid is also its process-group id and every descendant that did not
``setsid`` out keeps it. The group outlives its leader: a late child spawn
followed by the leader's exit leaves a group with members but no leader, and
``os.getpgid(pid)`` -- what a leader-addressed group kill reads -- raises
``ProcessLookupError`` for it, which a naive caller reads as "the group is gone"
while the tree keeps running. So the handle retains the group id READ WHILE THE
LEADER WAS ALIVE and identity-checked (:func:`isolated_group_of`), and a kill that
finds the leader gone decides by that retained group, not by the leader's absence:
a group that still has members is signalled by its id once a member verified as
this run's vouches for it (the root's zombie, or a descendant the client recorded
at spawn -- a number nobody verified may lead a stranger's tree by now), and
``ProcessLookupError`` from ``os.killpg`` means "group empty" ONLY for a group so
verified. A group that is alive but cannot be verified as this run's is a kill
failure, never a reap. On Windows the same question is asked of the exact-tree
cleanup pin the spawn reserved: a pin still pending for the root's identity is
drained (``kill_process_tree_pinned``), and only a completed drain is a kill.

Leaf module: it imports :mod:`kiro_crew.platform_compat` and the executor only,
and never the ACP layer. The client's child-process helpers (the child-tree probe,
the record capture, the escaped-children sweep) are handed in by the caller as
:data:`ChildHelpers`, resolved at call time the way the session teardown resolves
them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.executors import subprocess_executor

logger = logging.getLogger(__name__)


def failure_name(exc: BaseException) -> str:
    """Name the failure a kill raised or reported, for the run's terminal record."""
    detail = str(exc)
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


@dataclass(frozen=True)
class ProcessHandle:
    """What a kill needs of a run's session process, taken BEFORE the reset.

    ``SessionLifecycle.reset`` pops the session out of the session map under its
    lock before the awaits that can hang (the end record, the unlink, the child
    probes, the provider shutdown), so once ``wait_for(reset)`` has timed out the
    map does not name the process the reset could not stop. A kill that looks
    the session up afterwards finds nothing, and without this handle it would
    call a still-running process nothing to kill. The handle is the pid the
    client recorded at spawn, the start id it read for that pid then
    (:func:`platform_compat.get_process_start_id`, the recycling detector the
    kill re-reads before it signals), the POSIX process group the pid led while
    its identity held (:func:`isolated_group_of`; None on Windows, when the
    leader was already gone or recycled at snapshot time, or when it was not an
    isolated group leader), and the child records the client had accumulated --
    snapshotted from the live client while the map still held the session, or,
    when the run's OWN teardown reset has already popped it, from the session
    the manager retains for exactly the life of that teardown
    (``SessionManager.tearing_down``), which is the same client.
    """

    pid: int | None
    start_id: str | None
    pgid: int | None
    child_pids: dict[Any, Any]


def isolated_group_of(pid: int, start_id: str | None) -> int | None:
    """The POSIX process group *pid* leads, read while its identity holds, else None.

    A group id is only as trustworthy as the read that produced it: ``getpgid``
    of a recycled pid names a stranger's group. So the read is bracketed by the
    recorded start id -- before, so the pid is the process the client spawned,
    and after, so it did not exit and get recycled in between -- and the id is
    kept only for an ISOLATED leader (``pgid == pid``, not init's group, not our
    own), which is what ``start_new_session=True`` makes the ``kiro-cli``
    process: under that predicate the group holds this run's tree and nothing
    else, so a signal to it can never reach a foreign process. ``None`` means a
    later group signal is off the table (only pid-scoped kills and the recorded
    children remain), never a guess. Windows has no process groups in this
    sense; ``taskkill /T`` walks the tree there while the root is alive.

    In-process and non-blocking (two ``/proc`` reads and a ``getpgid``), so safe
    to call on the event loop -- as the snapshot that takes the handle is.
    """
    if platform_compat.IS_WINDOWS or start_id is None or pid <= 1:
        return None
    getpgid = getattr(os, "getpgid", None)
    getpgrp = getattr(os, "getpgrp", None)
    if getpgid is None or getpgrp is None:
        return None  # no POSIX process groups here: no group signal later either
    if platform_compat.get_process_start_id(pid) != start_id:
        return None
    try:
        pgid = getpgid(pid)
    except OSError:
        return None
    if pgid != pid or pgid <= 1 or pgid == getpgrp():
        return None
    if platform_compat.get_process_start_id(pid) != start_id:
        return None
    return pgid


def process_handle_of(session: Any) -> ProcessHandle:
    """Read the kill handle off a live session's provider.

    The pid, start id and child records are the ACP client's own (no syscalls).
    A provider that runs its process through an owned ``Popen`` instead -- the
    claude harness keeps it on ``_proc`` / ``_active_proc``, the same fallback
    the session teardown reads -- has no client-recorded start id, so one is
    read now from the live pid: the owned handle with ``returncode`` unset is
    what makes that read safe, since the kernel keeps the pid for a child its
    parent has not reaped. The group id is read from the kernel now either way,
    identity-bracketed, because it must be taken while the leader is alive to
    be worth anything later.
    """
    provider = getattr(session, "provider", None)
    client = getattr(provider, "_client", None)
    raw_pid = getattr(client, "_pid", None) if client else None
    raw_start = getattr(client, "_start_time", None) if client else None
    raw_children = getattr(client, "_child_pids", None) if client else None
    pid = raw_pid if isinstance(raw_pid, int) and raw_pid > 1 else None
    start_id = raw_start if isinstance(raw_start, str) else None
    if pid is None:
        for attribute in ("_proc", "_active_proc"):
            proc = getattr(provider, attribute, None)
            proc_pid = getattr(proc, "pid", None)
            if (
                proc is not None
                and getattr(proc, "returncode", 0) is None
                and isinstance(proc_pid, int)
                and proc_pid > 1
            ):
                pid = proc_pid
                start_id = platform_compat.get_process_start_id(pid)
                break
    return ProcessHandle(
        pid=pid,
        start_id=start_id,
        pgid=isolated_group_of(pid, start_id) if pid else None,
        child_pids=dict(raw_children) if isinstance(raw_children, dict) else {},
    )


def _tree_survived(handle: ProcessHandle) -> bool:
    """Whether what the handle's leader left behind still stands.

    POSIX: the process group, probed with signal 0 and never signalled. An
    unverified number (``pgid`` None -- the leader was already gone at snapshot
    time, so the group id falls back to the leader's pid, which IS the id of a
    ``start_new_session`` group) may be asked whether anything answers under it,
    because a probe reaches nothing.

    Windows: the exact-tree cleanup pin. Windows has no process groups, but the
    spawn reserves an exact-handle cleanup for the tree
    (``platform_compat.windows_tree_cleanup_pending``), and a pin still pending
    for this root's identity means descendants -- or cleanup debt -- outlived
    the root: the pin outranks numeric liveness, including a dead root pid.
    """
    if not handle.pid:
        return False
    if platform_compat.IS_WINDOWS:
        # A pin is keyed by (root pid, creation time); with no recorded identity
        # the registry still answers by root pid alone.
        return platform_compat.windows_tree_cleanup_pending(handle.pid, handle.start_id)
    number = handle.pgid if handle.pgid is not None else handle.pid
    return platform_compat.pgroup_exists(number)


def _child_start_of(record: Any) -> str | None:
    """The start id a client child record carries (``(start, basename)`` or a bare start)."""
    start = record[0] if isinstance(record, tuple) else record
    return start if isinstance(start, str) else None


def _in_group(pid: int, pgid: int, start_id: str | None) -> bool:
    """Whether *pid* -- live or a zombie -- is a member of process group *pgid*.

    ``pgroup_of`` (``getpgid``) answers for a live member everywhere and for a
    zombie on Linux; macOS refuses a zombie with ``ESRCH`` while its start id
    still reads, which is exactly the exited-but-unreaped root that has to be
    able to vouch for its group, so there the kernel's group listing
    (``darwin_pgroup_members``) settles it, matched on the start id as well as
    the pid so a recycled number cannot pass. A definite answer naming another
    group is final; unreadable on both is "not a member".
    """
    answer = platform_compat.pgroup_of(pid)
    if answer is not None:
        return answer == pgid
    if sys.platform != "darwin" or start_id is None:
        return False
    members = platform_compat.darwin_pgroup_members(pgid)
    if members is None:
        return False
    return any(member.pid == pid and member.start_id == start_id for member in members)


def _group_member_vouches(handle: ProcessHandle) -> bool:
    """Whether an identity-verified member of the run's tree still sits in the retained group.

    A group id is only as trustworthy as its members. Once every process in the
    group is gone the number is free, and a group signal aimed at it lands on
    whichever tree takes it next -- so a leaderless group may only be signalled
    while it can still name a member verified as this run's: the root itself
    (a zombie still reads its start id and its group), or one of the descendants
    the client recorded at spawn, whose start id reads back and whose group is
    the retained one. A member the client never recorded (a late spawn) cannot
    vouch, and a group only such members keep alive is not signalled -- reported,
    never reaped. The same rule ``session_pid``'s orphan teardown applies.
    """
    pgid = handle.pgid
    pid = handle.pid
    if pgid is None or not pid:
        return False
    if (
        handle.start_id is not None
        and platform_compat.get_process_start_id(pid) == handle.start_id
        and _in_group(pid, pgid, handle.start_id)
    ):
        return True
    for cpid, record in handle.child_pids.items():
        start = _child_start_of(record)
        if not isinstance(cpid, int) or cpid <= 1 or start is None:
            continue
        if platform_compat.get_process_start_id(cpid) != start:
            continue
        if _in_group(cpid, pgid, start):
            return True
    return False


def process_survived(handle: ProcessHandle) -> bool:
    """Whether the process ``handle`` names may still be standing after a reset.

    A reset that completed is not proof the process is gone: its own shutdown
    can fail without raising out of it, and one that answered ``False`` found
    no session and stopped nothing. So the callers ask the process itself,
    the way :func:`kill_verified_process` will: False only on evidence that it
    is gone -- no usable pid; or a leader that is gone (no start id readable
    AND no process behind the pid, a start id that differs from the recorded
    one because the pid is another process's now, or a pid the platform
    reports as exited even though its start id still reads back) WHOSE TREE IS
    GONE TOO. The exited-but-readable case is Windows: the creation time is
    read through a query handle, which opens for an EXITED process for as long
    as any handle to the process object is still held (the transport's, until
    GC), so identity alone does not say alive there; ``pid_exists`` confirms
    the exit code. The tree case is POSIX's process group -- a leader that
    exited after a late child spawn leaves members under its group id -- and
    Windows's exact-tree cleanup pin, still pending for this root's identity
    when descendants or cleanup debt outlived it; a gone leader is asked for
    its tree before it is called gone. Anything else -- the recorded start id
    read back on a live pid, a live pid whose identity cannot be confirmed, or
    a gone leader whose tree still stands -- is a process the run still has to
    answer for, and the kill then decides between the signal and a named
    failure.
    """
    pid = handle.pid
    if not pid:
        return False
    actual_start = platform_compat.get_process_start_id(pid)
    if actual_start is None:
        return platform_compat.pid_exists(pid) or _tree_survived(handle)
    if handle.start_id is not None and actual_start != handle.start_id:
        return _tree_survived(handle)  # recycled: our leader is gone; its group may not be
    return platform_compat.pid_exists(pid) or _tree_survived(handle)


async def _finish_gone_leader(handle: ProcessHandle, *, who: str, key: str) -> str | None:
    """Finish the tree a gone leader left behind, or say why it could not be.

    Reached once the leader is known to be gone -- exited before the kill,
    recycled, or vanished between the last identity read and the signal (the
    ``ProcessLookupError`` a leader-addressed kill raises then). The leader's
    absence says nothing about its tree. Returns ``None`` when the tree is gone
    (nothing to kill) or has just been signalled and drained, and the failure
    otherwise.

    POSIX -- the process group. Empty, decided by a signal-0 probe of the
    retained id (or, when no id was verified while the leader was alive, of the
    leader's pid, which is the id of a ``start_new_session`` group -- probed,
    never signalled): nothing to kill. Alive:

    * No verified group id: the group cannot be told from a stranger's, so it
      is not signalled -- and the run is not recorded as reaped over members
      that may survive.
    * Verified, but no identity-verified member still in it vouches for it
      (:func:`_group_member_vouches`): once our members are gone the number is
      free for another tree, so the group is not signalled -- reported.
    * Vouched for: ``os.killpg`` on the retained id, behind the same broadcast
      guard ``kill_process_tree`` carries. ``ProcessLookupError`` from it is the
      group emptying between the probe and the signal -- nothing to kill; any
      other error is a failure.

    Windows -- the exact-tree cleanup pin. There are no process groups, but the
    spawn reserves an exact-handle cleanup for the tree, and an exited root may
    still anchor surviving descendants under a pin that is still pending
    (``platform_compat.windows_tree_cleanup_pending``). No pin pending: nothing
    this code can reach (``taskkill /T`` walked the tree while the root lived),
    nothing to kill. Pending: the reservation is drained through
    ``platform_compat.kill_process_tree_pinned`` -- off the loop, it waits on
    the tree's handles -- and only a completed drain is a kill; an identity that
    cannot be pinned or a drain that raises (unknown or incomplete cleanup) is
    a failure.
    """
    if handle.pid is None:
        return None
    pid = handle.pid
    if platform_compat.IS_WINDOWS:
        if not platform_compat.windows_tree_cleanup_pending(pid, handle.start_id):
            return None
        if handle.start_id is None:
            # The pin answers by root pid, but a drain needs the creation time to
            # pin the identity it terminates through: nothing safe to drain.
            logger.error(
                "%s: exited pid %d has Windows tree cleanup pending but no recorded identity "
                "to pin it by; not drained (%s)",
                who,
                pid,
                key,
            )
            return (
                f"pid {pid} exited with Windows tree cleanup pending and no recorded identity "
                "to pin it by; not drained"
            )
        loop = asyncio.get_running_loop()
        try:
            drained = await loop.run_in_executor(
                subprocess_executor(),
                platform_compat.kill_process_tree_pinned,
                pid,
                handle.start_id,
                platform_compat.SIGKILL,
            )
        except Exception as exc:
            failure = failure_name(exc)
            logger.error(
                "%s: Windows tree cleanup incomplete for exited pid %d (%s): %s",
                who,
                pid,
                key,
                failure,
            )
            return f"Windows tree cleanup incomplete for exited pid {pid}: {failure}"
        if not drained:
            logger.error(
                "%s: exited pid %d has Windows tree cleanup pending but its identity could "
                "not be pinned; not drained (%s)",
                who,
                pid,
                key,
            )
            return (
                f"pid {pid} exited with Windows tree cleanup pending and its identity could not "
                "be pinned; not drained"
            )
        logger.warning("%s: drained the Windows tree of exited pid %d for %s", who, pid, key)
        return None
    pgid = handle.pgid
    number = pgid if pgid is not None else pid
    if not platform_compat.pgroup_exists(number):
        logger.debug("%s: process group %d of exited pid %d is empty for %s", who, number, pid, key)
        return None
    if pgid is None:
        logger.error(
            "%s: pid %d exited but process group %d still has members and was not verified "
            "as this run's while the leader was alive; not signalled (%s)",
            who,
            pid,
            number,
            key,
        )
        return (
            f"pid {pid} exited but its process group {number} still has members and could not "
            "be verified as this run's; not signalled"
        )
    if not _group_member_vouches(handle):
        logger.error(
            "%s: process group %d of exited pid %d still has members but none could be "
            "verified as this run's; not signalled (%s)",
            who,
            pgid,
            pid,
            key,
        )
        return (
            f"pid {pid} exited and its process group {pgid} still has members, but none could "
            "be verified as this run's; not signalled"
        )
    killpg = getattr(os, "killpg", None)
    getpgrp = getattr(os, "getpgrp", None)
    if killpg is None or getpgrp is None:
        return f"no process-group signal on this platform for process group {pgid}; not signalled"
    if pgid <= 1 or pgid == getpgrp():
        return f"refusing broadcast/self process group {pgid} for exited pid {pid}; not signalled"
    try:
        killpg(pgid, platform_compat.SIGKILL)
    except ProcessLookupError:
        logger.debug("%s: process group %d emptied before the signal for %s", who, pgid, key)
        return None
    except OSError as exc:
        failure = failure_name(exc)
        logger.error(
            "%s: SIGKILL of process group %d (exited leader %d) failed for %s: %s",
            who,
            pgid,
            pid,
            key,
            failure,
        )
        return failure
    logger.warning(
        "%s: killpg for process group %d whose leader %d had exited, for %s", who, pgid, pid, key
    )
    return None


#: The ACP client's child-process helpers, in the order the client defines them:
#: ``(capture_child_records, get_child_pids, kill_escaped_children)`` -- the same
#: triple ``SessionLifecycleDeps.get_child_process_helpers`` resolves for the
#: session teardown. Callers resolve them at call time (the client module imports
#: the session manager, which imports the callers): this module never reaches the
#: ACP layer itself, and the escaped-children sweep it runs is the client's own.
ChildHelpers = tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]]


async def kill_verified_process(
    handle: ProcessHandle, *, who: str, key: str, child_helpers: ChildHelpers
) -> str | None:
    """SIGKILL the process ``handle`` names, verified, and return what stopped it.

    ``handle`` is the ONLY thing that names the process: the reset pops the
    session from the manager's map before it can hang, and a session found under
    the key afterwards is a successor a cold start registered during the reset's
    awaits -- a different process, whose kill would leave the run's own alive
    while its record said reaped. ``key`` names the run in the log only; ``who``
    prefixes the log lines with the caller's name; ``child_helpers`` are the
    client's child-tree probe, record capture and escaped-children sweep
    (:data:`ChildHelpers`), resolved by the caller.

    The root's start id is read twice against the recorded one: before anything
    reads through the pid (a fresh child probe of a recycled pid would enlist
    another process's children) and again immediately before the signal,
    because the child walk awaits and the process can exit -- and the kernel
    hand its pid to another process -- while it does. A leader found gone at
    any of those points, or at the signal itself, hands the decision to its
    TREE (:func:`_finish_gone_leader`): on POSIX the group it led is signalled
    by the id retained while it was alive once a verified member vouches for
    it, an empty group is nothing to kill, and a live group that cannot be
    verified is a failure; on Windows a pending exact-tree cleanup pin is
    drained, and only a completed drain is a kill.

    Returns None once the run's process tree has been signalled or shown
    gone, and otherwise the failure, named as :func:`failure_name` names one
    (``"ValueError: kill_process_tree: refusing …"``), for the run's terminal
    record: the broadcast guard's refusal of the pid, an error the kill raised
    with no pid-scoped fallback landing (on Windows, any error other than a
    gone tree: the fallback signals the root alone and nothing sweeps the
    descendants there), a live pid whose identity could not be confirmed as
    this run's (no start id recorded, or none readable now), a live group that
    could not be verified as this run's, a Windows tree whose pending cleanup
    could not be drained, or an error in the kill path ahead of the signal.
    This never raises -- the caller took the run's claim and must still finish
    it -- but it never swallows either: a process tree left alive is the
    caller's to record, so its audit does not say the run was reaped.

    Nothing to kill is not a failure and also returns None: no usable pid on
    the handle, and a leader whose tree is gone -- exited or recycled before
    the kill, at either identity read, or between the last read and the
    signal. The escaped-children sweep of the recorded children still runs in
    every case.

    The escaped-children sweep is POSIX-only by nature: on Windows
    ``_direct_children`` returns ``[]`` and ``_kill_escaped_children`` is a
    no-op, because ``taskkill /T`` walks the tree itself while it can reach the
    root, and what outlives the root there is reached through the exact-tree
    cleanup pin the spawn reserved (see :func:`_finish_gone_leader`) -- a root
    that is gone with no pin pending leaves nothing this code can enumerate,
    verify or signal, a platform limitation of the child helpers, not a verdict
    this per-run record can make. What is reported on Windows is the case with
    evidence: a tree kill that raised while the root was alive, whose root-only
    fallback cannot stand in for the walk, or a pending cleanup that would not
    drain.

    Async so the Windows ``taskkill`` spawn and the exact-tree drain offload to
    :func:`kiro_crew.executors.subprocess_executor` via
    :func:`platform_compat.kill_process_tree_async` / ``kill_pid_async`` /
    ``kill_process_tree_pinned`` instead of blocking the caller's event loop.
    The child-tree probe helpers (``_get_child_pids`` / ``_get_start_time`` /
    ``_read_basename``) also shell out to ``ps`` / ``pgrep`` on macOS, so they
    are offloaded to the same executor; the start-id read and ``os.killpg`` are
    in-process on every platform.
    """
    try:
        _capture_child_records, _get_child_pids, _kill_escaped_children = child_helpers

        pid = handle.pid
        if not pid:
            logger.warning("%s: no usable PID for %s", who, key)
            return None
        loop = asyncio.get_running_loop()
        # The children the client had recorded (start id + basename each):
        # the escaped-children sweep verifies every one before signalling.
        child_pids: dict = dict(handle.child_pids)
        failure: str | None = None
        # Validate the root by its recorded start id BEFORE anything else
        # reads through the pid: a fresh child probe of a recycled pid
        # would enlist another process's children. The start id is read
        # the way the client recorded it at spawn
        # (``platform_compat.get_process_start_id``), in-process.
        actual_start = platform_compat.get_process_start_id(pid)
        if actual_start is None and not platform_compat.pid_exists(pid):
            # The leader already exited. Its group may not have: a late child
            # spawn keeps members under the retained group id, so the group
            # decides. The sweep still runs for recorded children that outlived
            # it (POSIX only -- on win32 no descendant of a gone root is
            # reachable at all).
            logger.debug("%s: PID %d already dead for %s", who, pid, key)
            failure = await _finish_gone_leader(handle, who=who, key=key)
            await loop.run_in_executor(subprocess_executor(), _kill_escaped_children, child_pids)
            return failure
        if handle.start_id is None or actual_start is None:
            # A live process whose identity cannot be confirmed as this
            # run's -- the client recorded no start id, or none is readable
            # now -- is not signalled (deny-by-default, as the child sweep
            # does) and is not gone either: the caller records the failure.
            logger.error(
                "%s: PID %d is alive but unverified for %s (recorded %r, read %r)",
                who,
                pid,
                key,
                handle.start_id,
                actual_start,
            )
            await loop.run_in_executor(subprocess_executor(), _kill_escaped_children, child_pids)
            return f"pid {pid} is alive but could not be verified as this run's; not signalled"
        if actual_start != handle.start_id:
            # Recycled: the run's leader is gone and another process owns the
            # pid now. The group it led decides (the pid is not read through
            # again); only the recorded children are swept.
            logger.warning("%s: PID %d recycled for %s, not signalling it", who, pid, key)
            failure = await _finish_gone_leader(handle, who=who, key=key)
            await loop.run_in_executor(subprocess_executor(), _kill_escaped_children, child_pids)
            return failure
        # The root is ours: snapshot its live child tree before killing --
        # children in different PGIDs survive killpg. The macOS pgrep/ps
        # spawns happen on the subprocess_executor so the loop keeps ticking.
        fresh = await loop.run_in_executor(subprocess_executor(), _get_child_pids, pid)
        new_pids = [p for p in fresh if p not in child_pids]
        if new_pids:
            child_pids.update(
                await loop.run_in_executor(subprocess_executor(), _capture_child_records, new_pids)
            )
        # The walk above awaited (an executor hop everywhere, ``ps`` /
        # ``pgrep`` spawns on macOS), and the root can exit -- and the
        # kernel hand its pid to another process -- while it does; a
        # ``killpg`` through the pid then would signal that process's
        # group. Read the start id again immediately before the signal: a
        # reading that differs is a root that is gone, whose group decides,
        # and the fresh records above were read through a pid that is not
        # this run's any more, so only the children recorded before the
        # reset are swept.
        if platform_compat.get_process_start_id(pid) != handle.start_id:
            logger.warning(
                "%s: PID %d exited during the child walk for %s, deciding by its group",
                who,
                pid,
                key,
            )
            failure = await _finish_gone_leader(handle, who=who, key=key)
            await loop.run_in_executor(
                subprocess_executor(), _kill_escaped_children, dict(handle.child_pids)
            )
            return failure
        # Kill the entire process group first
        logger.warning("%s: killpg for PID %d (%d children) for %s", who, pid, len(child_pids), key)
        try:
            # killpg(getpgid) on POSIX, taskkill /T on Windows — routed
            # through platform_compat, whose POSIX path carries the
            # broadcast guard (refuses pgid<=1 / own group; see
            # platform_compat.kill_process_tree). Async variant offloads
            # Windows taskkill to subprocess_executor so the caller's loop
            # never blocks the event loop on taskkill.exe.
            await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
        except ValueError as exc:
            # Guard refused the pid outright (non-int/reserved) — nothing
            # safe to signal, so the group is still alive: report it.
            logger.error("%s: kill guard refused pid %r for %s", who, pid, key)
            failure = failure_name(exc)
        except ProcessLookupError:
            # The leader exited between the last identity read and the
            # signal (``getpgid`` has no leader to answer for) -- or, on
            # Windows, the tree is gone (``taskkill`` rc 128). The leader's
            # absence is not the group's: the retained group id decides.
            failure = await _finish_gone_leader(handle, who=who, key=key)
        except OSError as group_exc:
            # EPERM, a taskkill failure: the group's members may still stand,
            # so the pid-scoped fallback has to land for this kill to count.
            try:
                await platform_compat.kill_pid_async(pid, platform_compat.SIGKILL)
            except ProcessLookupError:
                # The pid is gone as well; the members the group signal could
                # not reach still stand.
                failure = failure_name(group_exc)
            except OSError as pid_exc:
                failure = failure_name(pid_exc)
            else:
                # The fallback landed on the root. On POSIX the sweep
                # below reaches the children the group signal missed, so
                # the kill counts. On Windows ``taskkill /T`` was the only
                # tree walker: ``kill_pid_async`` is a root-only
                # ``taskkill /PID`` and ``_kill_escaped_children`` is a
                # no-op there, so the descendants a tree kill that raised
                # could not terminate still stand -- still a failure.
                if platform_compat.IS_WINDOWS:
                    failure = failure_name(group_exc)
        if failure is not None:
            logger.error("%s: SIGKILL of pid %d failed for %s: %s", who, pid, key, failure)
        await loop.run_in_executor(subprocess_executor(), _kill_escaped_children, child_pids)
        return failure
    except Exception as exc:
        # An error ahead of the signal (a child-tree probe, the import,
        # the executor) or in the escaped-children sweep: the kill did not
        # happen as intended, and the caller's record has to say so.
        logger.exception("%s: SIGKILL failed for %s", who, key)
        return failure_name(exc)
