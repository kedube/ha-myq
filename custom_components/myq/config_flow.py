"""Sign-in with a password or in a browser, MFA, reauthentication, and polling options."""

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api.auth import Challenge, Login
from .api.backoff import ServiceBackoff
from .api.client import Client
from .api.const import IDENTITY
from .api.errors import (
    AccessDenied,
    AppCheckError,
    AppCheckNotConfigured,
    ApplicationVerificationError,
    AuthenticationError,
    BrowserVerificationRequired,
    CannotConnect,
    MyQError,
    ProtocolError,
    RateLimited,
    SessionRenewalFailed,
    UnsupportedAuthStep,
)
from .api.firebase import REGISTRATION_FILE, FirebaseDebugAppCheckProvider, FirebaseRegistration
from .api.http import Http
from .api.oauth import OAuthSession, find_callback, ui_locale
from .api.tokens import Tokens
from .const import (
    CONF_ENABLE_CAMERAS,
    CONF_PANEL_ENABLED,
    CONF_PANEL_ICON,
    CONF_PANEL_REQUIRE_ADMIN,
    CONF_PANEL_TITLE,
    CONF_POLL_INTERVAL,
    CONF_TOKENS,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MIN_POLL_INTERVAL,
)
from .panel_options import panel_options

SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
# Cooldowns that outlive a flow: origins and deadlines only, never credentials.
AUTH_BACKOFF = "auth_backoff"  # Per account (casefolded email): MyQ's services.
APP_CHECK_BACKOFF = "app_check_backoff"  # Shared: Firebase, for every account.
# The first screen of setup and reconnect: each option is a step.
SIGN_IN_CHOICES = ["password", "browser_start"]


def error_key(error: MyQError) -> str:
    if isinstance(error, AppCheckError):
        return "app_check_" + error.reason
    if isinstance(error, ApplicationVerificationError):
        return "app_verification"
    if isinstance(error, BrowserVerificationRequired):
        return "browser_verification"
    if isinstance(error, (AuthenticationError, SessionRenewalFailed)):
        return "invalid_auth"
    if isinstance(error, AccessDenied):
        return "access_denied"
    if isinstance(error, UnsupportedAuthStep):
        return "unsupported_auth"
    if isinstance(error, CannotConnect):
        return "cannot_connect"
    if isinstance(error, RateLimited):
        return "rate_limited"
    return "protocol_error"


class MyQFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._proof_session: aiohttp.ClientSession | None = None
        self._client: Client | None = None
        self._login: Login | None = None
        # Kept across sign-in attempts in this flow, so a mistyped password does not
        # spend another Firebase exchange. Replaced when the registration changes.
        self._provider: FirebaseDebugAppCheckProvider | None = None
        self._provider_key: FirebaseRegistration | None = None
        # The sign-in link shown for browser sign-in; replaced once used or expired.
        self._browser: OAuthSession | None = None
        self._email = ""
        self._cooldown_until = 0.0

    @property
    def _locale(self) -> str:
        return ui_locale(self.hass.config.language, self.hass.config.country)

    @property
    def _backoff(self) -> ServiceBackoff:
        """This account's MyQ cooldowns, kept across clients and restarted flows.

        A cooldown one account's sign-in earns does not hold up another account's.
        A browser sign-in started from the menu has no email until it finishes, so
        such sign-ins share one entry.
        """
        accounts = self.hass.data.setdefault(DOMAIN, {}).setdefault(AUTH_BACKOFF, {})
        return accounts.setdefault(self._email.casefold(), ServiceBackoff())

    @property
    def _app_check_backoff(self) -> ServiceBackoff:
        """Firebase cooldowns, shared by every flow: one registration serves all accounts."""
        return self.hass.data.setdefault(DOMAIN, {}).setdefault(APP_CHECK_BACKOFF, ServiceBackoff())

    def _signed_in(self) -> bool:
        """A session was issued; account checks can be retried without signing in again."""
        tokens = self._client.auth.tokens if self._client is not None else None
        return tokens is not None and bool(tokens.access_token)

    def _error(self, error: MyQError) -> str:
        if isinstance(error, RateLimited):
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + error.retry_after)
        return error_key(error)

    @callback
    def _close_login(self) -> None:
        """End one sign-in attempt, keeping the flow's App Check proof for the next."""
        if self._client is not None:
            self.hass.async_create_task(self._client.close())
            self._client = None
        if self._session is not None:
            # HA owns the shared connector; release this flow's cookie session.
            self._session.detach()
            self._session = None
        self._login = None

    @callback
    def _close(self) -> None:
        """End the sign-in and drop the flow's App Check proof."""
        self._close_login()
        self._browser = None
        if self._provider is not None:
            self._provider.invalidate()
            self._provider = self._provider_key = None
        if self._proof_session is not None:
            self._proof_session.detach()
            self._proof_session = None

    @callback
    def async_remove(self) -> None:
        self._close()

    async def _new_client(self) -> Client:
        self._close_login()
        self._session = async_create_clientsession(
            self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar()
        )
        self._client = Client(self._session, backoff=self._backoff)
        return self._client

    async def _registration(
        self, debug_token: str
    ) -> tuple[FirebaseRegistration, FirebaseRegistration | Path]:
        """The registration to use, and the source the provider loads it from.

        The submitted credential comes first, then the private file. The file is
        validated here and reread by the provider whenever it needs a new proof.
        """
        if debug_token:
            registration = FirebaseRegistration.from_debug_token(debug_token)
            return registration, registration
        path = Path(self.hass.config.path(REGISTRATION_FILE))
        return await self.hass.async_add_executor_job(FirebaseRegistration.read, path), path

    def _app_check(
        self, key: FirebaseRegistration, source: FirebaseRegistration | Path
    ) -> FirebaseDebugAppCheckProvider:
        """The flow's provider for this registration, so a retry reuses its proof."""
        if self._provider is None or self._provider_key != key:
            self._close()
            # Firebase never shares the hosted login's cookies or headers.
            self._proof_session = async_create_clientsession(
                self.hass, auto_cleanup=False, cookie_jar=aiohttp.DummyCookieJar()
            )
            self._provider = FirebaseDebugAppCheckProvider(
                Http(self._proof_session, self._app_check_backoff), source
            )
            self._provider_key = key
        return self._provider

    def _finish_form(self, errors=None):
        return self.async_show_form(
            step_id="finish", data_schema=vol.Schema({}), errors=errors or {}
        )

    async def async_step_finish(self, user_input=None):
        """Retry account checks with the session already issued; never sign in again."""
        if not self._signed_in():
            return self._menu()
        if user_input is None:
            return self._finish_form()
        if time.monotonic() < self._cooldown_until:
            return self._finish_form({"base": "rate_limited"})
        try:
            return await self._finish()
        except AuthenticationError as err:
            self._close()
            return self.async_abort(reason=error_key(err))
        except MyQError as err:
            return self._finish_form({"base": self._error(err)})

    def _menu(self):
        """Choose how to sign in: automatically with a password, or in a browser."""
        return self.async_show_menu(
            step_id="reauth_confirm" if self.source == SOURCE_REAUTH else "user",
            menu_options=SIGN_IN_CHOICES,
        )

    async def async_step_user(self, user_input=None):
        return self._menu()

    def _registration_error(self, debug_token: str, error: MyQError) -> dict[str, str]:
        """An unusable registration: the submitted token's field, or the file's."""
        if debug_token or isinstance(error, AppCheckNotConfigured):
            return {"debug_token": self._error(error)}
        return {"base": self._error(error)}

    async def async_step_password(self, user_input=None):
        """Sign in on MyQ's hosted pages for the person, with their email and password."""
        errors = {}
        if user_input is not None and time.monotonic() < self._cooldown_until:
            errors["base"] = "rate_limited"
        elif user_input is not None:
            self._email = user_input["username"].strip()
            self._close_login()
            debug_token = user_input.get("debug_token", "").strip()
            try:
                key, source = await self._registration(debug_token)
            except MyQError as err:
                return self._credentials_form(self._registration_error(debug_token, err))
            try:
                provider = self._app_check(key, source)
                client = await self._new_client()
                self._login = Login(
                    client.http, client.auth, locale=self._locale, app_check_provider=provider
                )
                # A sign-in that is still cooling down fails before Firebase is asked.
                self._backoff.check(IDENTITY)
                # Fail before sending a password or requesting MFA. complete()
                # reuses this proof, refreshing it if a long sign-in outlives it.
                await provider.acquire()
                result = await self._login.start(self._email, user_input["password"])
                if isinstance(result, Challenge):
                    return await self.async_step_mfa()
                return await self._finish()
            except MyQError as err:
                if self._needs_browser(err):
                    return self._start_browser({"base": "browser_required"})
                errors["base"] = self._error(err)
                if self._signed_in():
                    return self._finish_form(errors)
                self._close_login()
        return self._credentials_form(errors)

    def _needs_browser(self, error: MyQError) -> bool:
        """MyQ showed a browser check or a screen only a browser can complete.

        The debug token was accepted before any MyQ page was opened, so the
        browser sign-in's code exchange can be proved with it.
        """
        return (
            isinstance(error, UnsupportedAuthStep)
            and self._provider is not None
            and not self._signed_in()
        )

    async def async_step_browser_start(self, user_input=None):
        """Browser sign-in from the menu: check the debug token before showing a link.

        As with a password, an unusable token is reported before the person signs
        in, and the code exchange reuses the proof obtained here.
        """
        errors = {}
        if user_input is not None and time.monotonic() < self._cooldown_until:
            errors["base"] = "rate_limited"
        elif user_input is not None:
            debug_token = user_input.get("debug_token", "").strip()
            try:
                key, source = await self._registration(debug_token)
            except MyQError as err:
                return self._browser_start_form(self._registration_error(debug_token, err))
            try:
                provider = self._app_check(key, source)
                # A sign-in that is still cooling down fails before Firebase is asked.
                self._backoff.check(IDENTITY)
                await provider.acquire()
            except MyQError as err:
                errors["base"] = self._error(err)
            else:
                return self._start_browser()
        return self._browser_start_form(errors)

    def _browser_start_form(self, errors=None):
        return self.async_show_form(
            step_id="browser_start",
            data_schema=vol.Schema({vol.Optional("debug_token"): SECRET}),
            errors=errors or {},
        )

    def _start_browser(self, errors=None):
        """A new sign-in link, replacing any hosted sign-in in progress."""
        self._close_login()
        self._browser = None
        return self._browser_form(errors)

    async def async_step_browser(self, user_input=None):
        """Sign in on MyQ's page in the person's own browser, then paste the app address."""
        if self._provider is None:
            return self._browser_start_form()
        errors = {}
        if user_input is not None:
            errors = await self._complete_browser(user_input["callback"])
            if errors is None:
                try:
                    return await self._finish()
                except MyQError as err:
                    errors = {"base": self._error(err)}
            if self._signed_in():
                return self._finish_form(errors)
        return self._browser_form(errors)

    def _browser_form(self, errors=None):
        if self._browser is None:
            self._browser = OAuthSession(locale=self._locale)
        return self.async_show_form(
            step_id="browser",
            data_schema=vol.Schema({vol.Required("callback"): str}),
            errors=errors or {},
            description_placeholders={"authorize_url": self._browser.authorize_url},
        )

    def _credentials_form(self, errors=None):
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema(
                {
                    vol.Required("username", default=self._email): str,
                    vol.Required("password"): SECRET,
                    vol.Optional("debug_token"): SECRET,
                }
            ),
            errors=errors or {},
        )

    async def async_step_mfa(self, user_input=None):
        if self._login is None or self._login.challenge is None:
            return self._menu()
        errors = {}
        if user_input is not None and time.monotonic() < self._cooldown_until:
            errors["base"] = "rate_limited"
        elif user_input is not None:
            try:
                previous_kind = self._login.challenge.kind
                result = await self._login.respond(user_input["response"])
                if isinstance(result, Tokens):
                    return await self._finish()
                if previous_kind == "code" and result.kind == "code":
                    errors["base"] = "invalid_code"
            except MyQError as err:
                if self._needs_browser(err):
                    return self._start_browser({"base": "browser_required"})
                errors["base"] = self._error(err)
                if self._signed_in():
                    return self._finish_form(errors)
                # App Check is only asked again once MyQ has accepted the code and
                # returned to the app, so its 429 leaves no challenge to answer. A 429
                # from the code form itself keeps the code for after the cooldown.
                app_check_limited = (
                    isinstance(err, RateLimited)
                    and self._provider is not None
                    and self._provider.last_status == "rate_limited"
                )
                if (
                    isinstance(err, (AuthenticationError, AppCheckError))
                    or app_check_limited
                    or self._login.oauth.used
                ):
                    self._close_login()
        challenge = self._login.challenge if self._login else None
        if challenge is None:
            self._close_login()
            return self._credentials_form(errors or {"base": "unsupported_auth"})
        schema = vol.In(challenge.choices) if challenge.kind == "method" else SECRET
        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema(
                {
                    vol.Required("response"): schema,
                }
            ),
            errors=errors,
            description_placeholders={
                "instruction": "Choose a verification method."
                if challenge.kind == "method"
                else "Enter the verification code supplied by MyQ.",
            },
        )

    async def _complete_browser(self, pasted: str) -> dict[str, str] | None:
        """Exchange the pasted app address for a session; the form's errors if not.

        The link is replaced when it has expired or MyQ has used or refused it.
        """
        oauth = self._browser
        if time.monotonic() < self._cooldown_until:
            return {"base": "rate_limited"}
        try:
            if oauth is None:
                raise AuthenticationError("No browser sign-in link")
            oauth.check_active()
        except AuthenticationError:
            self._browser = None
            return {"base": "browser_expired"}
        try:
            callback_url = find_callback(pasted)
            if parse_qs(urlsplit(callback_url).query).get("state") != [oauth.state]:
                raise ProtocolError("The address is from a different sign-in link")
            oauth.callback_code(callback_url)
        except AuthenticationError:
            # The address is this link's, but MyQ ended the sign-in (an error=…).
            self._browser = None
            return {"base": "browser_auth_failed"}
        except MyQError:
            return {"callback": "invalid_callback"}
        try:
            client = await self._new_client()
            # A sign-in that is still cooling down fails before Firebase is asked.
            self._backoff.check(IDENTITY)
            await oauth.complete(callback_url, client.auth, app_check_provider=self._provider)
            return None
        except MyQError as err:
            if self._signed_in():
                return {"base": self._error(err)}
            self._close_login()
            if oauth.used or isinstance(err, AuthenticationError):
                # A code that was sent is spent: only a new sign-in can follow.
                self._browser = None
            if isinstance(err, AuthenticationError):
                return {"base": "browser_auth_failed"}
            return {"base": self._error(err)}

    async def _finish(self):
        profile = await self._client.profile()
        await self._client.discover()
        await self.async_set_unique_id(profile["user_id"])
        data = {
            "username": profile.get("email") or self._email,
            CONF_TOKENS: self._client.auth.tokens.as_dict(),
        }
        self._close()
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data["username"] or "MyQ", data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        self._email = entry_data.get("username", "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        return self._menu()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return MyQOptionsFlow()


class MyQOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data={**options, **panel_options(options)})
        panel = panel_options(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_POLL_INTERVAL,
                            DEFAULT_POLL_INTERVAL,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=900)),
                    vol.Required(
                        CONF_ENABLE_CAMERAS,
                        default=self.config_entry.options.get(
                            CONF_ENABLE_CAMERAS,
                            True,
                        ),
                    ): bool,
                    vol.Optional(CONF_PANEL_ENABLED, default=panel["panel_enabled"]): bool,
                    vol.Optional(CONF_PANEL_TITLE, default=panel["panel_title"]): str,
                    vol.Optional(CONF_PANEL_ICON, default=panel["panel_icon"]): str,
                    vol.Optional(
                        CONF_PANEL_REQUIRE_ADMIN, default=panel["panel_require_admin"]
                    ): bool,
                }
            ),
        )
