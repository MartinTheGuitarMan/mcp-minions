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


class NoQuorum(MinionError):
    code = "MINION_NO_QUORUM"


class BadInput(MinionError):
    code = "MINION_BAD_INPUT"


class BadConfig(MinionError):
    code = "MINION_BAD_CONFIG"
