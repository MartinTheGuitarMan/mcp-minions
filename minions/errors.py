"""Every failure surfaces as a MinionError whose message starts with a stable code.
Nothing falls back silently: if the backend is down the caller sees MINION_DOWN."""


class MinionError(Exception):
    code = "MINION_ERROR"

    def __init__(self, message: str):
        super().__init__(f"{self.code}: {message}")


class MinionDown(MinionError):
    code = "MINION_DOWN"


class BackendError(MinionError):
    code = "MINION_BACKEND_ERROR"


class SchemaError(MinionError):
    code = "MINION_SCHEMA"

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw  # the model's unparsed output, kept so a judge can repair it


class EmptyValue(SchemaError):
    """extract: a required field came back empty (or 0 with no 0 in the source). Schema-valid, but no answer."""
    code = "MINION_EMPTY"


class NotInSource(SchemaError):
    """extract: a required field holds a value that does not occur in the source text."""
    code = "MINION_NOT_IN_SOURCE"


class Invented(NotInSource):
    """summarize: the summary names a number, date, or capitalised name or term that does not occur in the source."""
    code = "MINION_INVENTED"


class TooLong(SchemaError):
    """summarize: the summary is longer than max_words allows (see values.word_limit)."""
    code = "MINION_TOO_LONG"


class NoQuorum(MinionError):
    code = "MINION_NO_QUORUM"


class BadInput(MinionError):
    code = "MINION_BAD_INPUT"


class BadConfig(MinionError):
    code = "MINION_BAD_CONFIG"


class PrivacyRefused(MinionError):
    code = "MINION_PRIVACY"


class RateLimited(MinionError):
    """A provider rate limit (HTTP 429, or our own per-minute cap). The one failure a route may fail over on."""
    code = "MINION_RATE_LIMITED"

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Busy(MinionError):
    """A model load was refused by the quiet gate (low memory, hold marker, quiet window, battery, ...)."""
    code = "MINION_BUSY"


class LoadFailed(MinionError):
    """The runtime tried to load or unload a model and failed, or the model is not resident afterwards."""
    code = "MINION_LOAD_FAILED"


class RefusedAddress(MinionError):
    """summarize_url: the url's host is local, internal, private or otherwise not a public address (also checked
    after every redirect). Nothing was fetched from it."""
    code = "MINION_REFUSED_ADDRESS"
