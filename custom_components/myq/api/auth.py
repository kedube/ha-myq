"""Password and interactive MFA over the app's hosted OAuth form flow.

Email/password and the email Otp form are evidenced by the supplied successful
website-login HAR. Other MFA screens remain provisional; OAuth completion is
validated separately and is never inferred from a website landing page.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

from .app_check import AppCheckProvider
from .const import IDENTITY
from .errors import AuthenticationError, ProtocolError, UnsupportedAuthStep
from .http import HttpTransport, check_status
from .oauth import DEFAULT_LOCALE, OAuthSession, is_callback
from .tokens import TokenManager, Tokens


@dataclass(repr=False)
class Form:
    action: str
    method: str
    hidden: dict[str, str] = field(default_factory=dict)
    inputs: dict[str, dict[str, str]] = field(default_factory=dict)
    choices: dict[str, dict[str, str]] = field(default_factory=dict)


class FormParser(HTMLParser):
    """Parse controls without running remote JavaScript."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self.current: Form | None = None
        self.select: str | None = None
        self.option: str | None = None
        self.has_sso_lookup = False
        # ip0/j.A0 reads #validation-errors after every page load; text there is a
        # failed sign-in. Only its presence is kept, never the server's wording.
        self.validation_error = False
        self._errors_tag: str | None = None
        self._errors_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {key: value or "" for key, value in attrs}
        if self._errors_tag == tag:
            self._errors_depth += 1
        elif self._errors_tag is None and a.get("id") == "validation-errors":
            self._errors_tag, self._errors_depth = tag, 1
        if tag == "script" and a.get("src"):
            src = urlsplit(urljoin(IDENTITY, a["src"]))
            if (
                src.scheme == "https"
                and src.netloc == urlsplit(IDENTITY).netloc
                and src.path.startswith("/Scripts/dist/sso.")
                and src.path.endswith(".js")
            ):
                self.has_sso_lookup = True
        if tag == "form":
            self.current = Form(a.get("action", ""), a.get("method", "get").lower())
            self.forms.append(self.current)
        if self.current is None or "disabled" in a:
            return
        if tag == "select" and a.get("name"):
            self.select = a["name"]
            self.current.choices[self.select] = {}
        elif tag == "option" and self.select and "value" in a:
            self.option = a["value"]
            self.current.choices[self.select][self.option] = ""
        elif tag == "input" and a.get("name"):
            name = a["name"]
            kind = a.get("type", "text").lower()
            if kind == "hidden":
                self.current.hidden[name] = a.get("value", "")
            elif kind == "radio":
                value = a.get("value", "")
                self.current.choices.setdefault(name, {})[value] = a.get("aria-label") or value
            elif kind not in {"submit", "button", "checkbox", "reset"}:
                self.current.inputs[name] = a

    def handle_data(self, data: str) -> None:
        if self._errors_tag is not None and data.strip():
            self.validation_error = True
        if self.current and self.select and self.option is not None:
            self.current.choices[self.select][self.option] += data.strip()

    def handle_endtag(self, tag: str) -> None:
        if self._errors_tag == tag:
            self._errors_depth -= 1
            if self._errors_depth == 0:
                self._errors_tag = None
        if tag == "form":
            self.current = None
        elif tag == "select":
            self.select = self.option = None
        elif tag == "option":
            self.option = None


@dataclass(repr=False)
class Challenge:
    kind: str
    field_name: str
    form: Form
    page_url: str
    choices: dict[str, str] = field(default_factory=dict)


def has_error_id(url: str) -> bool:
    """ip0/j.B0: a page address carrying errorId means the sign-in failed."""
    try:
        values = parse_qs(urlsplit(url).query, keep_blank_values=True, max_num_fields=64)
    except ValueError:
        return False
    return any(values.get("errorId", ()))


def identity_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != urlsplit(IDENTITY).netloc
        or parsed.username
        or parsed.password
    ):
        raise ProtocolError("Refused authentication redirect outside the MyQ identity service")
    return url


