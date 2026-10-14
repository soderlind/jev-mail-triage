"""Local pre-filter that runs before Jev.

Checks run in a fixed order, cheapest and most certain first:

1. Header checks: authenticated allowlist and authenticated known correspondents.
2. Allowed languages.
3. Near-duplicates of mail the user filed as junk.
4. A Bayes filter trained on the user's own folders, corrections, and Jev results.

A check either returns a confident local verdict or defers to Jev. Anything the
gate is unsure about goes to Jev.
"""

from __future__ import annotations

import hashlib
import math

from . import features
from .config import GateConfig, PolicyConfig
from .language import LanguageChecker
from .models import Assessment, LocalLabel, LocalVerdict, PreparedMessage
from .policy import is_allowlisted
from .state import LOCAL_MODEL_VERSION, StateStore

GATE_POLICY_VERSION = "gate-v1"
CORRECTION_WEIGHT = 3.0
JEV_CLEAN_MAX_SCORE = 0.05
USER_RESTORED = "user_restored"

# Robinson/Fisher combining, as used by SpamBayes.
_UNKNOWN_WORD_STRENGTH = 0.45
_UNKNOWN_WORD_PROB = 0.5
_MIN_PROB_STRENGTH = 0.1
_MAX_DISCRIMINATORS = 150
# Kept-mail weight a sender domain needs before the gate treats it as familiar.
_FAMILIAR_SENDER_HAM = 3.0


def address_hash(address: str) -> str:
    return hashlib.sha256(f"addr:{address.strip().casefold()}".encode()).hexdigest()


def verdict_assessment(verdict: LocalVerdict) -> Assessment:
    """Represent a local verdict as an assessment so stored decisions replay unchanged."""
    score = 1.0 if verdict.label == LocalLabel.SPAM else 0.0
    return Assessment(
        spam_score=score,
        scam_score=0.0,
        model_version=LOCAL_MODEL_VERSION,
        policy_version=GATE_POLICY_VERSION,
        reason=verdict.reason,
    )


