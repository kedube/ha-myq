"""Errors carry fixed messages, never server bodies, URLs, or credentials."""


class MyQError(Exception):
    """Base protocol error."""


class CannotConnect(MyQError):
    """Network failure."""


class AuthenticationError(MyQError):
    """Sign-in failed, or MyQ ended the session; only a new sign-in can recover.

    Outside sign-in this is raised only when the token endpoint ends the session
    (invalid_grant or 401.102, as ip0/j.f91078u), never for a single 401 response.
    """


class SessionRenewalFailed(MyQError):
    """The token endpoint refused one renewal without ending the session.

    The app stays signed in after such a refusal (ip0/j.J0) and tries again later.
    """

    def __init__(self) -> None:
        super().__init__("MyQ did not renew the session this time; it will be retried")


class AccessDenied(MyQError):
    """Server policy or account permissions rejected the request."""


class RequestUnauthorized(AccessDenied):
    """A MyQ service answered 401 for one request; the session itself still works.

    As the app's authenticator (ft0/c1) gives up, this fails only that request.
    """

    def __init__(self) -> None:
        super().__init__("MyQ did not authorize this request (HTTP 401)")


class ApplicationVerificationError(AccessDenied):
    """MyQ's explicit application-authorization rejection."""

    def __init__(self) -> None:
        super().__init__("MyQ rejected application verification (401.122)")


class UnsupportedAuthStep(MyQError):
    """The server requires an unimplemented interactive or attestation step."""


class BrowserVerificationRequired(UnsupportedAuthStep):
    """The response is a browser challenge, not a normal API quota response."""

    def __init__(self) -> None:
        super().__init__(
            "MyQ requires browser verification; an HTTP form client cannot complete it"
        )


class ProtocolError(MyQError):
    """Response differs from the observed protocol."""


class AppCheckError(ProtocolError):
    """Local proof acquisition failed before an OAuth request was sent."""

    reason = "unavailable"


class AppCheckUnavailable(AppCheckError):
    def __init__(self) -> None:
        super().__init__("A current MyQ App Check token source is unavailable")


class AppCheckExpired(AppCheckError):
    reason = "expired"

    def __init__(self) -> None:
        super().__init__("App Check proof needs more than five minutes of remaining validity")


class AppCheckInvalid(AppCheckError):
    reason = "invalid"

    def __init__(self) -> None:
        super().__init__("App Check proof is malformed or has no usable expiry")


class AppCheckIntegrityError(AppCheckError):
    reason = "integrity"

    def __init__(self) -> None:
        super().__init__("The native provider could not obtain application integrity proof")


class CommandRejected(MyQError):
    """Device capabilities or safety flags prohibit the action."""


class CommandUncertain(MyQError):
    """Command may have reached the server; never automatically replay it."""


class RateLimited(MyQError):
    """Service requested a cooldown."""

    def __init__(self, retry_after: float = 60, *, server_retry_after: bool = False) -> None:
        super().__init__("MyQ returned HTTP 429; no automatic retry was sent")
        self.retry_after = retry_after
        self.server_retry_after = server_retry_after
