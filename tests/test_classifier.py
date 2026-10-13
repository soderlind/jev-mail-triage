from pathlib import Path

import pytest
from typesafe_sdk import TypeSafeAPIConnectionError

from jev_mail_triage.classifier import JevAssessor
from jev_mail_triage.errors import RetryableError
from jev_mail_triage.message import prepare_message


class _Answer:
    def __init__(self, score: object) -> None:
        self.noul = score


class _Response:
    def __init__(self, spam: object, scam: object) -> None:
        self.answers = {"spam": _Answer(spam), "scam_phishing": _Answer(scam)}


class _Client:
    def __init__(self, response: object) -> None:
        self.response = response
        self.request: dict[str, object] | None = None

    def system_one(self, **kwargs: object) -> object:
        self.request = kwargs
        return self.response


@pytest.mark.parametrize(
    "filename, phrase",
    [
        ("norwegian-receipt.eml", "Takk for bestillingen"),
        ("clean-link.eml", "Meeting notes"),
    ],
)
def test_two_noul_questions_are_sent_together_for_bilingual_email(
    filename: str, phrase: str
) -> None:
    prepared = prepare_message(
        (Path(__file__).parent / "fixtures" / filename).read_bytes(), max_input_bytes=4096
    )
    client = _Client(_Response(0.02, 0.01))
    assessment = JevAssessor(client, model="jev-latest").assess(prepared)
    assert assessment.spam_score == 0.02
    request = client.request
    assert request is not None
    assert set(request["questions"]) == {"spam", "scam_phishing"}
    assert phrase in request["state"]["email"]
    assert "Norwegian or English" in request["state"]["language"]


@pytest.mark.parametrize(
    "response",
    [
        object(),
        type("Bad", (), {"answers": []})(),
        _Response(True, 0.2),
        _Response(1.1, 0.2),
        _Response(0.2, "0.3"),
    ],
)
def test_malformed_api_responses_fail_closed(response: object) -> None:
    prepared = prepare_message(b"From: a@example.test\r\n\r\nbody", max_input_bytes=4096)
    with pytest.raises(RetryableError):
        JevAssessor(_Client(response), model="jev-latest").assess(prepared)


class _FailingClient:
    def system_one(self, **kwargs: object) -> object:
        raise TypeSafeAPIConnectionError("synthetic transport failure")


def test_typesafe_transport_failures_are_retryable() -> None:
    prepared = prepare_message(b"From: a@example.test\r\n\r\nbody", max_input_bytes=4096)
    with pytest.raises(RetryableError):
        JevAssessor(_FailingClient(), model="jev-latest").assess(prepared)
