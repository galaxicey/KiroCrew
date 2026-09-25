"""A cut may not hand the reader a credential neither chunk holds.

Two halves of a key, one per message, read as one key down the screen. The
per-chunk scan clears both halves, so nothing reports it: the reader sees the
key and the operator sees a clean send. The splitter therefore has to know about
credentials before it chooses a boundary, and every path that turns one chunk
into one message has to tell it, which is what the enumeration below enforces.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.messaging.display_safety import joins_to_a_credential
from kiro_crew.messaging.renderer import _default_redactor
from kiro_crew.messaging.split import (
    _rejoins_a_key,
    chunk_utf8_bytes,
    split_markdown_safe,
)
from kiro_crew.whatsapp.renderer import render_chunks

#: AWS's own documented example key, the same fixture the display-safety tests
#: use, so no real credential shape is introduced anywhere.
KEY = "AKIAIOSFODNN7EXAMPLE"
HEAD, TAIL = KEY[:10], KEY[10:]

SRC = Path(kiro_crew.__file__).parent

#: Every path that delivers one splitter chunk as one message. A boundary there
#: is a seam between two messages a reader reads in order, so each of these must
#: pass its redactor at every splitter call. Enumerated rather than discovered:
#: a path that maintains none of the state is invisible to a search for the
#: state's names, and those are exactly the ones left open.
DELIVERY_PATHS = (
    "slack/renderer.py",
    "webex/renderer.py",
    "whatsapp/renderer.py",
    "teams/renderer.py",
    "wecom/renderer.py",
    "dashboard/chat_mirror.py",
)

SPLITTERS = frozenset({"split_markdown_safe", "chunk_utf8_bytes"})


def _rejoins(chunks: list[str]) -> bool:
    """Does any boundary hand the reader a key neither chunk holds?

    The rendered pair, because a platform drops the whitespace at a message's
    edges: the test models the screen the same way the splitter does.
    """
    return any(
        joins_to_a_credential(chunks[i].rstrip(), chunks[i + 1].lstrip(), _default_redactor)
        for i in range(len(chunks) - 1)
    )


def _on_screen(chunks: list[str]) -> str:
    """What a reader scrolling the delivered messages reads, as one string.

    Each message's edge whitespace goes, because the platform drops it, and a
    chunk holding nothing else contributes nothing at all -- which is why it
    separates nothing.
    """
    return "".join(chunk.strip() for chunk in chunks)


def _name_of(node: ast.expr) -> str:
    """The bare name a call or reference resolves to, attribute access included."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _splitter_calls(tree: ast.AST) -> list[tuple[str, set[str]]]:
    """Every splitter call in *tree* as ``(splitter, keyword names)``.

    Both shapes count. A renderer that offloads the splitter to a worker thread
    passes it as a REFERENCE to ``asyncio.to_thread`` and hands the arguments to
    ``to_thread``, so a check that only reads direct calls sees four of these
    seven sites and reports the rest as absent rather than as unguarded.
    """
    calls: list[tuple[str, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = {kw.arg for kw in node.keywords if kw.arg}
        called = _name_of(node.func)
        if called in SPLITTERS:
            calls.append((called, keywords))
        elif called == "to_thread" and node.args:
            offloaded = _name_of(node.args[0])
            if offloaded in SPLITTERS:
                calls.append((offloaded, keywords))
    return calls


class TestEveryDeliveryPathIsEnumerated:
    """The enforcement: a path that forgets the redactor fails here."""

    @pytest.mark.parametrize("path", DELIVERY_PATHS)
    def test_the_path_still_reaches_a_splitter(self, path: str) -> None:
        calls = _splitter_calls(ast.parse((SRC / path).read_text(encoding="utf-8")))
        assert calls, (
            f"{path} reaches no splitter. If its delivery moved, move it in "
            "DELIVERY_PATHS too -- an empty list would otherwise pass the "
            "redactor check below by having nothing to check."
        )

    @pytest.mark.parametrize("path", DELIVERY_PATHS)
    def test_every_splitter_call_passes_a_redactor(self, path: str) -> None:
        calls = _splitter_calls(ast.parse((SRC / path).read_text(encoding="utf-8")))
        unguarded = [name for name, keywords in calls if "redactor" not in keywords]
        assert not unguarded, (
            f"{path} calls {unguarded} with no redactor, so the boundary is "
            "chosen by a length budget alone and a severed key reaches two "
            "adjacent messages."
        )


class TestACutCannotRejoinAKey:
    """The behaviour the enumeration protects."""

    def _across_a_line_break(self) -> str:
        """Text whose halves of a key sit on either side of a line break.

        Scanning this text as written finds nothing: the break separates the
        halves. The cut then removes the break, because sealing a chunk trims the
        whitespace that ended it.
        """
        return f"{'w ' * 18}{HEAD}\n{TAIL} and some trailing words"

    def test_the_budget_alone_hands_the_reader_the_key(self) -> None:
        """Without a redactor the seam is open -- the state this guards against."""
        chunks = split_markdown_safe(self._across_a_line_break(), 50)
        assert len(chunks) == 2
        assert KEY not in chunks[0] and KEY not in chunks[1]
        assert joins_to_a_credential(chunks[0], chunks[1], _default_redactor)
        assert KEY in "".join(chunks)

    def test_a_redactor_closes_the_seam(self) -> None:
        chunks = split_markdown_safe(self._across_a_line_break(), 50, redactor=_default_redactor)
        assert KEY not in "".join(chunks)
        assert not any(
            joins_to_a_credential(chunks[i], chunks[i + 1], _default_redactor)
            for i in range(len(chunks) - 1)
        )

    def test_each_chunk_also_reads_clean_on_its_own(self) -> None:
        """Both readings, because neither contains the other."""
        chunks = split_markdown_safe(self._across_a_line_break(), 50, redactor=_default_redactor)
        for chunk in chunks:
            assert KEY not in chunk
            assert HEAD not in chunk or TAIL not in chunk

    def test_a_key_on_one_long_line_is_a_marker_before_any_cut(self) -> None:
        text = f"{'w ' * 18}{KEY} trailing"
        chunks = split_markdown_safe(text, 50, redactor=_default_redactor)
        assert KEY not in "".join(chunks)

    def test_a_key_hidden_in_markup_is_caught_too(self) -> None:
        """The client renders the link away and joins the halves on screen."""
        text = f"{'y' * 48}[{HEAD}](https://ex.test/z){TAIL} tail"
        chunks = split_markdown_safe(text, 50, redactor=_default_redactor)
        assert KEY not in "".join(chunks)

    def test_a_reserve_still_applies(self) -> None:
        chunks = split_markdown_safe(
            self._across_a_line_break(), 50, reserve=10, redactor=_default_redactor
        )
        assert KEY not in "".join(chunks)

    def test_a_key_split_at_a_space_does_not_reach_two_messages(self) -> None:
        """A space at a boundary is as invisible as a line break.

        With the space in place the text is not a credential, so no scan of it
        objects. The cut lands on the space, the seal takes it, and the halves sit
        flush on screen.
        """
        text = f"{'w ' * 12}{HEAD} {TAIL} and some trailing words"
        unguarded = split_markdown_safe(text, 35)
        assert len(unguarded) == 2
        assert KEY not in unguarded[0] and KEY not in unguarded[1]
        assert KEY in unguarded[0].rstrip() + unguarded[1].lstrip()
        guarded = split_markdown_safe(text, 35, redactor=_default_redactor)
        assert not _rejoins(guarded)
        assert KEY not in "".join(part.strip() for part in guarded)

    def test_moving_the_cut_keeps_every_character(self) -> None:
        """The repair searched for a budget before giving anything up."""
        text = f"{'w ' * 12}{HEAD} {TAIL} and some trailing words"
        guarded = split_markdown_safe(text, 35, redactor=_default_redactor)
        assert "".join(guarded) == text, "no character was dropped or collapsed"

    def test_a_key_no_budget_can_cut_safely_is_redacted(self) -> None:
        """The terminal: the key spans every boundary the budget can offer."""
        text = " ".join(KEY[i : i + 2] for i in range(0, len(KEY), 2))
        guarded = split_markdown_safe(text, 6, redactor=_default_redactor)
        assert not _rejoins(guarded)
        assert KEY not in "".join(part.strip() for part in guarded)

    def test_prose_is_returned_byte_for_byte(self) -> None:
        """The reduction is one-directional: clean text keeps its breaks."""
        text = "first line of prose\n\nsecond paragraph here\nand a third line"
        assert split_markdown_safe(text, 24, redactor=_default_redactor) == split_markdown_safe(
            text, 24
        )

    def test_a_break_no_boundary_falls_on_keeps_its_content(self) -> None:
        """The repair is keyed on a CHOSEN boundary, not on every candidate.

        Two lines that read as one key when glued are the shape that makes a
        candidate-by-candidate scan destructive: no single cut ever joins them,
        yet a scan of every break at once sees a key and the whole message loses
        its structure. Here the cut falls after the prose, so the break between
        the halves is never a boundary and the text comes back as written --
        inside one message a line break is a line break, which the reader sees.
        """
        text = f"{'prose words ' * 6}\n{HEAD}\n{TAIL}\n"
        guarded = split_markdown_safe(text, 80, redactor=_default_redactor)
        assert guarded == split_markdown_safe(text, 80)
        assert len(guarded) > 1, "the fixture is meant to have a real boundary"
        assert any("\n" in chunk for chunk in guarded)
        assert HEAD in "".join(guarded) and TAIL in "".join(guarded)


class TestTheByteBudgetCarriesTheSameGuarantee:
    """Webex measures bytes, and a byte budget knows nothing about keys either."""

    def test_the_budget_alone_hands_the_reader_the_key(self) -> None:
        chunks = chunk_utf8_bytes(f"{'x' * 50}{KEY} trailing", 60)
        assert KEY not in chunks[0] and KEY not in chunks[1]
        assert KEY in "".join(chunks)

    def test_a_redactor_closes_the_seam(self) -> None:
        chunks = chunk_utf8_bytes(f"{'x' * 50}{KEY} trailing", 60, redactor=_default_redactor)
        assert KEY not in "".join(chunks)

    def test_a_key_split_at_a_space_is_caught_here_too(self) -> None:
        text = f"{'w ' * 10}{HEAD} {TAIL} and trailing"
        unguarded = chunk_utf8_bytes(text, 30)
        assert KEY not in unguarded[0] and KEY not in unguarded[1]
        assert KEY in unguarded[0].rstrip() + unguarded[1].lstrip()
        guarded = chunk_utf8_bytes(text, 30, redactor=_default_redactor)
        assert not _rejoins(guarded)
        assert KEY not in "".join(part.strip() for part in guarded)

    def test_clean_text_reassembles_exactly(self) -> None:
        text = "a table row | another cell | a third cell that makes this long"
        assert "".join(chunk_utf8_bytes(text, 20, redactor=_default_redactor)) == text

    def test_a_kept_newline_is_no_separator_once_rendered(self) -> None:
        """This splitter keeps the break; the client drops it when it renders.

        So a chunk ending in a newline is not a safe boundary just because the
        characters still hold one, and the grade has to read the stripped pair.
        """
        text = f"{'x' * 56}{HEAD}\n{TAIL} and trailing words"
        unguarded = chunk_utf8_bytes(text, 66)
        assert len(unguarded) == 2
        assert KEY not in unguarded[0] and KEY not in unguarded[1]
        assert KEY in unguarded[0].rstrip() + unguarded[1].lstrip()
        guarded = chunk_utf8_bytes(text, 66, redactor=_default_redactor)
        assert KEY not in "".join(part.strip() for part in guarded)


class TestAKeySpanningMoreThanTwoChunks:
    """A narrow budget puts a key across three chunks, or more.

    Each neighbouring pair then holds fragments that match nothing, so a grade
    asking only about neighbours clears every boundary while the screen shows the
    key whole. The reading that sees it is the whole sequence, which is what the
    grade has to be over.
    """

    #: Ten characters of key on either side of the run, so no budget at or below
    #: the ones used here can hold the whole key in one chunk.
    TEXT = f"aa{HEAD}{' ' * 10}{TAIL}bb"

    def test_neighbouring_pairs_alone_clear_a_key_on_screen(self) -> None:
        """The state a pairwise grade cannot see, measured on the raw splitter."""
        chunks = chunk_utf8_bytes(self.TEXT, 10)
        assert len(chunks) >= 3
        assert not _rejoins(chunks), "every neighbouring pair reads clean"
        assert all(KEY not in chunk for chunk in chunks), "no chunk holds the key"
        assert KEY in _on_screen(chunks), "yet the reader sees it whole"

    def test_the_byte_splitter_refuses_that_split(self) -> None:
        chunks = chunk_utf8_bytes(self.TEXT, 10, redactor=_default_redactor)
        assert KEY not in _on_screen(chunks)

    def test_the_character_splitter_refuses_that_split(self) -> None:
        chunks = split_markdown_safe(self.TEXT, 10, redactor=_default_redactor)
        assert KEY not in _on_screen(chunks)

    @pytest.mark.parametrize("budget", [6, 8, 10, 12, 14])
    def test_no_narrow_budget_delivers_the_key(self, budget: int) -> None:
        for chunks in (
            chunk_utf8_bytes(self.TEXT, budget, redactor=_default_redactor),
            split_markdown_safe(self.TEXT, budget, redactor=_default_redactor),
        ):
            assert KEY not in _on_screen(chunks)
            assert all(KEY not in chunk for chunk in chunks)

    def test_a_chunk_that_renders_to_nothing_is_no_separator(self) -> None:
        """The lossless splitter can place a whitespace-only chunk between halves.

        It renders to nothing, so it separates nothing on screen, and both of its
        own boundaries read clean against it.
        """
        text = f"aa{HEAD}{' ' * 40}{TAIL}bb"
        unguarded = chunk_utf8_bytes(text, 12)
        assert any(not chunk.strip() for chunk in unguarded), "a blank chunk exists"
        assert not _rejoins(unguarded)
        assert KEY in _on_screen(unguarded)
        guarded = chunk_utf8_bytes(text, 12, redactor=_default_redactor)
        assert KEY not in _on_screen(guarded)

    def test_the_grade_reads_the_whole_sequence(self) -> None:
        """Directly: three chunks, every neighbouring pair clean, key on screen.

        The middle chunk is a fragment of the key itself, so it matches nothing on
        its own and nothing when read against either neighbour.
        """
        chunks = [f"aa{KEY[:8]}", KEY[8:14], f"{KEY[14:]}bb"]
        assert _on_screen(chunks) == f"aa{KEY}bb"
        assert not _rejoins(chunks), "the neighbour reading clears it"
        assert _rejoins_a_key(chunks, _default_redactor)

    def test_innocent_text_keeps_its_whitespace(self) -> None:
        """The wider grade may not push ordinary prose onto the flush path."""
        prose = " ".join(["word"] * 40)
        chunks = split_markdown_safe(prose, 30, redactor=_default_redactor)
        assert len(chunks) > 1
        assert " ".join(chunks).split() == prose.split()


class TestTheFramesAChannelActuallySends:
    """One channel end to end, through the function its client sends from."""

    def test_whatsapp_delivery_chunks_cannot_be_rejoined(self) -> None:
        frames = render_chunks(f"{'w ' * 18}{HEAD}\n{TAIL} and some trailing words", 50)
        assert frames
        assert KEY not in "".join(frames)
        for frame in frames:
            assert KEY not in frame
        assert not any(
            joins_to_a_credential(frames[i], frames[i + 1], _default_redactor)
            for i in range(len(frames) - 1)
        )
