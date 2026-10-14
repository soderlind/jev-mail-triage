import random
from dataclasses import replace
from pathlib import Path

import pytest

from jev_mail_triage import features
from jev_mail_triage.config import GateConfig, PolicyConfig
from jev_mail_triage.gate import LocalGate, address_hash, bayes_score, verdict_assessment
from jev_mail_triage.models import LocalLabel, LocalVerdict, PreparedMessage
from jev_mail_triage.state import StateStore

SPAM_WORDS = [
    "winner",
    "prize",
    "claim",
    "reward",
    "bitcoin",
    "casino",
    "bonus",
    "urgent",
    "offer",
    "free",
    "cash",
    "lottery",
]
HAM_WORDS = [
    "meeting",
    "agenda",
    "minutes",
    "project",
    "budget",
    "review",
    "draft",
    "thanks",
    "lunch",
    "report",
    "plan",
]
FILLER = [
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "your",
    "have",
    "will",
    "about",
    "into",
    "over",
]

POLICY = PolicyConfig(
    move_enabled=False,
    spam_threshold=0.98,
    scam_threshold=0.95,
    max_input_bytes=4096,
    review_folder="Jev Review",
    trusted_authserv_id="mx.example.test",
    allowlist=("friend@trusted.example.test",),
    model="jev-latest",
)


def _message(
    body: str,
    *,
    sender: str = "someone@example.test",
    subject: str = "Hello",
    links: tuple[str, ...] = (),
    dmarc_pass: bool = False,
    key: str | None = None,
) -> PreparedMessage:
    digest = key or str(hash((body, sender, subject, links)))
    return PreparedMessage(
        message_id=None,
        sender=sender,
        subject=subject,
        prompt_text=body,
        fingerprint=digest,
        truncated=False,
        input_bytes=len(body),
        trusted_dmarc_pass=dmarc_pass,
        sender_address=sender,
        body=body,
        links=links,
        raw_digest=digest,
    )


def _random_text(rng: random.Random, words: list[str], length: int = 40) -> str:
    return " ".join(rng.choice(words + FILLER) for _ in range(length))


def _gate(tmp_path: Path, **overrides: object) -> tuple[LocalGate, StateStore]:
    store = StateStore(tmp_path / "state.sqlite3")
    config = replace(GateConfig(mode="shadow", min_examples=5), **overrides)
    return LocalGate(config, POLICY, store), store


def _train(gate: LocalGate, rng: random.Random, count: int = 20) -> None:
    for index in range(count):
        gate.learn(
            _message(
                _random_text(rng, SPAM_WORDS),
                sender=f"promo{index}@spam.example.test",
                links=("https://cheap-pills.example.test/x",),
            ),
            LocalLabel.SPAM,
            weight=1.0,
            source="folder",
        )
        gate.learn(
            _message(
                _random_text(rng, HAM_WORDS),
                sender="colleague@work.example.test",
                links=("https://intranet.work.example.test/doc",),
            ),
            LocalLabel.CLEAN,
            weight=1.0,
            source="folder",
        )


def test_tokens_are_deterministic_and_include_domains() -> None:
    message = _message(
        "Quarterly report attached", links=("https://docs.example.test/a",), sender="a@b.test"
    )
    assert features.tokens(message) == features.tokens(message)
    assert features.link_token("example.test") in features.tokens(message)
    assert features.link_domain("https://a.b.docs.example.test/path") == "example.test"
    assert features.sender_domain(message) == "b.test"


def test_minhash_detects_near_duplicates_but_not_unrelated_text() -> None:
    rng = random.Random(1)
    base = _random_text(rng, SPAM_WORDS, 80)
    variant = base.replace("winner", "winner!", 1) + " order 12345"
    unrelated = _random_text(rng, HAM_WORDS, 80)
    first = features.signature(_message(base))
    assert first is not None
    assert features.similarity(first, features.signature(_message(variant))) > 0.85
    assert features.similarity(first, features.signature(_message(unrelated))) < 0.5
    assert features.signature(_message("too short")) is None


def test_bayes_score_is_neutral_without_clues_and_follows_evidence() -> None:
    assert bayes_score([]) == 0.5
    assert bayes_score([0.99] * 20) > 0.99
    assert bayes_score([0.01] * 20) < 0.01


def test_verdict_assessment_maps_to_extreme_scores() -> None:
    spam = verdict_assessment(LocalVerdict(LocalLabel.SPAM, "language_not_allowed"))
    clean = verdict_assessment(LocalVerdict(LocalLabel.CLEAN, "known_correspondent"))
    assert (spam.spam_score, spam.scam_score, spam.reason) == (1.0, 0.0, "language_not_allowed")
    assert (clean.spam_score, clean.model_version) == (0.0, "local-gate")


def test_authenticated_allowlist_and_correspondents_are_clean(tmp_path: Path) -> None:
    gate, store = _gate(tmp_path)
    allowlisted = _message("x", sender="friend@trusted.example.test", dmarc_pass=True)
    assert gate.evaluate(allowlisted) == LocalVerdict(LocalLabel.CLEAN, "authenticated_allowlist")

    store.add_correspondents([address_hash("Pal@Example.test")])
    known = _message("x", sender="pal@example.test", dmarc_pass=True)
    assert gate.evaluate(known) == LocalVerdict(LocalLabel.CLEAN, "known_correspondent")
    # Without trusted authentication a forged From must not be trusted.
    assert gate.evaluate(replace(known, trusted_dmarc_pass=False)) is None


