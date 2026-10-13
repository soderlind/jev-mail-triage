from __future__ import annotations

import imaplib
import re
import ssl
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser
from typing import Self

import truststore

from .config import BridgeConfig
from .errors import ConfigurationError, RetryableError, TriageError
from .message import prepare_message

_MAX_RAW_FETCH_BYTES = 2 * 1024 * 1024
_LITERAL_RE = re.compile(rb"RFC822\.SIZE\s+(\d+)", re.IGNORECASE)
_COPYUID_RE = re.compile(rb"COPYUID\s+\d+\s+[\d:,]+\s+(\d+)", re.IGNORECASE)
_LIST_NAME_RE = re.compile(rb'("(?:\\.|[^"])*"|[^ ]+)\s*$')


@dataclass(frozen=True)
class FetchedMessage:
    raw: bytes
    truncated: bool


def _imap_quote(value: str) -> str:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ConfigurationError("IMAP mailbox names cannot contain control characters.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_mailbox_name(line: bytes) -> str | None:
    match = _LIST_NAME_RE.search(line)
    if match is None:
        return None
    token = match.group(1)
    if token.startswith(b'"') and token.endswith(b'"'):
        token = token[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    try:
        return token.decode("utf-8")
    except UnicodeDecodeError:
        return token.decode("ascii", errors="replace")


class BridgeImap:
    def __init__(self, config: BridgeConfig, password: str) -> None:
        self.config = config
        self._password = password
        self._connection: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None
        self._capabilities: frozenset[str] = frozenset()
        self._selected_mailbox: str | None = None
        self._readonly = True

    def __enter__(self) -> Self:
        try:
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            if self.config.tls_ca_file is not None:
                if not self.config.tls_ca_file.is_file():
                    raise ConfigurationError(
                        f"Configured Bridge CA file does not exist: {self.config.tls_ca_file}"
                    )
                try:
                    context.load_verify_locations(cafile=str(self.config.tls_ca_file))
                except (OSError, ssl.SSLError) as exc:
                    raise ConfigurationError(
                        "Could not load the configured Bridge CA file."
                    ) from exc
            if self.config.security_mode == "implicit_tls":
                connection: imaplib.IMAP4 | imaplib.IMAP4_SSL = imaplib.IMAP4_SSL(
                    self.config.host,
                    self.config.port,
                    ssl_context=context,
                    timeout=30,
                )
                self._connection = connection
            else:
                connection = imaplib.IMAP4(self.config.host, self.config.port, timeout=30)
                self._connection = connection
                connection.starttls(ssl_context=context)
            status, capability_data = connection.capability()
            if status != "OK":
                raise RetryableError("Bridge did not return IMAP capabilities.")
            capability_text = b" ".join(part for part in capability_data if isinstance(part, bytes))
            self._capabilities = frozenset(
                word.decode("ascii", errors="ignore").upper() for word in capability_text.split()
            )
            status, _ = connection.login(self.config.username, self._password)
            if status != "OK":
                raise RetryableError("Bridge IMAP authentication failed.")
            return self
        except TriageError:
            self.__exit__(None, None, None)
            raise
        except ssl.SSLCertVerificationError as exc:
            self.__exit__(None, None, None)
            raise ConfigurationError(
                "Bridge TLS certificate verification failed; check trust or tls_ca_file."
            ) from exc
        except (imaplib.IMAP4.error, OSError, ssl.SSLError, TimeoutError) as exc:
            self.__exit__(None, None, None)
            raise RetryableError(
                f"Could not connect to Proton Mail Bridge ({type(exc).__name__})."
            ) from exc

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._connection is not None:
            try:
                self._connection.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
            self._connection = None

    @property
    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def _conn(self) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
        if self._connection is None:
            raise TriageError("Bridge IMAP session is not connected.")
        return self._connection

    def list_mailboxes(self) -> list[str]:
        status, data = self._conn().list()
        if status != "OK":
            raise RetryableError("Could not list Bridge mailboxes.")
        names = [_parse_mailbox_name(line) for line in data if isinstance(line, bytes)]
        return [name for name in names if name is not None]

    def resolve_mailbox(self, configured_name: str) -> str:
        names = self.list_mailboxes()
        if configured_name.casefold() == "inbox":
            match = next((name for name in names if name.casefold() == "inbox"), None)
        else:
            match = next((name for name in names if name == configured_name), None)
        if match is None:
            raise ConfigurationError(
                f"Configured mailbox {configured_name!r} was not listed by Bridge."
            )
        return match

    def select(self, mailbox: str, *, readonly: bool = True) -> tuple[int, int]:
        status, _ = self._conn().select(_imap_quote(mailbox), readonly=readonly)
        if status != "OK":
            raise RetryableError(f"Could not select configured mailbox {mailbox!r}.")
        validity = self._response_integer("UIDVALIDITY")
        uid_next = self._response_integer("UIDNEXT")
        self._selected_mailbox = mailbox
        self._readonly = readonly
        return validity, uid_next

    def _response_integer(self, name: str) -> int:
        _, data = self._conn().response(name)
        if not data or data[0] is None:
            raise RetryableError(f"Bridge did not provide {name}.")
        try:
            return int(data[0])
        except (TypeError, ValueError) as exc:
            raise RetryableError(f"Bridge returned invalid {name}.") from exc

    def search_uids(self, start_uid: int | None = None) -> list[int]:
        if self._selected_mailbox is None:
            raise TriageError("Select a mailbox before searching.")
        criteria = ("ALL",) if start_uid is None else ("UID", f"{start_uid}:*")
        status, data = self._conn().uid("SEARCH", None, *criteria)
        if status != "OK":
            raise RetryableError("Bridge UID SEARCH failed.")
        if not data or not data[0]:
            return []
        try:
            return [int(part) for part in data[0].split()]
        except ValueError as exc:
            raise RetryableError("Bridge returned an invalid UID search result.") from exc

    def fetch_headers(self, uid: int) -> dict[str, str | bool] | None:
        if self._selected_mailbox is None or not self._readonly:
            raise TriageError("Select a mailbox read-only before fetching candidate headers.")
        limit = 16384
        try:
            status, data = self._conn().uid(
                "FETCH",
                str(uid),
                f"(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)]<0.{limit}>)",
            )
        except (imaplib.IMAP4.error, OSError) as exc:
            raise RetryableError("Bridge candidate header FETCH connection failed.") from exc
        if status != "OK":
            raise RetryableError("Bridge candidate header FETCH failed.")
        for entry in data:
            if not isinstance(entry, tuple) or len(entry) != 2:
                continue
            metadata, raw = entry
            if not isinstance(metadata, bytes) or not isinstance(raw, bytes):
                raise RetryableError("Bridge returned invalid candidate headers.")
            match = re.search(rb"\bUID\s+(\d+)\b", metadata)
            if match is None or int(match.group(1)) != uid:
                raise RetryableError("Bridge returned mismatched candidate header UID.")
            parsed = BytesHeaderParser(policy=policy.default).parsebytes(raw[:limit])
            return {
                "subject": str(parsed.get("Subject", "")),
                "sender": str(parsed.get("From", "")),
                "date": str(parsed.get("Date", "")),
                "message_id": str(parsed.get("Message-ID", "")),
                "truncated": len(raw) >= limit,
            }
        return None

    def fetch_uid(self, uid: int) -> FetchedMessage | None:
        if self._selected_mailbox is None:
            raise TriageError("Select a mailbox before fetching.")
        status, data = self._conn().uid(
            "FETCH",
            str(uid),
            f"(UID RFC822.SIZE BODY.PEEK[]<0.{_MAX_RAW_FETCH_BYTES}>)",
        )
        if status != "OK":
            raise RetryableError("Bridge UID FETCH failed.")
        response: bytes | None = None
        size: int | None = None
        for entry in data:
            if isinstance(entry, tuple) and len(entry) == 2:
                metadata, literal = entry
                if isinstance(metadata, bytes):
                    size_match = _LITERAL_RE.search(metadata)
                    if size_match:
                        size = int(size_match.group(1))
                if isinstance(literal, bytes):
                    response = literal
        if response is None:
            return None
        return FetchedMessage(
            raw=response,
            truncated=(
                size > len(response) if size is not None else len(response) >= _MAX_RAW_FETCH_BYTES
            ),
        )

    def ensure_review_folder(self, folder: str) -> None:
        names = self.list_mailboxes()
        if folder in names:
            return
        if self._conn().create(_imap_quote(folder))[0] != "OK":
            raise TriageError(f"Could not create review folder {folder!r}; no messages were moved.")
        if folder not in self.list_mailboxes():
            raise TriageError(
                f"Review folder {folder!r} was not visible after creation; no messages were moved."
            )

    def move_uid(self, uid: int, target_folder: str) -> int | None:
        if self._readonly:
            raise TriageError("Mailbox must be selected read-write before moving messages.")
        if "MOVE" not in self._capabilities:
            raise TriageError("Bridge lacks UID MOVE; use the verified UIDPLUS fallback.")
        status, _ = self._conn().uid("MOVE", str(uid), _imap_quote(target_folder))
        if status != "OK":
            raise RetryableError("UID MOVE failed.")
        return None

    def copy_uid(self, uid: int, target_folder: str) -> int:
        if self._readonly:
            raise TriageError("Mailbox must be selected read-write before copying messages.")
        if "UIDPLUS" not in self._capabilities:
            raise TriageError("Bridge lacks UIDPLUS; refusing unsafe movement.")
        status, data = self._conn().uid("COPY", str(uid), _imap_quote(target_folder))
        if status != "OK":
            raise RetryableError("UID COPY fallback failed.")
        destination_uid = self._copyuid_destination(data)
        if destination_uid is None:
            raise RetryableError("Bridge UIDPLUS COPY returned no COPYUID mapping.")
        return destination_uid

    def delete_uid_and_expunge(self, uid: int) -> None:
        if self._readonly:
            raise TriageError("Mailbox must be selected read-write before deleting a source copy.")
        if "UIDPLUS" not in self._capabilities:
            raise TriageError("Bridge lacks UIDPLUS; refusing unsafe EXPUNGE.")
        status, _ = self._conn().uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
        if status != "OK":
            raise RetryableError("UID STORE failed for the source UID.")
        status, _ = self._conn().uid("EXPUNGE", str(uid))
        if status != "OK":
            raise RetryableError("UID EXPUNGE failed for the source UID.")

    def new_matching_messages(
        self,
        folder: str,
        baseline: str,
        expected_fingerprint: str,
        *,
        max_input_bytes: int,
    ) -> list[tuple[int, FetchedMessage]]:
        try:
            baseline_validity_text, baseline_uidnext_text = baseline.split(":", 1)
            baseline_validity = int(baseline_validity_text)
            baseline_uidnext = int(baseline_uidnext_text)
        except (ValueError, AttributeError) as exc:
            raise TriageError("Move recovery has no valid destination baseline.") from exc
        validity, uid_next = self.select(folder, readonly=True)
        if validity != baseline_validity:
            raise TriageError("Review-folder UIDVALIDITY changed during move recovery.")
        if uid_next < baseline_uidnext:
            return []
        uids = self.search_uids(baseline_uidnext)
        matching: list[tuple[int, FetchedMessage]] = []
        for uid in uids:
            fetched = self.fetch_uid(uid)
            if fetched is None:
                continue
            prepared = prepare_message(
                fetched.raw,
                max_input_bytes=max_input_bytes,
                raw_truncated=fetched.truncated,
            )
            if prepared.fingerprint == expected_fingerprint:
                matching.append((uid, fetched))
        return matching

    @staticmethod
    def _copyuid_destination(data: list[bytes | tuple[bytes, bytes] | None]) -> int | None:
        for entry in data:
            metadata = entry[0] if isinstance(entry, tuple) else entry
            if isinstance(metadata, bytes):
                match = _COPYUID_RE.search(metadata)
                if match:
                    try:
                        return int(match.group(1))
                    except ValueError:
                        return None
        return None
