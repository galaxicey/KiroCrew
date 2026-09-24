"""Redaction against what a chat platform will DISPLAY, not the bytes it is sent.

Every channel scans outbound text for credentials, but a scan of the literal
bytes is not enough on any platform that renders markup away: ``AKIA**REST**``
and ``[AKIA](https://x)REST`` match no credential pattern as written, yet the
reader sees an intact key once the delimiters are stripped at render time. The
transformation happens AFTER the scan, so the scan has to anticipate it.

This lives in ``messaging`` rather than in one channel package because the
hazard is not Slack-specific: Telegram (MarkdownV2) and Discord (Markdown)
collapse the same emphasis, code-span and link syntax. It was written for
Slack first and hoisted here when :func:`kiro_crew.messaging.renderer.
format_overflow` began putting LLM-authored choice text into the message BODY
on every widget-capable channel -- the shared sink cannot depend on each
renderer remembering to canonicalise, which is the same reasoning that put the
``max_buttons`` cap in shared code.

Stdlib-only leaf: it takes the redactor as a parameter rather than importing
``kiro_crew.security``, so it stays importable from anywhere and each caller
keeps its own (possibly session-scoped) redactor.
"""

from __future__ import annotations

import re
from typing import Callable

from kiro_crew.preview_text import drop_format_chars

_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")

# Delimiter runs the platforms consume at render time. ``||`` is Discord's
# spoiler: the reader clicks it and the delimiters vanish, joining the halves --
# the same splitter property as ``**``, which is why it belongs in this run
# rather than in a pass of its own.
#
# The pipe counts only in PAIRS. A lone ``|`` is literal text on every channel
# here (Telegram's body goes out as HTML, where ``||`` is not spoiler markup
# either, and Slack renders it as-is), so collapsing single pipes would only
# widen the canonical form for no display that matches it. Slack link internals
# (``<url|label>``) are already consumed by ``_SLACK_LINK`` before this runs.
_EMPHASIS_RUN = re.compile(r"(?:[*_~`]|\|\|)+")
# ``[label](url)`` (Markdown) and ``<url|label>`` (Slack mrkdwn). Both DISPLAY only
# the label, so the url is invisible to a reader and the label joins whatever
# surrounds it -- which makes them a splitter, exactly like ``**``.
#
# The opening delimiter is excluded from every inner class (no ``[`` inside the
# Markdown label, no ``<`` inside the Slack one). That is not cosmetic: with ``[``
# allowed, input like ``[[[[[[...`` makes each start position consume the whole
# remaining string before failing to find ``]``, so the scan is quadratic in the
# length of attacker-supplied text (CodeQL ``py/polynomial-redos``). Excluding it
# makes a failed start fail immediately, which matters because this runs on every
# outbound message. A label containing a literal ``[`` is simply not collapsed --
# safe, since the fallback is to scan the text as written.
_MD_LINK = re.compile(r"\[([^\[\]\n]*)\]\(([^()\n]*)\)")
_SLACK_LINK = re.compile(r"<([^<>|\n]*)\|([^<>\n]*)>")
# Markers a platform consumes at the START of a line, leaving NO character in
# their place: Markdown headings, Discord's ``-# `` small text, and blockquotes.
# Same splitter property as ``**`` -- the marker vanishes at render time, so the
# text after it joins whatever the reader has directly above.
#
# List bullets (``- ``, ``* ``, ``1. ``) are deliberately absent: the platform
# SUBSTITUTES a visible bullet or number rather than removing the marker, so a
# character still stands between the two halves on screen. Six heading levels
# even where a channel implements three, because the canonical form must not
# depend on which subset a client supports and a wider removal only ever
# withholds more.
#
# One pass is enough although this runs last. The match is anchored at a line
# start, so removing it cannot bring two characters together that were not
# already adjacent -- there is nothing before it on the line to join to -- and
# no new inline delimiter run can form out of the removal. Repeating the group
# handles a blockquote that itself carries a heading (``> # title``).
#
# The blockquote is the one family whose trailing space is OPTIONAL, because a
# channel consumes ``>`` with or without it: this repo's own Telegram converter
# matches ``^&gt;[ \t]?`` and puts nothing in its place, so ``>KEY`` reaches the
# reader as ``KEY``. A heading and Discord's small text keep the space required,
# which is what their own parsers demand -- ``#KEY`` is literal text on screen,
# and treating it as a marker would withhold characters over a pair that shows a
# ``#`` between the halves.
#
# The indent stops at three, where every parser here stops: this repo's own
# heading rule is ``^\s{0,3}#{1,6}\s+``, and a fourth space makes the line
# INDENTED CODE, which a channel shows verbatim marker and all. Admitting any
# indent is the one way this family can be too wide rather than too narrow, and
# too wide costs real characters -- a four-space ``# text`` under a frozen
# credential prefix would be withheld and replaced by the tag, losing output
# that was never at risk.
_BLOCK_MARKER = re.compile(r"(?m)^[ \t]{0,3}(?:(?:#{1,6}|-#)[ \t]+|>{1,3}[ \t]*)+")

