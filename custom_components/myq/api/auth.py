"""Password and interactive MFA over the app's hosted OAuth form flow.

The unified flow handles credentials, MFA and consent before completing OAuth.
See docs/research/android-request-profile.md for behavioral evidence. An ordinary
website landing page never counts as an API session.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit

from .app_check import AppCheckProvider
from .const import HOSTED_HEADERS, HOSTED_NAVIGATION_HEADERS, IDENTITY
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
    controls: list[tuple[str, str]] = field(default_factory=list)
    buttons: list[tuple[str, str]] = field(default_factory=list)

    def editable(self) -> dict[str, dict[str, str]]:
        return {name: attrs for name, attrs in self.inputs.items() if "readonly" not in attrs}

    def identify(self, role: str, page_url: str = "") -> str | None:
        """Use field metadata; only the known OTP endpoint permits a fallback."""
        found = []
        for name, attrs in self.editable().items():
            kind = attrs.get("type", "text").casefold()
            autocomplete = attrs.get("autocomplete", "").casefold()
            names = (name.casefold(), attrs.get("id", "").casefold())
            if role == "password":
                match = kind == "password" and autocomplete != "one-time-code"
            elif role == "email":
                match = (
                    kind == "email"
                    or autocomplete in {"email", "username"}
                    or any("email" in label for label in names)
                )
            else:
                match = autocomplete == "one-time-code" or any(
                    "otp" in label
                    or label.replace("_", "").replace("-", "")
                    in {"code", "verificationcode", "securitycode", "twofactorcode"}
                    for label in names
                )
                match = match and kind in {"text", "tel", "number", "password"}
            if match:
                found.append(name)
        if not found and role == "code":
            target = urlsplit(urljoin(page_url, self.action)).path.casefold()
            if target == "/accountmfa/verifyotp":
                email = self.identify("email")
                candidates = [
                    name
                    for name, attrs in self.editable().items()
                    if name != email
                    and attrs.get("type", "text").casefold() in {"text", "tel", "number"}
                ]
                found = [name for name in candidates if name.casefold().endswith("code")]
                if not found:
                    found = candidates
        return found[0] if len(found) == 1 else None

    def payload(self, values: dict[str, str]) -> dict[str, str] | list[tuple[str, str]]:
        """Preserve successful controls, including repeated checked scope values."""
        pairs = [(name, values.get(name, value)) for name, value in self.controls]
        pairs.extend((name, value) for name, value in values.items() if name not in dict(pairs))
        return dict(pairs) if len(dict(pairs)) == len(pairs) else pairs


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
        elif self._errors_tag is None and (
            a.get("id") == "validation-errors"
            or {"validation-summary-errors", "field-validation-error"}
            & set(a.get("class", "").split())
        ):
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
        elif tag in {"input", "button"} and a.get("name"):
            name = a["name"]
            kind = a.get("type", "submit" if tag == "button" else "text").lower()
            if kind == "hidden":
                self.current.hidden[name] = a.get("value", "")
                self.current.controls.append((name, a.get("value", "")))
            elif kind == "radio":
                value = a.get("value", "")
                self.current.choices.setdefault(name, {})[value] = a.get("aria-label") or value
                if "checked" in a:
                    self.current.controls.append((name, value))
            elif kind == "checkbox":
                if "checked" in a:
                    self.current.controls.append((name, a.get("value", "on")))
            elif kind == "submit":
                self.current.buttons.append((name, a.get("value", "")))
            elif kind not in {"button", "reset", "file", "image"}:
                self.current.inputs[name] = a
                self.current.controls.append((name, a.get("value", "")))

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
        unified: bool = True,
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
        self._consent_submitted = False
        self.app_check_provider = app_check_provider
        self.form_structure: list[dict] = []

    def _progress(self, message: str) -> None:
        if self._on_progress:
            self._on_progress(message)

    @property
    def authorize_url(self) -> str:
        return self.oauth.authorize_url

    @property
    def _accept_language(self) -> str:
        language = self.oauth.locale.split("-")[0]
        return (
            f"{self.oauth.locale},{language};q=0.9" if language != self.oauth.locale else language
        )

    async def _navigate(
        self,
        method: str,
        url: str,
        data: dict[str, str] | list[tuple[str, str]] | None = None,
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
            headers = {
                **HOSTED_NAVIGATION_HEADERS,
                "Accept-Language": self._accept_language,
            }
            if self._page_url:
                headers["Referer"] = identity_url(self._page_url)
            if method == "POST":
                headers["Origin"] = IDENTITY
                headers["Content-Type"] = "application/x-www-form-urlencoded"
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
            parser.close()
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
                **HOSTED_HEADERS,
                "Accept-Language": self._accept_language,
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
            raise UnsupportedAuthStep("This email domain requires SSO, which is unsupported")

    async def _submit(self, form: Form, url: str, values: dict[str, str]) -> list[Form] | Tokens:
        if form.method != "post":
            raise UnsupportedAuthStep("Unsupported MyQ sign-in form method")
        target = identity_url(urljoin(url, form.action))
        return await self._navigate("POST", target, form.payload(values))

    async def _advance(self, result: list[Form] | Tokens) -> Challenge | Tokens:
        """Finish the known consent step before interpreting the next MFA screen."""
        while (
            not isinstance(result, Tokens)
            and urlsplit(self._page_url).path.casefold() == "/consent"
        ):
            self._raise_if_rejected()
            if self._consent_submitted:
                raise UnsupportedAuthStep("MyQ repeated the consent step; start a new sign-in")
            forms = [
                form
                for form in result
                if form.method == "post"
                and urlsplit(urljoin(self._page_url, form.action)).path.casefold() == "/consent"
            ]
            if len(forms) != 1:
                raise UnsupportedAuthStep("MyQ returned an unrecognized consent form")
            form = forms[0]
            target = identity_url(urljoin(self._page_url, form.action))
            if form.buttons and ("button", "yes") not in form.buttons:
                raise UnsupportedAuthStep("MyQ returned an unrecognized consent action")
            # Consent is a one-shot action in this transaction, just like the
            # code exchange. A lost response must not cause a second POST.
            self._consent_submitted = True
            self.challenge = None
            self._progress("Completing MyQ application consent")
            result = await self._submit(form, self._page_url, {"button": "yes"})
            if isinstance(result, Tokens):
                break
            self._raise_if_rejected()
            if urlsplit(self._page_url).path.casefold() != "/consent":
                break
            # This endpoint can acknowledge with 200 rather than redirecting.
            # Resume only its identity-service authorization callback, never a
            # custom URI or an arbitrary returnUrl from the HTML.
            try:
                destinations = [
                    value
                    for name, value in parse_qsl(urlsplit(target).query, max_num_fields=64)
                    if name.casefold() == "returnurl"
                ]
            except ValueError:
                raise ProtocolError("Invalid consent continuation") from None
            if len(destinations) != 1 or not destinations[0]:
                raise UnsupportedAuthStep(
                    "MyQ consent did not provide an authorization continuation"
                )
            resume = identity_url(urljoin(IDENTITY, destinations[0]))
            parsed = urlsplit(resume)
            if parsed.path != "/connect/authorize/callback" or parsed.fragment:
                raise ProtocolError("Unexpected consent continuation")
            result = await self._navigate("GET", resume)
        return self._result(result)

    async def _prefer_email(self, challenge: Challenge | Tokens) -> Challenge | Tokens:
        if not isinstance(challenge, Challenge) or challenge.kind != "code":
            return challenge
        fields = {name.casefold(): value for name, value in challenge.form.hidden.items()}
        if fields.get("selectedmfamethod", "").casefold() != "sms":
            return challenge
        if fields.get("isemailmethodenabled", "").casefold() == "false":
            return challenge
        page = urlsplit(identity_url(challenge.page_url))
        if page.path.casefold() != "/accountmfa/verifyotp":
            return challenge
        try:
            query = [
                (name, value)
                for name, value in parse_qsl(page.query, keep_blank_values=True, max_num_fields=64)
                if name.casefold() != "selectedmfamethod"
            ]
        except ValueError:
            raise ProtocolError("Invalid MFA method selection address") from None
        query.append(("selectedMfaMethod", "Email"))
        self._progress("Requesting the verification code by email")
        return await self._advance(
            await self._navigate("GET", page._replace(query=urlencode(query)).geturl())
        )

    def _result(self, result: list[Form] | Tokens) -> Challenge | Tokens:
        self.challenge = None
        if isinstance(result, Tokens):
            return result
        for form in result:
            if form.identify("password") is not None:
                raise AuthenticationError("MyQ did not accept the sign-in")
        code_forms = [
            (form, name)
            for form in result
            if form.method == "post" and (name := form.identify("code", self._page_url)) is not None
        ]
        if len(code_forms) == 1:
            form, name = code_forms[0]
            self.challenge = Challenge("code", name, form, self._page_url)
            return self.challenge
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
        if len(form.choices) == 1 and not form.inputs:
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
        if urlsplit(self._page_url).path.casefold() == "/consent":
            return await self._advance(result)
        forms = [
            (form, email, secret)
            for form in result
            if (email := form.identify("email")) is not None
            and (secret := form.identify("password")) is not None
        ]
        if len(forms) != 1:
            self._raise_if_rejected()
            raise UnsupportedAuthStep("The expected MyQ email/password form was not returned")
        if self._has_sso_lookup and not self.oauth.unified:
            await self._check_sso(username)
        form, email, secret = forms[0]
        self._progress("Submitting email and password")
        outcome = await self._advance(
            await self._submit(
                form,
                self._page_url,
                {email: username, secret: password},
            )
        )
        return await self._prefer_email(outcome)

    async def respond(self, value: str) -> Challenge | Tokens:
        challenge = self.challenge
        if challenge is None:
            raise AuthenticationError("No pending MFA challenge")
        if not value or (challenge.kind == "method" and value not in challenge.choices):
            raise AuthenticationError("Invalid MFA response")
        self._progress("Submitting the verification response")
        return await self._advance(
            await self._submit(
                challenge.form,
                challenge.page_url,
                {challenge.field_name: value},
            )
        )
