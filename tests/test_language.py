import pytest

from jev_mail_triage.errors import ConfigurationError
from jev_mail_triage.language import LanguageChecker, normalize_language_codes

ALLOWED = normalize_language_codes(["en", "no", "da", "sv"])


@pytest.fixture(scope="module")
def checker() -> LanguageChecker:
    return LanguageChecker(ALLOWED)


def test_norwegian_alias_expands_to_bokmal_and_nynorsk() -> None:
    assert ALLOWED == ("en", "nb", "nn", "da", "sv")


def test_unknown_language_code_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        normalize_language_codes(["en", "xx"])


@pytest.mark.parametrize(
    "text",
    [
        (
            "Hei! Takk for møtet i går. Jeg sender deg referatet i morgen, og så kan vi "
            "avtale et nytt tidspunkt for neste uke."
        ),
        (
            "Hej! Tak for mødet i går. Jeg sender dig referatet i morgen, og så kan vi "
            "aftale et nyt tidspunkt i næste uge."
        ),
        (
            "Hej! Tack för mötet igår. Jag skickar referatet i morgon, och sedan kan vi "
            "boka en ny tid nästa vecka."
        ),
        (
            "Hi! Thanks for the meeting yesterday. I will send you the minutes tomorrow, "
            "and then we can agree on a new time next week."
        ),
    ],
)
def test_scandinavian_and_english_are_allowed(checker: LanguageChecker, text: str) -> None:
    assert not checker.is_disallowed(text)


@pytest.mark.parametrize(
    "text",
    [
        (
            "Sehr geehrter Kunde, Ihr Konto wurde vorübergehend gesperrt. Bitte bestätigen "
            "Sie Ihre Daten innerhalb von 24 Stunden, um eine Schließung zu vermeiden."
        ),
        (
            "Estimado cliente, su cuenta ha sido suspendida temporalmente. Por favor "
            "confirme sus datos en las próximas 24 horas para evitar el cierre."
        ),
    ],
)
def test_other_languages_are_disallowed(checker: LanguageChecker, text: str) -> None:
    assert checker.is_disallowed(text)


def test_english_mail_with_foreign_signature_is_allowed(checker: LanguageChecker) -> None:
    text = (
        "Hi Per,\nThanks for the quick reply. I have attached the updated proposal and "
        "we can discuss the budget on Thursday.\nLet me know if that works for you.\n\n"
        "Mit freundlichen Grüßen\nGeschäftsführer der Beispiel GmbH, Sitz der Gesellschaft "
        "ist München."
    )
    assert not checker.is_disallowed(text)


def test_short_text_is_not_judged(checker: LanguageChecker) -> None:
    assert checker.allowed_share("Hallo, wie geht's?") is None
    assert not checker.is_disallowed("Hallo, wie geht's?")


def test_links_and_addresses_are_ignored(checker: LanguageChecker) -> None:
    text = "https://example.test/a/very/long/path?with=query " * 10 + "someone@example.test " * 5
    assert checker.allowed_share(text) is None


def test_short_english_transactional_sentences_are_allowed(checker: LanguageChecker) -> None:
    # Regression: lingua spreads confidence over many languages for short sentences.
    text = (
        "Reservation number #AC318D6CD6\nYour reservation number AC318D6CD6\n"
        "Thank you Per Søderlind\nYour reservation for Hotel La Casa Sul Mare\n"
        "We'll wait for you on 18/06/2025\nThe code to the online Check-In is bd3de5"
    )
    assert not checker.is_disallowed(text)