#: The same family for a channel that shows ``-#`` LITERALLY. Small text is
#: Discord's own markup, and a channel that does not implement it puts those two
#: characters on screen between the halves -- so removing them from the canonical
#: form withholds characters over a pair the reader never sees joined. Measured on
#: this repo's own Telegram converter: ``-# text`` reaches the reader verbatim,
#: while ``# text`` becomes ``<b>text</b>`` and ``> text`` a ``<blockquote>``, both
#: with the marker consumed. A caller that knows its channel passes this; the wide
#: default stays for :func:`redact_for_display`, whose floor serves every channel
#: at once and can only err toward withholding.
BLOCK_MARKERS_WITHOUT_SMALL_TEXT = re.compile(r"(?m)^[ \t]{0,3}(?:#{1,6}[ \t]+|>{1,3}[ \t]*)+")


def strip_ansi(text: str) -> str:
    """Remove SGR colour escapes.

    Public because redaction call sites need it: this strip can *reassemble* a
    credential that escape sequences had broken up, so a caller that redacts
    around a conversion has to normalise with the SAME function first, or the
    secret slips through the regex and is put back together afterwards.
    """
    return _ANSI_SGR.sub("", text)


def _strip_format_chars(text: str) -> str:
    """Drop Unicode *format* characters (category ``Cf``) and soft hyphens.

    The delimiter families above are visible markup a platform consumes. This is
    the invisible half of the same hazard, and it is strictly worse: a
    zero-width space, joiner, bidi mark or BOM between two halves of a key is
    rendered as NOTHING, so the reader sees an intact credential with no click
    and no markup, while every literal scan sees it broken. ``Cf`` is the
    principled set -- it is exactly Unicode's "format" category (ZWSP, ZWNJ,
    ZWJ, word joiner, bidi controls, BOM, soft hyphen) and contains nothing a
    reader can see.

    Delegates to :func:`kiro_crew.preview_text.drop_format_chars`, the one
    implementation of the Cf drop (its docstring carries the fast-path
    soundness argument), so the display-safety canonicalizer and the preview
    stripper cannot drift apart on which characters count as invisible.
    """
    return drop_format_chars(text)


def canonicalize_display(text: str, *, block_markers: "re.Pattern[str]" = _BLOCK_MARKER) -> str:
    """Reduce *text* to what the platform will actually SHOW a reader.

    Four families, one property: the platform removes them at render time, so a
    credential broken across them is whole on screen while every literal scan
    sees it broken.

    * **links** collapse to their label -- ``[AKIA](https://x)REST`` displays as
      the joined key, with the url nowhere in sight;
    * **emphasis / code / spoiler delimiters** vanish -- ``AKIA**REST**`` and
      Discord's ``AKIA||REST||`` likewise;
    * **invisible format characters** were never rendered at all -- see
      :func:`_strip_format_chars`;
    * **line-leading block markers** -- a heading, Discord's small text or a
      blockquote -- are consumed with nothing left in their place, so a message
      opening ``# REST`` shows only ``REST`` under whatever precedes it.

    Links are reduced FIRST: a url can itself contain ``_`` or ``~``, and dropping
    those before the url is removed would corrupt the label boundaries. Delimiters
    go next and invisible characters after them, which is safe in either order
    because the delimiter pattern matches a SINGLE delimiter as readily as a run:
    ``*``+ZWSP+``*`` loses each ``*`` on its own, so nothing depends on the
    zero-width character being gone first.
    Block markers are removed LAST, because every earlier family can be what
    reveals one: ``[#](url) REST`` is a heading only after the link collapses.
    """
    out = _MD_LINK.sub(r"\1", text)
    out = _SLACK_LINK.sub(r"\2", out)
    out = _EMPHASIS_RUN.sub("", out)
    return block_markers.sub("", _strip_format_chars(out))