def test_disallowed_language_is_spam_only_when_configured(tmp_path: Path) -> None:
    german = (
        "Sehr geehrter Kunde, Ihr Konto wurde vorübergehend gesperrt. Bitte bestätigen "
        "Sie Ihre Daten innerhalb von 24 Stunden, um eine Schließung zu vermeiden."
    )
    gate, _ = _gate(tmp_path, allowed_languages=("en", "nb", "nn", "da", "sv"))
    assert gate.evaluate(_message(german)) == LocalVerdict(LocalLabel.SPAM, "language_not_allowed")
    unrestricted, _ = _gate(tmp_path / "other")
    assert unrestricted.evaluate(_message(german)) is None


def test_near_duplicate_of_junk_is_spam_unless_it_also_matches_ham(tmp_path: Path) -> None:
    rng = random.Random(2)
    gate, _ = _gate(tmp_path)
    junk = _random_text(rng, SPAM_WORDS, 80)
    gate.learn(_message(junk, key="junk"), LocalLabel.SPAM, weight=1.0, source="folder")
    assert gate.evaluate(_message(junk + " ref 991")) == LocalVerdict(
        LocalLabel.SPAM, "near_duplicate_junk"
    )

    shared = _random_text(rng, HAM_WORDS, 80)
    gate.learn(_message(shared, key="s1"), LocalLabel.SPAM, weight=1.0, source="folder")
    gate.learn(_message(shared, key="s2"), LocalLabel.CLEAN, weight=1.0, source="folder")
    assert gate.evaluate(_message(shared + " ref 992")) is None


def test_bayes_needs_minimum_examples_per_class(tmp_path: Path) -> None:
    rng = random.Random(3)
    gate, _ = _gate(tmp_path, min_examples=50)
    _train(gate, rng)
    assert gate.evaluate(_message(_random_text(rng, SPAM_WORDS))) is None


def test_bayes_classifies_after_training(tmp_path: Path) -> None:
    rng = random.Random(4)
    gate, _ = _gate(tmp_path)
    _train(gate, rng)
    spam = _message(
        _random_text(rng, SPAM_WORDS),
        sender="new@spam.example.test",
        links=("https://cheap-pills.example.test/y",),
    )
    ham = _message(
        _random_text(rng, HAM_WORDS),
        sender="colleague@work.example.test",
        links=("https://intranet.work.example.test/other",),
    )
    assert gate.evaluate(spam) == LocalVerdict(LocalLabel.SPAM, "local_bayes_spam")
    assert gate.evaluate(ham) == LocalVerdict(LocalLabel.CLEAN, "local_bayes_clean")


def test_bayes_clean_defers_on_unfamiliar_link_or_dmarc_fail(tmp_path: Path) -> None:
    rng = random.Random(5)
    gate, _ = _gate(tmp_path)
    _train(gate, rng)
    ham = _message(
        _random_text(rng, HAM_WORDS),
        sender="colleague@work.example.test",
        links=("https://intranet.work.example.test/doc",),
    )
    assert gate.evaluate(ham) is not None
    assert gate.evaluate(replace(ham, links=("https://login-work.example-phish.test/",))) is None
    assert gate.evaluate(replace(ham, trusted_dmarc_fail=True)) is None


def test_learning_reinforces_and_relabels(tmp_path: Path) -> None:
    gate, store = _gate(tmp_path)
    message = _message("meeting agenda for the project budget review", key="m1")
    assert gate.learn(message, LocalLabel.CLEAN, weight=1.0, source="jev") == "added"
    assert gate.learn(message, LocalLabel.CLEAN, weight=1.0, source="jev") == "unchanged"
    assert gate.learn(message, LocalLabel.SPAM, weight=1.0, source="folder") == "relabelled"
    totals = store.gate_totals()
    assert totals["spam_examples"] == 1
    assert totals["ham_examples"] == 0
    assert totals["spam_weight"] >= 3.0
    weights = store.token_weights(features.tokens(message))
    assert all(ham == 0 for _, ham in weights.values())


@pytest.mark.parametrize("weight", [1.0, 3.0])
def test_token_counts_never_go_negative(tmp_path: Path, weight: float) -> None:
    gate, store = _gate(tmp_path)
    message = _message("meeting agenda for the project budget review", key="m1")
    gate.learn(message, LocalLabel.SPAM, weight=weight, source="folder")
    gate.learn(message, LocalLabel.CLEAN, weight=weight, source="correction")
    gate.learn(message, LocalLabel.SPAM, weight=weight, source="correction")
    for spam, ham in store.token_weights(features.tokens(message)).values():
        assert spam >= 0 and ham >= 0


def test_bayes_clean_requires_a_familiar_sender_domain(tmp_path: Path) -> None:
    rng = random.Random(6)
    gate, _ = _gate(tmp_path)
    _train(gate, rng)
    ham_text = _random_text(rng, HAM_WORDS)
    familiar = _message(ham_text, sender="colleague@work.example.test")
    assert gate.evaluate(familiar) == LocalVerdict(LocalLabel.CLEAN, "local_bayes_clean")
    assert gate.evaluate(replace(familiar, sender_address="x@unknown.example.test")) is None


def test_language_rule_defers_for_familiar_or_known_senders(tmp_path: Path) -> None:
    german = (
        "Sehr geehrter Kunde, Ihr Konto wurde vorübergehend gesperrt. Bitte bestätigen "
        "Sie Ihre Daten innerhalb von 24 Stunden, um eine Schließung zu vermeiden."
    )
    gate, store = _gate(tmp_path, allowed_languages=("en", "nb"))
    _train(gate, random.Random(7))
    assert gate.evaluate(_message(german, sender="colleague@work.example.test")) is None
    store.add_correspondents([address_hash("pal@elsewhere.example.test")])
    assert gate.evaluate(_message(german, sender="pal@elsewhere.example.test")) is None
    assert gate.evaluate(_message(german, sender="x@unknown.example.test")) == LocalVerdict(
        LocalLabel.SPAM, "language_not_allowed"
    )
