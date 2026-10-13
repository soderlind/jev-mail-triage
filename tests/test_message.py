from pathlib import Path

import pytest

from jev_mail_triage.errors import PermanentMessageError
from jev_mail_triage.message import prepare_message

FIXTURES = Path(__file__).parent / "fixtures"


def test_plain_text_and_message_id_are_prepared() -> None:
    prepared = prepare_message((FIXTURES / "clean-link.eml").read_bytes(), max_input_bytes=4096)
    assert prepared.message_id == "<meeting-1@example.test>"
    assert "alex@example.test" in prepared.prompt_text
    assert "https://example.test/notes?id=42" in prepared.prompt_text
    assert not prepared.truncated


def test_html_fallback_extracts_destinations_without_script_content() -> None:
    prepared = prepare_message((FIXTURES / "phishing.eml").read_bytes(), max_input_bytes=4096)
    assert "https://login-lookalike.example.test/verify" in prepared.prompt_text
    assert "https://remote.example.test/" not in prepared.prompt_text
    assert "document.location" not in prepared.prompt_text


def test_plain_text_is_preferred_and_attached_messages_are_excluded() -> None:
    raw = (
        b"From: sender@example.test\r\n"
        b"Subject: Multipart\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: multipart/alternative; boundary=y\r\n\r\n"
        b"--y\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nplain body\r\n"
        b"--y\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<b>HTML fallback</b>\r\n"
        b"--y--\r\n"
        b"--x\r\nContent-Type: message/rfc822\r\n"
        b"Content-Disposition: attachment\r\n\r\n"
        b"From: secret@example.test\r\n\r\nattached body\r\n--x--\r\n"
    )
    prepared = prepare_message(raw, max_input_bytes=4096)
    assert "plain body" in prepared.prompt_text
    assert "HTML fallback" not in prepared.prompt_text
    assert "attached body" not in prepared.prompt_text
    assert "secret@example.test" not in prepared.prompt_text


def test_fingerprint_covers_excluded_attachment_bytes() -> None:
    prefix = (
        b"From: sender@example.test\r\n"
        b"Subject: Same visible content\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: text/plain\r\n\r\nsame body\r\n"
        b"--x\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=data.bin\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
    )
    suffix = b"\r\n--x--\r\n"
    first = prepare_message(prefix + b"YWJj" + suffix, max_input_bytes=4096)
    second = prepare_message(prefix + b"ZGVm" + suffix, max_input_bytes=4096)
    assert first.prompt_text == second.prompt_text
    assert first.fingerprint != second.fingerprint


@pytest.mark.parametrize(
    "filename, phrase",
    [
        ("newsletter.eml", "subscribed"),
        ("norwegian-receipt.eml", "Takk for bestillingen"),
    ],
)
def test_legitimate_fixture_text_is_preserved(filename: str, phrase: str) -> None:
    prepared = prepare_message((FIXTURES / filename).read_bytes(), max_input_bytes=4096)
    assert phrase in prepared.prompt_text


def test_input_cap_marks_prepared_message_as_truncated() -> None:
    raw = b"From: a@example.test\r\nSubject: " + b"x" * 1000 + b"\r\n\r\nbody"
    prepared = prepare_message(raw, max_input_bytes=256)
    assert prepared.truncated
    assert prepared.input_bytes <= 256


def test_invalid_input_limit_is_rejected_at_truncation_boundary() -> None:
    raw = b"From: a@example.test\r\n\r\nbody"
    with pytest.raises(ValueError):
        prepare_message(raw, max_input_bytes=0)


def test_malformed_mime_is_reported_as_permanent_message_error() -> None:
    malformed = (
        b"Content-Type: text/plain; charset=x-no-such-charset\r\n\r\n"
        b"not decodable with the declared charset"
    )
    with pytest.raises(PermanentMessageError):
        prepare_message(malformed, max_input_bytes=4096)