def joins_to_a_credential(
    head: str,
    tail: str,
    redactor: Callable[[str], str],
    *,
    block_markers: "re.Pattern[str]" = _BLOCK_MARKER,
) -> bool:
    """Would a reader shown *head* and then *tail* see a key neither half holds?

    A cap that cuts text into two messages is applied to the RAW string, while the
    reader sees the CANONICAL rendering of each piece. So a credential the model
    split with markup can be severed by the cut: each piece is scrubbed on its own
    and matches nothing, and the reader's client renders the markup away and
    rejoins the halves on screen.

    This answers the question directly rather than guessing which characters could
    hide such a split. Each side is put through the same redaction the sender will
    actually apply (:func:`redact_for_display`), then reduced to what the platform
    SHOWS (:func:`canonicalize_display`), and the result of putting the two sides
    together is scanned.

    Soundness, which is the whole point: ``redact_for_display`` already emits the
    canonical form whenever canonicalising reveals something the literal form hid,
    so ``redactor(canonicalize_display(redact_for_display(x)[0]))`` is a fixed
    point for any single string ``x``. Whatever this scan finds is therefore
    produced by putting the two sides together and by nothing else. No character
    class, no window and no
    anchor list: a search window built from a hand-written set of characters cannot
    be closed, because the next character the set does not know about is one more
    place a split can hide -- the walk that finds the window's edge stops there, the
    check runs on a span the credential's prefix was never inside, and it passes
    vacuously.

    BOTH readings a reader can produce are scanned, because neither one contains
    the other:

    * **canonicalise the join** models a COPY of both messages, and a client
      lenient about where one message ends. It is the wider reading for runs of
      delimiters, which concatenation can only extend: ``AKIA**`` beside
      ``**REST`` is a run only once the halves sit together.
    * **canonicalise each side, then join** models the screen -- two messages
      rendered separately, read one after the other. This is the wider reading
      wherever canonicalising DROPS text rather than just deleting delimiters,
      which is exactly what a link does to its target. A cut one character inside
      ``[l](https://x/AKIA`` + ``REST)`` completes the link only in the join,
      where the url then collapses to the label and the key vanishes from the
      scan -- while on screen each half is an unfinished link whose url stays
      visible, and the reader reads straight through it.

    So the join alone would pass a cut through a credential in a url, and the
    per-side reading alone would miss a credential split by markup at the
    boundary. Either reading finding something is enough to refuse the cut, and
    ``test_display_split_safety.py`` pins one shape per reading.
    """
    head_safe = redact_for_display(head, redactor)[0]
    tail_safe = redact_for_display(tail, redactor)[0]
    readings = (
        canonicalize_display(head_safe + tail_safe, block_markers=block_markers),
        canonicalize_display(head_safe, block_markers=block_markers)
        + canonicalize_display(tail_safe, block_markers=block_markers),
    )
    return any(redactor(reading) != reading for reading in readings)


def safe_split_offset(text: str, limit: int, redactor: Callable[[str], str]) -> int:
    """The largest SAMPLED offset at or below *limit* that severs no credential.

    Not the largest safe offset: the candidates are sampled, so a safe offset
    between two samples is passed over. Those characters are not lost, only
    deferred to the next delivery.

    Used by a renderer whose message cap forces *text* into two deliveries: cut
    here and :func:`joins_to_a_credential` is false, so the reader cannot rejoin a
    key across the boundary.

    Candidates step back EXPONENTIALLY (``limit``, then 1, 2, 4, 8 ... characters
    before it), for a cost bound: the nearest safe boundary is not needed, only a
    safe one, and stepping past it merely defers a few more characters to the next
    delivery. A linear walk would be O(*limit*) redaction passes over
    attacker-influenced text on every frame; this is O(log *limit*), and the common
    case -- prose, where any cut is safe -- costs one pass, or none at all when
    *text* already fits.

    ``0`` means every SAMPLED candidate was unsafe -- one matched region covers all
    of them. A safe offset between two samples may still exist; the search does not
    look for it, because the answer it needs is only "is there a safe cut I can take
    now". Callers treat ``0`` as "deliver nothing yet", which is always available to
    them: text withheld now is text the next delivery carries.
    """
    if limit <= 0:
        return 0
    if limit >= len(text):
        # Nothing is severed, so there is no boundary to check.
        return len(text)
    offset, step = limit, 0
    while offset > 0:
        if not joins_to_a_credential(text[:offset], text[offset:], redactor):
            return offset
        step = 1 if step == 0 else step * 2
        offset = limit - step
    return 0


#: Stands in for the characters withheld at a seam a reader could otherwise
#: rejoin. The same tag the credential redactor emits, so a piece carrying it is
#: already a fixed point of the scan (``test_display_split_safety.py`` pins that)
#: and the breaker cannot introduce something that scans as a secret itself.
CREDENTIAL_SEAM_TAG = "[REDACTED: credential]"


