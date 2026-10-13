class TriageError(Exception):
    """Base class for operational errors safe to report without message content."""


class ConfigurationError(TriageError):
    pass


class RetryableError(TriageError):
    pass


class PermanentMessageError(TriageError):
    pass