class Login:
    def __init__(
        self,
        http: HttpTransport,
        token_manager: TokenManager,
        on_progress: Callable[[str], None] | None = None,
        *,
        unified: bool = False,
        app_check_provider: AppCheckProvider | None = None,
        locale: str = DEFAULT_LOCALE,
    ) -> None:
        self.http = http
        self.token_manager = token_manager
        self.challenge: Challenge | None = None
        self.oauth = OAuthSession(unified=unified, locale=locale)
        self._page_url = ""
        self._on_progress = on_progress
        self._has_sso_lookup = False
        self._validation_error = False
        self.app_check_provider = app_check_provider
        self.form_structure: list[dict] = []

    def _progress(self, message: str) -> None:
        if self._on_progress:
            self._on_progress(message)

    @property
    def authorize_url(self) -> str:
        return self.oauth.authorize_url

    async def _navigate(
        self,
        method: str,
        url: str,
        data: dict[str, str] | None = None,
    ) -> list[Form] | Tokens:
        self.oauth.check_active()
        for _ in range(12):
            if is_callback(url):
                self._progress("Exchanging the authorization code for a session")
                return await self.oauth.complete(
                    url, self.token_manager, app_check_provider=self.app_check_provider
                )
            identity_url(url)
            if has_error_id(url):
                # Fixed message: the errorId value is a server string, never surfaced.
                raise AuthenticationError("MyQ reported that the sign-in failed")
            self._progress(f"Sending sign-in {method} request (30-second network timeout)")
            headers = {}
            if self._page_url:
                headers = {
                    "Referer": identity_url(self._page_url),
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-User": "?1",
                }
            if method == "POST":
                headers["Origin"] = IDENTITY
            response = await self.http.request(method, url, data=data, headers=headers)
            self._progress(f"MyQ sign-in responded with HTTP {response.status}")
            if response.status in {301, 302, 303, 307, 308}:
                location = response.header("Location")
                if not location:
                    raise ProtocolError("MyQ returned an empty sign-in redirect")
                target = urljoin(url, location)
                if response.status in {307, 308} and method == "POST":
                    # Replaying a password/OTP POST may duplicate an operation.
                    raise UnsupportedAuthStep("MyQ requires an unsupported form redirect")
                url, method, data = target, "GET", None
                continue
            check_status(response)
            parser = FormParser()
            parser.feed(response.body.decode("utf-8", errors="replace"))
            self._page_url = url
            self._has_sso_lookup = parser.has_sso_lookup
            self._validation_error = parser.validation_error
            # Structural diagnostics only. Never retain field values, action URLs,
            # hidden values, option labels, HTML, cookies, or return URLs here.
            self.form_structure = [
                {
                    "method": form.method,
                    "inputs": {
                        name: {
                            "type": attrs.get("type", "text"),
                            "autocomplete": attrs.get("autocomplete", ""),
                        }
                        for name, attrs in form.inputs.items()
                    },
                    "choice_fields": {name: len(values) for name, values in form.choices.items()},
                    "hidden_field_count": len(form.hidden),
                }
                for form in parser.forms
            ]
            return parser.forms
        raise ProtocolError("Too many MyQ sign-in redirects")

    async def _check_sso(self, username: str) -> None:
        """Run the captured email focus-out lookup once when the page advertises it."""
        self.oauth.check_active()
        self._progress("Checking the email domain's sign-in method")
        response = await self.http.request(
            "POST",
            IDENTITY + "/api/v1.0/externalaccounts/IsSSOEnabledForDomain",
            json={"email": username},
            headers={
                "Origin": IDENTITY,
                "Referer": identity_url(self._page_url),
                "Content-Type": "application/json; charset=UTF-8",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )
        check_status(response)
        enabled = response.json()
        if type(enabled) is not bool:
            raise ProtocolError("Unexpected MyQ SSO lookup response")
        if enabled:
            raise UnsupportedAuthStep("This email domain requires SSO; use browser sign-in")

    async def _submit(self, form: Form, url: str, values: dict[str, str]) -> list[Form] | Tokens:
        if form.method != "post":
            raise UnsupportedAuthStep("Unsupported MyQ sign-in form method")
        target = identity_url(urljoin(url, form.action))
        return await self._navigate("POST", target, {**form.hidden, **values})

    def _result(self, result: list[Form] | Tokens) -> Challenge | Tokens:
        self.challenge = None
        if isinstance(result, Tokens):
            return result
        for form in result:
            if "Password" in form.inputs:
                raise AuthenticationError("MyQ did not accept the sign-in")
        candidates = [f for f in result if f.method == "post" and (f.inputs or f.choices)]
        if len(candidates) != 1:
            self._raise_if_rejected()
            if urlsplit(self._page_url).path == "/Home/Secure":
                raise UnsupportedAuthStep(
                    "MyQ returned its website landing page without an OAuth callback; "
                    "no API session was obtained"
                )
            raise UnsupportedAuthStep("MyQ requires an unsupported sign-in screen")
        form = candidates[0]
        if len(form.inputs) == 1 and not form.choices:
            name, attrs = next(iter(form.inputs.items()))
            if attrs.get("autocomplete") == "one-time-code" or name.lower() in {
                "code",
                "verificationcode",
                "twofactorcode",
                "securitycode",
                "otp",
            }:
                self.challenge = Challenge("code", name, form, self._page_url)
        elif len(form.choices) == 1 and not form.inputs:
            name, choices = next(iter(form.choices.items()))
            if choices:
                self.challenge = Challenge("method", name, form, self._page_url, choices)
        if self.challenge is None:
            self._raise_if_rejected()
            raise UnsupportedAuthStep("MyQ requires an unsupported MFA or app-verification step")
        return self.challenge

    def _raise_if_rejected(self) -> None:
        # A recognised code form is kept even with an error shown, so a mistyped
        # code can be entered again; anything else with an error box is a failure.
        if self._validation_error:
            raise AuthenticationError("MyQ did not accept the sign-in")

    async def start(self, username: str, password: str) -> Challenge | Tokens:
        self._progress("Opening MyQ hosted sign-in")
        result = await self._navigate("GET", self.authorize_url)
        if isinstance(result, Tokens):
            return result
        forms = [f for f in result if {"Email", "Password"} <= f.inputs.keys()]
        if len(forms) != 1:
            self._raise_if_rejected()
            raise UnsupportedAuthStep("The expected MyQ email/password form was not returned")
        if self._has_sso_lookup:
            await self._check_sso(username)
        self._progress("Submitting email and password")
        return self._result(
            await self._submit(
                forms[0],
                self._page_url,
                {
                    "Email": username,
                    "Password": password,
                },
            )
        )

    async def respond(self, value: str) -> Challenge | Tokens:
        challenge = self.challenge
        if challenge is None:
            raise AuthenticationError("No pending MFA challenge")
        if not value or (challenge.kind == "method" and value not in challenge.choices):
            raise AuthenticationError("Invalid MFA response")
        self._progress("Submitting the verification response")
        return self._result(
            await self._submit(
                challenge.form,
                challenge.page_url,
                {challenge.field_name: value},
            )
        )
