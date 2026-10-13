import pytest

from jev_mail_triage.config import BridgeConfig
from jev_mail_triage.errors import TriageError
from jev_mail_triage.imap_client import BridgeImap


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        self.calls.append((command, args))
        if command == "FETCH":
            return "OK", [(b"1 (UID 1 RFC822.SIZE 5)", b"hello")]
        if command == "COPY":
            return "OK", [b"[COPYUID 4 1 7]"]
        return "OK", []


def _client() -> tuple[BridgeImap, _Connection]:
    client = BridgeImap(
        BridgeConfig(
            account_id="test",
            host="127.0.0.1",
            port=1143,
            security_mode="starttls",
            username="synthetic",
            mailbox="INBOX",
            tls_ca_file=None,
        ),
        "not-a-real-password",
    )
    connection = _Connection()
    client._connection = connection
    client._selected_mailbox = "INBOX"
    return client, connection


def test_fetch_uses_body_peek_and_does_not_change_message_flags() -> None:
    client, connection = _client()
    fetched = client.fetch_uid(1)
    assert fetched is not None and fetched.raw == b"hello"
    assert "BODY.PEEK[]" in str(connection.calls[0][1])


def test_candidate_headers_fetch_only_bounded_headers_and_decode_mime() -> None:
    client, connection = _client()
    headers = client.fetch_headers(1)
    assert headers is not None
    assert connection.calls == [
        ("FETCH", ("1", "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)]<0.16384>)"))
    ]


def test_candidate_headers_decode_unicode_and_reject_mismatched_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, connection = _client()
    raw = b"Subject: =?utf-8?q?Kvittering_p=C3=A5_norsk?=\r\nFrom: sender@example.test\r\n\r\n"
    monkeypatch.setattr(connection, "uid", lambda *_args: ("OK", [(b"1 (UID 1)", raw)]))
    headers = client.fetch_headers(1)
    assert headers is not None and headers["subject"] == "Kvittering på norsk"
    monkeypatch.setattr(connection, "uid", lambda *_args: ("OK", [(b"1 (UID 2)", raw)]))
    with pytest.raises(TriageError, match="mismatched"):
        client.fetch_headers(1)


def test_candidate_headers_missing_failure_and_readonly_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, connection = _client()
    monkeypatch.setattr(connection, "uid", lambda *_args: ("OK", [None]))
    assert client.fetch_headers(1) is None
    monkeypatch.setattr(connection, "uid", lambda *_args: ("NO", []))
    with pytest.raises(TriageError, match="FETCH failed"):
        client.fetch_headers(1)
    client._readonly = False
    with pytest.raises(TriageError, match="read-only"):
        client.fetch_headers(1)


def test_copy_fallback_requires_readwrite_and_uidplus() -> None:
    client, connection = _client()
    client._capabilities = frozenset({"UIDPLUS"})
    client._readonly = False
    assert client.copy_uid(1, "Jev Review") == 7
    assert connection.calls[-1][0] == "COPY"

    client._capabilities = frozenset()
    with pytest.raises(TriageError):
        client.copy_uid(1, "Jev Review")


def test_source_cleanup_uses_uid_scoped_expunge_only() -> None:
    client, connection = _client()
    client._capabilities = frozenset({"UIDPLUS"})
    client._readonly = False
    client.delete_uid_and_expunge(1)
    assert connection.calls == [
        ("STORE", ("1", "+FLAGS.SILENT", r"(\Deleted)")),
        ("EXPUNGE", ("1",)),
    ]


def test_moving_in_readonly_mode_is_rejected() -> None:
    client, _ = _client()
    client._capabilities = frozenset({"MOVE"})
    with pytest.raises(TriageError):
        client.move_uid(1, "Jev Review")
