from __future__ import annotations

from typing import Protocol

from typesafe_sdk import Noul, NoulCriteria, TypeSafeError

from .errors import RetryableError
from .models import Assessment, PreparedMessage

POLICY_VERSION = "email-v1"


class SystemOneClient(Protocol):
    def system_one(self, **kwargs: object) -> object: ...


class JevAssessor:
    def __init__(
        self,
        client: SystemOneClient,
        *,
        model: str,
        policy_version: str = POLICY_VERSION,
    ) -> None:
        self._client = client
        self._model = model
        self._policy_version = policy_version

    def assess(self, prepared: PreparedMessage) -> Assessment:
        try:
            response = self._client.system_one(
                model=self._model,
                state={
                    "email": prepared.prompt_text,
                    "language": "Norwegian or English; assess the message in its original language.",
                },
                questions={
                    "spam": Noul(
                        instructions=(
                            "Is this unsolicited spam? Do not classify a newsletter as spam "
                            "when the evidence suggests the recipient subscribed to it. "
                            "Receipts, genuine correspondence, and other expected transactional "
                            "or personal messages are not spam."
                        ),
                        criteria=NoulCriteria(
                            true=(
                                "Unsolicited bulk or promotional mail the recipient did not "
                                "request."
                            ),
                            false=(
                                "Subscribed newsletter, receipt, genuine correspondence, or "
                                "other expected legitimate email."
                            ),
                        ),
                    ),
                    "scam_phishing": Noul(
                        instructions=(
                            "Is this a scam or phishing attempt? Evaluate deceptive intent to "
                            "steal credentials or personal data, obtain money, impersonate a "
                            "trusted party, or induce another harmful action. A legitimate "
                            "message is not malicious merely because it contains links."
                        ),
                        criteria=NoulCriteria(
                            true="The message uses deception to induce a harmful action.",
                            false=(
                                "No deceptive harmful intent is evident; ordinary legitimate "
                                "links do not count."
                            ),
                        ),
                    ),
                },
            )
        except TypeSafeError as exc:
            raise RetryableError("TypeSafe request failed.") from exc

        answers = getattr(response, "answers", None)
        if not isinstance(answers, dict):
            raise RetryableError("TypeSafe response has no typed answers mapping.")
        spam = getattr(answers.get("spam"), "noul", None)
        scam = getattr(answers.get("scam_phishing"), "noul", None)
        if not self._valid_probability(spam) or not self._valid_probability(scam):
            raise RetryableError("TypeSafe returned an invalid probability response.")
        return Assessment(
            spam_score=float(spam),
            scam_score=float(scam),
            model_version=self._model,
            policy_version=self._policy_version,
        )

    @staticmethod
    def _valid_probability(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0.0 <= float(value) <= 1.0
        )
