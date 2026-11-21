"""Email/password sign-in, MFA, reauthentication, and polling options."""

import time
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

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
from .api.oauth import ui_locale
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

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None and time.monotonic() < self._cooldown_until:
            errors["base"] = "rate_limited"
        elif user_input is not None:
            self._email = user_input["username"].strip()
            client = await self._new_client()
            try:
                self._login = Login(client.http, client.auth, locale=self._locale)
                result = await self._login.start(self._email, user_input["password"])
                if isinstance(result, Challenge):
                    return await self.async_step_mfa()
                return await self._finish()
            except MyQError as err:
                errors["base"] = self._error(err)
                if self._signed_in():
                    return self._finish_form(errors)
                self._close()
        return self._credentials_form(errors)

    def _credentials_form(self, errors=None):
        return self.async_show_form(
            step_id="reauth_confirm" if self.source == SOURCE_REAUTH else "user",
            data_schema=vol.Schema(
                {
                    vol.Required("username", default=self._email): str,
                    vol.Required("password"): SECRET,
                }
            ),
            errors=errors or {},
        )

    async def async_step_mfa(self, user_input=None):
        if self._login is None or self._login.challenge is None:
            return await self.async_step_user()
        errors = {}
        if user_input is not None:
            try:
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
                if isinstance(err, AuthenticationError) or self._login.oauth.used:
                    self._close()
        challenge = self._login.challenge if self._login else None
        if challenge is None:
            self._close()
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
        return await self.async_step_user(user_input)

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