def _chi2q(x2: float, degrees: int) -> float:
    m = x2 / 2.0
    term = total = math.exp(-m)
    for index in range(1, degrees // 2):
        term *= m / index
        total += term
    return min(total, 1.0)


def bayes_score(clues: list[float]) -> float:
    if not clues:
        return 0.5
    count = len(clues)
    spam_log = sum(math.log(1.0 - probability) for probability in clues)
    ham_log = sum(math.log(probability) for probability in clues)
    spam = 1.0 - _chi2q(-2.0 * spam_log, 2 * count)
    ham = 1.0 - _chi2q(-2.0 * ham_log, 2 * count)
    return (spam - ham + 1.0) / 2.0


class LocalGate:
    def __init__(
        self,
        gate: GateConfig,
        policy: PolicyConfig,
        store: StateStore,
        language_checker: LanguageChecker | None = None,
    ) -> None:
        self._gate = gate
        self._policy = policy
        self._store = store
        if language_checker is None and gate.allowed_languages:
            language_checker = LanguageChecker(gate.allowed_languages)
        self._language = language_checker

    def evaluate(self, prepared: PreparedMessage) -> LocalVerdict | None:
        if prepared.trusted_dmarc_pass:
            if is_allowlisted(prepared.sender, self._policy.allowlist):
                return LocalVerdict(LocalLabel.CLEAN, "authenticated_allowlist")
            if prepared.sender_address and self._store.is_correspondent(
                address_hash(prepared.sender_address)
            ):
                return LocalVerdict(LocalLabel.CLEAN, "known_correspondent")

        if (
            self._language is not None
            and not self._known_sender(prepared)
            and self._language.is_disallowed(f"{prepared.subject}\n{prepared.body}")
        ):
            return LocalVerdict(LocalLabel.SPAM, "language_not_allowed")

        signature = features.signature(prepared)
        if signature is not None and self._near_duplicate_of_junk(signature):
            return LocalVerdict(LocalLabel.SPAM, "near_duplicate_junk")

        totals = self._store.gate_totals()
        if (
            totals["spam_examples"] < self._gate.min_examples
            or totals["ham_examples"] < self._gate.min_examples
        ):
            return None
        tokens = features.tokens(prepared)
        weights = self._store.token_weights(tokens)
        score = bayes_score(self._clues(weights, totals))
        if score >= self._gate.bayes_spam_threshold:
            return LocalVerdict(LocalLabel.SPAM, "local_bayes_spam")
        if (
            score <= self._gate.bayes_clean_threshold
            and not prepared.trusted_dmarc_fail
            and self._links_familiar(prepared, weights)
            and self._sender_familiar(prepared, weights)
        ):
            return LocalVerdict(LocalLabel.CLEAN, "local_bayes_clean")
        return None

    def learn(
        self, prepared: PreparedMessage, label: LocalLabel, *, weight: float, source: str
    ) -> str:
        return self._store.learn_example(
            prepared.raw_digest or prepared.fingerprint,
            label,
            weight,
            source,
            features.signature(prepared),
            features.tokens(prepared),
            relabel_weight=CORRECTION_WEIGHT,
        )

    def _near_duplicate_of_junk(self, signature: bytes) -> bool:
        threshold = self._gate.near_duplicate_similarity
        junk = clean = False
        for label, other in self._store.example_signatures():
            if features.similarity(signature, other) >= threshold:
                if label == LocalLabel.SPAM:
                    junk = True
                else:
                    clean = True
        # A template shared with legitimate mail is not evidence of spam.
        return junk and not clean

    @staticmethod
    def _clues(weights: dict[int, tuple[float, float]], totals: dict[str, float]) -> list[float]:
        spam_total = max(totals["spam_weight"], 1.0)
        ham_total = max(totals["ham_weight"], 1.0)
        clues: list[tuple[float, float]] = []
        for spam_count, ham_count in weights.values():
            seen = spam_count + ham_count
            if seen <= 0:
                continue
            spam_ratio = spam_count / spam_total
            ham_ratio = ham_count / ham_total
            probability = spam_ratio / (spam_ratio + ham_ratio)
            adjusted = (_UNKNOWN_WORD_STRENGTH * _UNKNOWN_WORD_PROB + seen * probability) / (
                _UNKNOWN_WORD_STRENGTH + seen
            )
            adjusted = min(max(adjusted, 0.0001), 0.9999)
            strength = abs(adjusted - 0.5)
            if strength >= _MIN_PROB_STRENGTH:
                clues.append((strength, adjusted))
        clues.sort(reverse=True)
        return [probability for _, probability in clues[:_MAX_DISCRIMINATORS]]

    def _known_sender(self, prepared: PreparedMessage) -> bool:
        """Sender we have mail from or wrote to. Only used to defer to Jev, never to trust."""
        if prepared.sender_address and self._store.is_correspondent(
            address_hash(prepared.sender_address)
        ):
            return True
        domain = features.sender_domain(prepared)
        if domain is None:
            return False
        token = features.token_hash(f"from:{domain}")
        return self._sender_familiar(prepared, self._store.token_weights({token}))

    @staticmethod
    def _sender_familiar(
        prepared: PreparedMessage, weights: dict[int, tuple[float, float]]
    ) -> bool:
        """Text alone does not make mail clean; unknown senders must go to Jev."""
        domain = features.sender_domain(prepared)
        if domain is None:
            return False
        spam, ham = weights.get(features.token_hash(f"from:{domain}"), (0.0, 0.0))
        return ham >= _FAMILIAR_SENDER_HAM and ham > spam

    @staticmethod
    def _links_familiar(prepared: PreparedMessage, weights: dict[int, tuple[float, float]]) -> bool:
        """A copied legitimate template with a new link destination must go to Jev."""
        for domain in features.link_domains(prepared):
            _, ham = weights.get(features.link_token(domain), (0.0, 0.0))
            if ham <= 0:
                return False
        return True