def break_credential_seam(
    prior: str,
    text: str,
    redactor: Callable[[str], str],
    *,
    block_markers: "re.Pattern[str]" = _BLOCK_MARKER,
) -> tuple[str, bool]:
    """Make *text* safe to show directly under *prior*, which is already on screen.

    A boundary is graded by whoever CREATES it. A renderer that cuts its output
    grades its own cut, but the message above is then frozen while the text below
    it can still be replaced -- a presentation snapshot, an options expansion, a
    footer -- and the pair the reader ends up with is not the pair anyone asked
    about. *prior* cannot be taken back, so the only text left to fix is *text*.

    The failure direction is to WITHHOLD: leading characters of *text* are
    replaced by :data:`CREDENTIAL_SEAM_TAG` until
    :func:`joins_to_a_credential` is false. Losing characters beats showing a key,
    and the tag says out loud that something was held back rather than leaving a
    silent gap.

    Each candidate is graded on the TRUNCATION, without the tag in front of it.
    That is not a detail: the tag is itself a redaction placeholder, so a scanner
    rule that skips an already-redacted value reads the tag as the value and stops
    looking -- a url cut right after ``?token=`` then passes with ZERO characters
    withheld, and the reader is shown the whole token under a tag claiming it was
    held back. Grading the truncation asks the only question that matters, what the
    reader would rejoin from the characters actually left, and the tag is prepended
    to the answer afterwards.

    Candidates step back EXPONENTIALLY (1, 2, 4, 8 ... characters), the same cost
    bound :func:`safe_split_offset` takes: the smallest withholding is not needed,
    only one that works, and the tag alone is always available as the last
    candidate. Withholding NOTHING is not a candidate here -- the entry check above
    has already answered it -- so this costs O(log len(*text*)) redaction passes
    when a seam is open, and the ONE :func:`joins_to_a_credential` call
    that clears it when nothing is wrong -- which is every ordinary message.

    Returns:
        ``(safe_text, broken)``. ``broken`` is True when text was withheld, so a
        caller can count it the way it counts any other redaction.
    """
    if (
        not prior
        or not text
        or not joins_to_a_credential(prior, text, redactor, block_markers=block_markers)
    ):
        return text, False
    drop = step = 1
    while drop < len(text):
        if not joins_to_a_credential(prior, text[drop:], redactor, block_markers=block_markers):
            # The graded bytes are what ships. The check clears on the redacted form
            # of this truncation, and a SUFFIX of a fixed point need not be one
            # itself: a leading word character blocks the scanner's left boundary, so
            # dropping it can expose a credential that the whole text did not match.
            # Returning the raw truncation then puts that credential on screen right
            # behind a tag announcing that something was withheld -- and the turn's
            # notice counts the tag, so the reader is told the opposite of the truth.
            #
            # Redacting can make the result slightly LONGER than the truncation when
            # the credential is shorter than the tag. A caller's cap then trims the
            # end, which removes text and cannot join anything; the alternative is
            # shipping the key.
            return CREDENTIAL_SEAM_TAG + redact_for_display(text[drop:], redactor)[0], True
        step *= 2
        drop = step
    # Nothing of *text* survives. The tag alone cannot extend a key: it is a fixed
    # point of the scan, and *prior* was scrubbed on its own before it was sent.
    return CREDENTIAL_SEAM_TAG, True


def redact_for_display(text: str, redactor: Callable[[str], str]) -> tuple[str, bool]:
    """Redact *text* against what the platform will DISPLAY, not just the bytes.

    Two normalisations, for the same underlying reason: a transformation applied
    *after* the scan can reassemble a credential the scanner saw as broken.

    1. **ANSI escapes** -- stripped outright, because they are display noise with
       no meaning to preserve.
    2. **Link markup and emphasis/code delimiters** -- these DO carry meaning, so
       they cannot simply be deleted. Instead the canonical (display) form is
       scanned as well. Neither ``AKIA**<rest>**`` nor ``[AKIA](https://x)<rest>``
       matches a credential pattern as written, yet the platform renders the
       markup away and shows the reader an intact key.

    When the canonical form reveals a secret that the literal form hid, the
    canonical text is emitted -- so the message loses that markup. That is
    deliberate and one-directional: formatting is worth less than a credential, and
    the downgrade only happens on a message that actually contains one.

    Returns:
        ``(safe_text, redacted)``. ``redacted`` is True when the redactor changed
        anything, by either route -- callers that already published the text rely
        on it to go back and replace what is visible.
    """
    stripped = strip_ansi(text or "")
    safe = redactor(stripped)
    changed = safe != stripped

    literal = _strip_format_chars(safe)
    if literal != safe:
        literal_safe = redactor(literal)
        if literal_safe != literal:
            safe, changed = literal_safe, True
    canonical = canonicalize_display(safe)
    if canonical != safe:
        canonical_safe = redactor(canonical)
        if canonical_safe != canonical:
            # The markup was hiding a credential from the scan. Emit the canonical,
            # redacted form: losing formatting beats leaking the key.
            return canonical_safe, True
    return safe, changed
