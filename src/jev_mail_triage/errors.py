class TriageError(Exception):
    """Base class for operational errors safe to report without message content.

    ``code`` is an optional stable identifier used by the app protocol; messages stay
    human-readable and may change.
    """

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class ConfigurationError(TriageError):
    pass


class RetryableError(TriageError):
    pass


class PermanentMessageError(TriageError):
    pass
