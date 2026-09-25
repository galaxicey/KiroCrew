"""Integration tests for binary file support in outbox notify + download handlers."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import file_delivery_consent
from kiro_crew.dashboard.handlers import api_outbox_download, api_outbox_notify


def _synth_flagged_png() -> bytes:
    """PNG-shaped bytes that fail UTF-8 decode and carry key-SHAPED material.

    Assembled at runtime from fragments rather than checked in as a literal, the
    same convention ``test_file_delivery_consent`` documents: a diff carrying
    literal PEM headers reads as an exfiltration recipe to a review provider, and
    the scanner sees identical bytes either way. The body is deterministic base64
    over a digest, so it is private-key-SHAPED without being a private key.

    ``\\xff\\xfe`` is what makes the UTF-8 decode raise, which is the branch under
    test; ``.png`` puts the guessed MIME type inside the allow-list so the bytes
    reach the content scan rather than stopping at the type check.
    """
    rule = "-" * 5
    begin = " ".join(["BEGIN", "RSA", "PRIVATE", "KEY"])
    end = " ".join(["END", "RSA", "PRIVATE", "KEY"])
    body = "\n".join(
        base64.b64encode(hashlib.sha256(f"kc-8779-{i}".encode()).digest() * 2).decode()
        for i in range(4)
    )
    pem = f"{rule}{begin}{rule}\n{body}\n{rule}{end}{rule}\n"
    return b"\x89PNG\r\n\x1a\n\xff\xfe" + pem.encode() + b"\x80\x81"


def _clean_png() -> bytes:
    return b"\x89PNG\r\n\x1a\n\xff\xfe" + b"\x00" * 200


#: Wide encodings a credential can be written in inside an allow-listed
#: container: both widths, both byte orders. An ID3v2 tag in ``audio/mpeg`` is
#: UTF-16 and an ``application/pdf`` text string is commonly UTF-16BE, so these
#: are what standard writers emit rather than a crafted shape.
_WIDE_ENCODINGS = ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")


def _pem_text() -> str:
    """Key-SHAPED PEM text, assembled at runtime for the reason above.

    Same convention as :func:`_synth_flagged_png`: literal PEM headers in a diff
    read as an exfiltration recipe to a review provider, and the scanner sees
    identical bytes either way.
    """
    rule = "-" * 5
    begin = " ".join(["BEGIN", "RSA", "PRIVATE", "KEY"])
    end = " ".join(["END", "RSA", "PRIVATE", "KEY"])
    body = "\n".join(
        base64.b64encode(hashlib.sha256(f"kc-8779-wide-{i}".encode()).digest() * 2).decode()
        for i in range(4)
    )
    return f"{rule}{begin}{rule}\n{body}\n{rule}{end}{rule}\n"


def _synth_wide_utf8_pdf(encoding: str) -> bytes:
    """PDF-shaped bytes that DECODE AS UTF-8 and carry the key at wide spacing.

    The distinction from :func:`_synth_flagged_png` is the whole point: that one
    carries ``\\xff\\xfe`` so the UTF-8 decode raises and the bytes take each
    gate's binary branch. Every byte here is either ASCII or NUL, and NUL is
    itself valid UTF-8, so these bytes decode cleanly and take the TEXT branch --
    where the key's characters arrive NUL-separated and no contiguous-ASCII
    detector matches them.

    Explicit ``-le``/``-be`` spellings keep a byte-order mark off the front, so
    the run starts where this function says it does and the buffer stays
    UTF-8-decodable.
    """
    return b"%PDF-1.7\n" + _pem_text().encode(encoding) + b"\n%%EOF\n"


def _make_app(state=None) -> web.Application:
    app = web.Application()
    app["state"] = state or MagicMock(_slots={})
    app.router.add_post("/api/outbox/notify", api_outbox_notify)
    app.router.add_get("/api/outbox/{filename}", api_outbox_download)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.files._sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.fixture(autouse=True)
def isolated_consent_store(tmp_path, monkeypatch):
    """Point the consent store at a tmp file so the host's real grant cannot leak in.

    Without this a machine that happens to hold an ``owner_dashboard`` grant would
    turn every "refused without a grant" assertion below green for the wrong
    reason.
    """
    store = tmp_path / "file_delivery_consent.json"
    monkeypatch.setattr(
        file_delivery_consent, "file_delivery_consent_path", lambda: store, raising=True
    )
    return store


def _grant() -> None:
    file_delivery_consent.record_grant(
        file_delivery_consent.CLASS_OWNER_DASHBOARD, granted_at="2026-09-05T00:00:00+00:00"
    )


@pytest.fixture
def outbox(tmp_path):
    # Not ``tmp_path``: on macOS pytest's basetemp carries the per-user
    # ``/var/folders/<..30 random chars..>/T`` segment, which trips the bare-secret
    # heuristic in redact_credentials() and has api_outbox_notify reject the path
    # with 400 before any test logic runs.
    #
    # Not a literal ``/tmp`` either: the rootdir conftest already gives the run
    # its own ``tempfile`` base -- ``/tmp/kc-pytest-<user>-<pid>-<rand>`` on macOS,
    # ``$TMPDIR/kc-pytest-...`` elsewhere -- short, low-entropy, removed at session
    # end and RESIDUE-REPORTED, whereas a directory dropped straight into the
    # shared ``/tmp`` is owned by nobody and, on hosts that reap ``/tmp``
    # mid-session, can vanish under a running test. A bare ``mkdtemp()`` lands
    # in that base; the assertion pins it so a ``dir=`` creeping back in is a
    # red test rather than residue on someone's disk. Same fixture as
    # test_outbox_notify_broadcast.py.
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp())
    assert base.is_relative_to(tempfile.gettempdir()), base
    odir = base / "outbox"
    odir.mkdir()
    try:
        with patch("kiro_crew.config.loader.outbox_dir", return_value=odir):
            yield odir
    finally:
        shutil.rmtree(base, ignore_errors=True)


class TestOutboxNotifyBinary:
    @pytest.mark.asyncio
    async def test_binary_mp3_accepted(self, outbox, mock_sel):
        """Binary MP3 file passes notify validation (in allowlist)."""
        mp3 = outbox / "test.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 50)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(mp3),
                    "filename": "test.mp3",
                    "description": "test audio",
                    "size": mp3.stat().st_size,
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_binary_exe_rejected(self, outbox, mock_sel):
        """Binary EXE file rejected (not in allowlist)."""
        exe = outbox / "payload.exe"
        exe.write_bytes(b"\x4d\x5a\x90\x00\x03\x00\xff\xfe\x80\x81" * 10)  # non-UTF-8 PE header
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(exe),
                    "filename": "payload.exe",
                    "description": "bad file",
                    "size": exe.stat().st_size,
                },
            )
            assert resp.status == 400
            data = await resp.json()
            assert "not allowed" in data["error"]

    @pytest.mark.asyncio
    async def test_text_with_secrets_rejected(self, outbox, mock_sel):
        """Text file with AWS key is rejected."""
        txt = outbox / "secrets.txt"
        txt.write_text("key=AKIAIOSFODNN7EXAMPLE")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(txt),
                    "filename": "secrets.txt",
                    "description": "oops",
                    "size": txt.stat().st_size,
                },
            )
            assert resp.status == 400
            data = await resp.json()
            assert "sensitive" in data["error"]


class TestOutboxDownloadBinary:
    @pytest.mark.asyncio
    async def test_download_mp3_inline(self, outbox, mock_sel):
        """MP3 served with audio/mpeg content-type and inline disposition."""
        mp3 = outbox / "standup.mp3"
        mp3.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/standup.mp3")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "audio/mpeg"
            assert "inline" in resp.headers["Content-Disposition"]
            assert resp.headers["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.asyncio
    async def test_download_exe_rejected(self, outbox, mock_sel):
        """EXE file rejected by download handler (not in allowlist).

        macOS ships /etc/apache2/mime.types which classifies .exe as
        application/x-msdownload; Linux CI lacks that file and falls
        through to application/octet-stream. Compute the expected mime
        from the same call the handler makes so the assertion is
        platform-portable.
        """
        exe = outbox / "bad.exe"
        exe.write_bytes(b"\x4d\x5a\x90\x00\x03\x00\xff\xfe\x80\x81" * 10)  # non-UTF-8
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/bad.exe")
            assert resp.status == 403
            data = await resp.json()
            assert "not allowed" in data["error"]
        expected_mime = mimetypes.guess_type("bad.exe")[0] or "application/octet-stream"
        mock_sel.log_tool_invocation.assert_called_with(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"binary_mime_not_allowed: {expected_mime}",
        )

    @pytest.mark.asyncio
    async def test_download_text_served(self, outbox, mock_sel):
        """Clean text file served normally."""
        txt = outbox / "readme.txt"
        txt.write_text("hello world")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/readme.txt")
            assert resp.status == 200
            body = await resp.read()
            assert b"hello world" in body

    @pytest.mark.asyncio
    async def test_download_video_inline(self, outbox, mock_sel):
        """MP4 served with video/mp4 and inline disposition."""
        mp4 = outbox / "clip.mp4"
        mp4.write_bytes(b"\x00\x00\x00\x1cftyp\xff\xfe\x80\x81" + b"\x90" * 100)  # non-UTF-8
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/clip.mp4")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "video/mp4"
            assert "inline" in resp.headers["Content-Disposition"]


class TestGeneratedBinaryActuallyTrips:
    """A fixture the scanner ignores would make every case below vacuous."""

    def test_the_flagged_png_is_detected(self):
        from kiro_crew.platform import binary_content_is_flagged

        assert binary_content_is_flagged(_synth_flagged_png())

    def test_the_clean_png_is_not_detected(self):
        from kiro_crew.platform import binary_content_is_flagged

        assert not binary_content_is_flagged(_clean_png())

    def test_both_fixtures_really_are_binary(self):
        for raw in (_synth_flagged_png(), _clean_png()):
            with pytest.raises(UnicodeDecodeError):
                raw.decode("utf-8")


class TestOwnerFacingGatesScanBinaryContent:
    """An allow-listed media type carrying a credential is not waved through.

    The type allow-list answers "can the browser render these bytes safely", which
    is a different question from "do these bytes contain a secret". Both
    owner-facing HTTP gates decide the second question with the same recorded
    grant their text branch already consults, so one file cannot be refused as
    text and accepted as a PNG.
    """

    @pytest.mark.asyncio
    async def test_notify_refuses_a_flagged_media_file_without_a_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(png),
                    "filename": "shot.png",
                    "description": "screenshot",
                    "size": png.stat().st_size,
                },
            )
            assert resp.status == 400
            assert "embedded credentials" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_notify_delivers_the_card_under_the_owners_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        _grant()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(png),
                    "filename": "shot.png",
                    "description": "screenshot",
                    "size": png.stat().st_size,
                },
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

    @pytest.mark.asyncio
    async def test_download_refuses_flagged_media_without_a_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/shot.png")
            assert resp.status == 400
            assert "embedded credentials" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_download_refuses_flagged_media_for_a_non_owner_holding_a_grant(
        self, outbox, mock_sel
    ):
        """The grant releases bytes to the OWNER, not to every authenticated caller.

        Same second conjunct the text branch carries: this route needs
        authentication, which a Slack allow-listed non-owner running ``!dashboard``
        also has. The collaborator is patched rather than an identity forged onto
        the request, so what is pinned is the route's decision and not the harness.
        """
        png = outbox / "shot.png"
        png.write_bytes(_synth_flagged_png())
        _grant()
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/outbox/shot.png")
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_download_serves_flagged_media_to_the_owner_under_a_grant(self, outbox, mock_sel):
        png = outbox / "shot.png"
        raw = _synth_flagged_png()
        png.write_bytes(raw)
        _grant()
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=True,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get("/api/outbox/shot.png")
                assert resp.status == 200
                assert await resp.read() == raw

    @pytest.mark.asyncio
    async def test_a_clean_media_file_needs_no_grant_on_either_gate(self, outbox, mock_sel):
        """The scan must not turn ordinary media into a consent prompt."""
        png = outbox / "clean.png"
        png.write_bytes(_clean_png())
        async with TestClient(TestServer(_make_app())) as client:
            notify = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(png),
                    "filename": "clean.png",
                    "description": "plain",
                    "size": png.stat().st_size,
                },
            )
            assert notify.status == 200
            download = await client.get("/api/outbox/clean.png")
            assert download.status == 200

    @pytest.mark.asyncio
    async def test_a_refused_media_type_is_never_scanned(self, outbox, mock_sel):
        """Type first, content second: a type this route refuses stops at the type.

        The ordering is what keeps a 50 MB executable from being decoded and
        scanned only to be refused for its type anyway, and the audited reason
        stays the accurate one.
        """
        exe = outbox / "bad.exe"
        exe.write_bytes(_synth_flagged_png())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/bad.exe")
            assert resp.status == 403
            assert "not allowed" in (await resp.json())["error"]
        expected_mime = mimetypes.guess_type("bad.exe")[0] or "application/octet-stream"
        mock_sel.log_tool_invocation.assert_called_with(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"binary_mime_not_allowed: {expected_mime}",
        )


class TestWideEncodedCredentialInAUtf8DecodableFile:
    """A wide-encoded credential in bytes that DECODE must still be refused.

    NUL-interleaved ASCII is itself valid UTF-8, so these bytes take each gate's
    TEXT branch rather than its binary one, and ``redact`` matches contiguous
    ASCII so it matches none of the key. A gate whose wide pass sits behind a
    decode failure therefore never scans this file at all.

    A negative answer from the scan does not degrade to "owner only" on the
    download route -- it skips the owner conjunct entirely, so the bytes leave to
    any authenticated caller, and the completed-download record notes the
    handover without undoing it.

    The first test is the control that keeps the rest honest: it asserts the
    buffer really does decode as UTF-8 and that the text pass really does miss
    the key, so a gate doing only the narrow pass fails the others.
    """

    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    def test_the_container_decodes_as_utf8_and_the_text_pass_misses_it(self, encoding):
        from kiro_crew import security

        raw = _synth_wide_utf8_pdf(encoding)
        text = raw.decode("utf-8")
        assert security.redact(text) == text

    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    def test_the_shared_wide_pass_answers_on_it(self, encoding):
        from kiro_crew.platform import wide_content_is_flagged

        assert wide_content_is_flagged(_synth_wide_utf8_pdf(encoding))

    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    def test_innocent_wide_text_in_a_utf8_container_is_not_flagged(self, encoding):
        """The pass must discriminate on content, not on wide text existing."""
        from kiro_crew.platform import wide_content_is_flagged

        innocent = "the quick brown fox jumps over the lazy dog\n" * 3
        raw = b"%PDF-1.7\n" + innocent.encode(encoding) + b"\n%%EOF\n"
        assert raw.decode("utf-8")
        assert not wide_content_is_flagged(raw)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    async def test_notify_refuses_it_without_a_grant(self, outbox, mock_sel, encoding):
        pdf = outbox / "report.pdf"
        pdf.write_bytes(_synth_wide_utf8_pdf(encoding))
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(pdf),
                    "filename": "report.pdf",
                    "description": "report",
                    "size": pdf.stat().st_size,
                },
            )
            assert resp.status == 400
            assert "sensitive" in (await resp.json())["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    async def test_download_refuses_it_without_a_grant(self, outbox, mock_sel, encoding):
        pdf = outbox / "report.pdf"
        pdf.write_bytes(_synth_wide_utf8_pdf(encoding))
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/report.pdf")
            assert resp.status == 400


#: Byte blocks of the standard baseline JPEG Huffman symbol table, each a run of
#: consecutive values, written back to back exactly as the default tables carry
#: them. Assembled from its structure rather than pasted, so the test states what
#: makes these bytes a table instead of asserting against an opaque literal.
_JPEG_HUFFMAN_BLOCKS: tuple[tuple[int, int], ...] = (
    (0x25, 0x2A),
    (0x34, 0x3A),
    (0x43, 0x4A),
    (0x53, 0x5A),
    (0x63, 0x6A),
    (0x73, 0x7A),
)


def _enumerated_table() -> bytes:
    """The container symbol table, as successive runs of consecutive values."""
    return b"".join(bytes(range(low, high + 1)) for low, high in _JPEG_HUFFMAN_BLOCKS)


def _clean_jpeg() -> bytes:
    """JPEG-shaped bytes carrying the standard table and nothing sensitive.

    ``\\xff\\xd8\\xff\\xe0`` is the SOI/APP0 head a JPEG opens with and puts the
    guessed MIME type inside the allow-list; ``\\xff\\xdb`` opens the table
    segment. The high bytes at the end are what makes the UTF-8 decode raise, so
    these bytes take each gate's binary branch.
    """
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\xff\xdb\x00C" + _enumerated_table() + b"\x80\x81\xff\xd9"
    )


def _telegram_shaped_token(bot_id: str) -> str:
    """A bot-token-SHAPED value, assembled at runtime.

    Same convention as :func:`_synth_flagged_png`: the scanner sees identical
    bytes either way, and a diff carrying a literal token reads as key material
    to a review provider. The body is deterministic base64 over a digest, so it
    is token-SHAPED without being a token.
    """
    body = base64.urlsafe_b64encode(hashlib.sha256(bot_id.encode()).digest()).decode().rstrip("=")
    return f"{bot_id}:{body[:35]}"


class TestContainerSymbolTablesAreNotCredentials:
    """A container's own symbol table must not spend the owner's delivery grant.

    A binary container writes its fixed tables as successive blocks of
    consecutive byte values. Read as ``latin-1`` such a block is printable ASCII
    in strict ascending order, and the standard baseline Huffman table spells six
    digits, a colon and thirty-two letters -- the shape of an unlabelled bot
    token. The detectors' entropy floor scores the character multiset, and an
    ascending block has maximal symbol diversity, so nothing in the catalogue
    measures the one property that gives a table away.

    The cost is not the refusal. The three owner-facing gates read one durable
    grant, so the owner's only way past a refused photo is to record a grant that
    is class-wide: once recorded it disarms the refusal for every content kind on
    all three gates, and a genuine key afterwards leaves with an audit entry and
    no refusal. Noise on this path spends the control, which is why a table has to
    be invisible to the scan rather than merely rare.

    The first test is the control that keeps the rest honest: it asserts the table
    really does satisfy a credential shape on an unmasked scan, so a scanner that
    dropped the mask fails the tests below rather than passing them trivially.
    """

    def test_the_table_really_does_satisfy_a_credential_shape(self):
        from kiro_crew import security

        table = _enumerated_table().decode("latin-1")
        assert security.redact(table) != table

    def test_a_container_carrying_only_its_table_is_not_flagged(self):
        from kiro_crew.platform import binary_content_is_flagged

        raw = _clean_jpeg()
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
        assert not binary_content_is_flagged(raw)

    @pytest.mark.parametrize(
        "secret",
        [
            _telegram_shaped_token("110201543"),
            "AKIA" + "IOSFODNN7EXAMPLE",
            "gh" + "p_" + hashlib.sha256(b"kc-gate").hexdigest()[:36],
        ],
        ids=["bot-token", "access-key-id", "forge-token"],
    )
    def test_a_credential_inside_the_same_container_is_still_flagged(self, secret):
        from kiro_crew.platform import binary_content_is_flagged

        assert binary_content_is_flagged(_clean_jpeg() + secret.encode())

    def test_a_key_inside_the_same_container_is_still_flagged(self):
        from kiro_crew.platform import binary_content_is_flagged

        assert binary_content_is_flagged(_clean_jpeg() + _pem_text().encode())

    def test_tables_on_both_sides_of_a_credential_do_not_hide_it(self):
        """A credential's own characters do not ascend, so a table cannot reach it.

        The masked region ends where the ascending runs end. Padding is therefore
        not a bypass: it is the one case an attacker would reach for, and the
        credential between the two tables keeps its match.
        """
        from kiro_crew.platform import binary_content_is_flagged

        table = _enumerated_table()
        secret = _telegram_shaped_token("110201543").encode()
        assert binary_content_is_flagged(b"\xff\xd8\xff\xe0" + table + secret + table + b"\x80")

    def test_one_long_ordered_fragment_keeps_its_whole_match(self):
        """An ordered span inside an identifier is not a table, so nothing is masked.

        A bot id can be ascending on its own. One run is not an enumerated table,
        and the value must stay visible to the detectors.
        """
        from kiro_crew.platform import binary_content_is_flagged

        secret = _telegram_shaped_token("123456789")
        assert secret.startswith("123456789:")
        assert binary_content_is_flagged(b"\xff\xd8\xff\xe0\x80\x81" + secret.encode())

    def test_short_ascending_pairs_inside_a_value_are_not_a_table(self):
        """The run floor is what stops character coincidence from masking a value.

        Adjacent ascending PAIRS are ordinary coincidence in any long value, so a
        floor low enough to admit them turns a credential body into one masked
        region and loses the match. The body below is pairs end to end, which is
        the worst case that coincidence can reach, and the first assertion is what
        makes it that case rather than an opaque string.
        """
        from kiro_crew.platform import binary_content_is_flagged
        from kiro_crew.security.redaction import _ascending_runs

        paired = "abdeghjkmnpqstvwyzABDEGHJKMNPQ"
        assert {end - start for start, end in _ascending_runs(paired)} == {2}
        secret = f"110201543:{paired}"
        assert binary_content_is_flagged(b"\xff\xd8\xff\xe0\x80\x81" + secret.encode())

    def test_masking_leaves_every_byte_outside_a_table_alone(self):
        from kiro_crew.security.redaction import mask_enumerated_byte_tables

        table = _enumerated_table().decode("latin-1")
        head, tail = "the quick brown fox", "jumps over the lazy dog"
        masked = mask_enumerated_byte_tables(head + table + tail)
        assert masked.startswith(head)
        assert masked.endswith(tail)
        assert masked[len(head) : len(masked) - len(tail)] == "\x00" * len(table)

    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    def test_the_wide_pass_masks_the_table_too(self, encoding):
        """A table written at wide spacing lifts back to the same ascending run."""
        from kiro_crew.platform import wide_content_is_flagged

        table = _enumerated_table().decode("latin-1")
        assert not wide_content_is_flagged(b"%PDF-1.7\n" + table.encode(encoding) + b"\n%%EOF\n")

    @pytest.mark.parametrize("encoding", _WIDE_ENCODINGS)
    def test_the_wide_pass_still_answers_on_a_key_beside_a_table(self, encoding):
        from kiro_crew.platform import wide_content_is_flagged

        payload = _enumerated_table().decode("latin-1") + _pem_text()
        assert wide_content_is_flagged(b"%PDF-1.7\n" + payload.encode(encoding) + b"\n%%EOF\n")

    @pytest.mark.asyncio
    async def test_notify_delivers_a_container_carrying_only_its_table(self, outbox, mock_sel):
        """The owner is never sent to the grant panel for a clean container."""
        jpeg = outbox / "photo.jpg"
        jpeg.write_bytes(_clean_jpeg())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={
                    "path": str(jpeg),
                    "filename": "photo.jpg",
                    "description": "photo",
                    "size": jpeg.stat().st_size,
                },
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_download_serves_a_container_carrying_only_its_table(self, outbox, mock_sel):
        jpeg = outbox / "photo.jpg"
        jpeg.write_bytes(_clean_jpeg())
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/outbox/photo.jpg")
            assert resp.status == 200
