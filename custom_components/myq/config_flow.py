"""Interactive sign-in, MFA, token import, reauthentication, and polling options."""

import time
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api.app_check import SuppliedAppCheckProvider
from .api.auth import Challenge, Login
from .api.client import Client
from .api.errors import (
    AccessDenied,
    AppCheckError,
    ApplicationVerificationError,
    AuthenticationError,
    BrowserVerificationRequired,
    CannotConnect,
    MyQError,
    RateLimited,
    SessionRenewalFailed,
    UnsupportedAuthStep,
)
from .api.handoff import create_request, read_result
from .api.oauth import OAuthSession, ui_locale
from .api.onetime import LINK_PATH, one_time_token
from .api.tokens import Tokens
from .const import (
    CONF_ENABLE_CAMERAS,
    CONF_POLL_INTERVAL,
    CONF_TOKENS,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MIN_POLL_INTERVAL,
)

SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
MENU = ["browser", "onetime", "login", "token"]
# Passed as placeholders: hassfest does not allow URLs in translation strings.
HELPER_GUIDE = "https://github.com/kedube/ha-myq/blob/main/docs/browser-sign-in.md"
ONE_TIME_LINK = "https://www.myliftmaster.com" + LINK_PATH


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
        self._client: Client | None = None
        self._login: Login | None = None
        self._oauth: OAuthSession | None = None
        self._email = ""
        self._cooldown_until = 0.0

    @property
    def _locale(self) -> str:
        return ui_locale(self.hass.config.language, self.hass.config.country)

    def _signed_in(self) -> bool:
        """A session was issued; account checks can be retried without signing in again."""
        tokens = self._client.auth.tokens if self._client is not None else None
        return tokens is not None and bool(tokens.access_token)

    def _error(self, error: MyQError) -> str:
        if isinstance(error, RateLimited):
            self._cooldown_until = time.monotonic() + error.retry_after
        return error_key(error)

    @callback
    def _close(self) -> None:
        if self._session is not None:
            # HA owns the shared connector; release this flow's cookie session.
            self._session.detach()
            self._session = None
        if self._login is not None and self._login.app_check_provider is not None:
            self._login.app_check_provider.invalidate()
        self._login = None
        self._oauth = None

    @callback
    def async_remove(self) -> None:
        self._close()

    async def _new_client(self) -> Client:
        self._close()
        self._session = async_create_clientsession(
            self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar()
        )
        self._client = Client(self._session)
        return self._client

    async def async_step_user(self, user_input=None):
        return self.async_show_menu(step_id="user", menu_options=MENU)

    async def async_step_browser(self, user_input=None):
        errors = {}
        if self._oauth is None:
            await self._new_client()
            self._oauth = OAuthSession(locale=self._locale)
        if user_input is not None:
            provider = None
            try:
                if time.monotonic() < self._cooldown_until:
                    raise RateLimited(self._cooldown_until - time.monotonic())
                callback_url, token = read_result(user_input["handoff"])
                # Validate state before parsing or acquiring supplied proof.
                self._oauth.callback_code(callback_url)
                provider = SuppliedAppCheckProvider(token)
                await self._oauth.complete(
                    callback_url,
                    self._client.auth,
                    app_check_provider=provider,
                    require_app_check=True,
                )
                return await self._finish()
            except MyQError as err:
                errors["base"] = self._error(err)
                if self._signed_in():
                    # Discovery can be retried without replaying the OAuth code.
                    return self._finish_form(errors)
            finally:
                if provider is not None:
                    provider.invalidate()
        try:
            self._oauth.check_active()
        except AuthenticationError:
            await self._new_client()
            self._oauth = OAuthSession(locale=self._locale)
        return self.async_show_form(
            step_id="browser",
            data_schema=vol.Schema({vol.Required("handoff"): SECRET}),
            description_placeholders={
                "request": create_request(self._oauth),
                "guide_url": HELPER_GUIDE,
            },
            errors=errors,
        )

    def _finish_form(self, errors=None):
        return self.async_show_form(
            step_id="finish", data_schema=vol.Schema({}), errors=errors or {}
        )

    async def async_step_finish(self, user_input=None):
        """Retry account checks with the session already issued; never sign in again."""
        if not self._signed_in():
            return await self.async_step_user()
        if user_input is None:
            return self._finish_form()
        try:
            return await self._finish()
        except AuthenticationError as err:
            self._close()
            return self.async_abort(reason=error_key(err))
        except MyQError as err:
            return self._finish_form({"base": self._error(err)})

    async def async_step_onetime(self, user_input=None):
        """Sign in with a one-time MyQ link, as the app's link handler does."""
        errors = {}
        if user_input is not None and time.monotonic() < self._cooldown_until:
            errors["base"] = "rate_limited"
        elif user_input is not None:
            try:
                token = one_time_token(user_input["link"])
            except ValueError:
                errors["base"] = "invalid_link"
            else:
                client = await self._new_client()
                try:
                    # Sent once: MyQ consumes the token whether or not setup finishes.
                    await client.auth.exchange_one_time(token)
                    return await self._finish()
                except MyQError as err:
                    errors["base"] = self._error(err)
                    if self._signed_in():
                        return self._finish_form(errors)
                    self._close()
        return self.async_show_form(
            step_id="onetime",
            data_schema=vol.Schema({vol.Required("link"): SECRET}),
            description_placeholders={"link_start": ONE_TIME_LINK},
            errors=errors,
        )

    async def async_step_login(self, user_input=None):
        errors = {}
        if user_input is not None and time.monotonic() < self._cooldown_until:
            errors["base"] = "rate_limited"
        elif user_input is not None:
            self._email = user_input["username"].strip()
            client = await self._new_client()
            try:
                provider = (
                    SuppliedAppCheckProvider(user_input["app_check_token"].strip())
                    if user_input.get("app_check_token")
                    else None
                )
                self._login = Login(
                    client.http, client.auth, app_check_provider=provider, locale=self._locale
                )
                result = await self._login.start(self._email, user_input["password"])
                if isinstance(result, Challenge):
                    return await self.async_step_mfa()
                return await self._finish()
            except MyQError as err:
                errors["base"] = self._error(err)
                if self._signed_in():
                    return self._finish_form(errors)
                self._close()
        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema(
                {
                    vol.Required("username", default=self._email): str,
                    vol.Required("password"): SECRET,
                    vol.Optional("app_check_token"): SECRET,
                }
            ),
            errors=errors,
        )

    async def async_step_mfa(self, user_input=None):
        if self._login is None or self._login.challenge is None:
            return await self.async_step_login()
        errors = {}
        if user_input is not None:
            try:
                if user_input.get("app_check_token"):
                    self._login.app_check_provider = SuppliedAppCheckProvider(
                        user_input["app_check_token"].strip()
                    )
                previous_kind = self._login.challenge.kind
                result = await self._login.respond(user_input["response"])
                if isinstance(result, Tokens):
                    return await self._finish()
                if previous_kind == "code" and result.kind == "code":
                    errors["base"] = "invalid_code"
            except MyQError as err:
                errors["base"] = self._error(err)
                if self._signed_in():
                    return self._finish_form(errors)
                if isinstance(err, AuthenticationError):
                    self._close()
        challenge = self._login.challenge if self._login else None
        if challenge is None:
            self._close()
            return self.async_show_form(
                step_id="login",
                data_schema=vol.Schema(
                    {
                        vol.Required("username", default=self._email): str,
                        vol.Required("password"): SECRET,
                    }
                ),
                errors=errors or {"base": "unsupported_auth"},
            )
        schema = vol.In(challenge.choices) if challenge.kind == "method" else SECRET
        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema(
                {
                    vol.Required("response"): schema,
                    vol.Optional("app_check_token"): SECRET,
                }
            ),
            errors=errors,
            description_placeholders={
                "instruction": "Choose a verification method."
                if challenge.kind == "method"
                else "Enter the verification code supplied by MyQ.",
            },
        )

    async def async_step_token(self, user_input=None):
        errors = {}
        if user_input is not None and time.monotonic() < self._cooldown_until:
            errors["base"] = "rate_limited"
        elif user_input is not None:
            client = await self._new_client()
            client.auth.tokens = Tokens("", user_input["refresh_token"].strip(), 0)
            try:
                await client.auth.access_token()
                return await self._finish()
            except MyQError as err:
                errors["base"] = self._error(err)
                if self._signed_in():
                    # The pasted token was already exchanged for a new one; keep it.
                    return self._finish_form(errors)
                self._close()
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema(
                {
                    vol.Required("refresh_token"): SECRET,
                }
            ),
            errors=errors,
        )

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
        return self.async_show_menu(step_id="reauth_confirm", menu_options=MENU)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return MyQOptionsFlow()


class MyQOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
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
                }
            ),
        )
