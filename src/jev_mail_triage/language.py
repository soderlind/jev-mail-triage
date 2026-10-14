from __future__ import annotations

import re
from collections.abc import Iterable
from functools import cache

from lingua import IsoCode639_1, Language, LanguageDetector, LanguageDetectorBuilder

from .errors import ConfigurationError

# "no" is a convenience alias: Norwegian mail can be Bokmål or Nynorsk.
_ALIASES = {"no": ("nb", "nn")}
_URL_OR_ADDRESS_RE = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+", re.IGNORECASE)
_CHUNK_SPLIT_RE = re.compile(r"\n|(?<=[.!?])\s+")
_MAX_TEXT_CHARS = 4000
_MIN_CHUNK_LETTERS = 15
MIN_LETTERS = 50
MIN_ALLOWED_SHARE = 0.2


def normalize_language_codes(codes: Iterable[object]) -> tuple[str, ...]:
    normalized: list[str] = []
    for code in codes:
        if not isinstance(code, str) or not code.strip():
            raise ConfigurationError("allowed_languages must contain ISO 639-1 codes.")
        value = code.strip().casefold()
        for expanded in _ALIASES.get(value, (value,)):
            if not hasattr(IsoCode639_1, expanded.upper()):
                raise ConfigurationError(
                    f"Unsupported language code {code!r} in allowed_languages; use ISO 639-1 "
                    "codes such as en, nb, nn, da, sv."
                )
            if expanded not in normalized:
                normalized.append(expanded)
    return tuple(normalized)


@cache
def _detector() -> LanguageDetector:
    return LanguageDetectorBuilder.from_all_languages().build()


class LanguageChecker:
    """Decides locally whether a message is written in one of the allowed languages.

    The text is checked sentence by sentence so that a message written in an allowed
    language with, for example, a German legal signature is still treated as allowed.
    """

    def __init__(self, allowed_codes: tuple[str, ...]) -> None:
        self._allowed = frozenset(
            Language.from_iso_code_639_1(getattr(IsoCode639_1, code.upper()))
            for code in allowed_codes
        )

    def allowed_share(self, text: str) -> float | None:
        """Share of letters in allowed-language sentences, or None when too little text."""
        cleaned = _URL_OR_ADDRESS_RE.sub(" ", text[:_MAX_TEXT_CHARS])
        total = allowed = 0
        detector = _detector()
        for chunk in _CHUNK_SPLIT_RE.split(cleaned):
            letters = sum(character.isalpha() for character in chunk)
            if letters < _MIN_CHUNK_LETTERS:
                continue
            # Confidence is spread over many languages, so short sentences rarely get a
            # high absolute value. Compare the allowed languages against the best other one.
            allowed_confidence = other_confidence = 0.0
            for value in detector.compute_language_confidence_values(chunk):
                if value.language in self._allowed:
                    allowed_confidence += value.value
                else:
                    other_confidence = max(other_confidence, value.value)
            total += letters
            if allowed_confidence >= other_confidence:
                allowed += letters
        if total < MIN_LETTERS:
            return None
        return allowed / total

    def is_disallowed(self, text: str) -> bool:
        share = self.allowed_share(text)
        return share is not None and share < MIN_ALLOWED_SHARE
