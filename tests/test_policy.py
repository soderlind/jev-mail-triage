from dataclasses import replace
from pathlib import Path

from jev_mail_triage.config import PolicyConfig
from jev_mail_triage.message import prepare_message
from jev_mail_triage.models import Assessment, Outcome
from jev_mail_triage.policy import decide


def _policy(**changes: object) -> PolicyConfig:
    return replace(
        PolicyConfig(
            move_enabled=False,
            spam_threshold=0.98,
            scam_threshold=0.95,
            max_input_bytes=4096,
            review_folder="Jev Review",
            trusted_authserv_id=None,
            allowlist=(),
            model="jev-latest",
        ),
        **changes,
    )


def test_threshold_boundaries_are_inclusive() -> None:
    prepared = prepare_message(
        (Path(__file__).parent / "fixtures/spam.eml").read_bytes(),
        max_input_bytes=4096,
    )
    result = decide(prepared, Assessment(0.98, 0.0, "model", "policy"), _policy())
    assert result.outcome == Outcome.MOVE
    result = decide(prepared, Assessment(0.0, 0.95, "model", "policy"), _policy())
    assert result.outcome == Outcome.MOVE


def test_below_threshold_and_truncated_inputs_are_left_untouched() -> None:
    prepared = prepare_message(
        (Path(__file__).parent / "fixtures/clean-link.eml").read_bytes(),
        max_input_bytes=4096,
    )
    assessment = Assessment(0.9799, 0.9499, "model", "policy")
    assert decide(prepared, assessment, _policy()).outcome == Outcome.LEAVE
    assert (
        decide(replace(prepared, truncated=True), assessment, _policy()).reason == "truncated_input"
    )


def test_genuine_message_with_a_link_remains_unmoved_below_threshold() -> None:
    prepared = prepare_message(
        (Path(__file__).parent / "fixtures/clean-link.eml").read_bytes(),
        max_input_bytes=4096,
    )
    decision = decide(prepared, Assessment(0.01, 0.02, "model", "policy"), _policy())
    assert decision.outcome == Outcome.LEAVE


def test_allowlist_requires_trusted_dmarc_pass() -> None:
    prepared = prepare_message(
        b"From: person@sub.example.test\r\n"
        b"Authentication-Results: mx.example.test; dmarc=pass\r\n"
        b"\r\nHello",
        max_input_bytes=4096,
        trusted_authserv_id="mx.example.test",
    )
    policy = _policy(allowlist=("example.test",))
    assert decide(prepared, Assessment(0.99, 0.99, "model", "policy"), policy).reason == (
        "authenticated_allowlist"
    )
    untrusted = replace(prepared, trusted_dmarc_pass=False)
    assert (
        decide(untrusted, Assessment(0.99, 0.0, "model", "policy"), policy).outcome == Outcome.MOVE
    )
