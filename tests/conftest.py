"""Local synthetic fixtures; no live account or garage commands."""

import base64
import copy
import json
import time
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from custom_components.myq.api.errors import CannotConnect
from custom_components.myq.api.firebase import REGISTRATION_FILE, FirebaseRegistration
from custom_components.myq.api.history import HistoryPage
from custom_components.myq.api.http import Response
from custom_components.myq.api.models import Device
from custom_components.myq.api.tokens import Tokens


@pytest.fixture
def registration():
    return FirebaseRegistration(
        "test-project", "1:123456:android:abcdef", "private-key", "private-debug", "ab" * 20
    )


@pytest.fixture
def app_check_registration_file(hass, tmp_path, registration):
    """Give sign-in tests a synthetic configuration file, never the local credential."""
    hass.config.config_dir = str(tmp_path)
    path = tmp_path / REGISTRATION_FILE
    path.write_text(json.dumps(asdict(registration)))
    return path


@pytest.fixture
def firebase_exchange(monkeypatch, registration):
    """A successful preflight for tests that replace the hosted MyQ login."""
    from .test_firebase import firebase_token

    exchange = AsyncMock(
        return_value=json_response({"token": firebase_token(registration), "ttl": "3600s"})
    )
    monkeypatch.setattr("custom_components.myq.api.http.Http.request", exchange)
    return exchange


@pytest.fixture(autouse=True)
def block_curl_cloud_access(request, monkeypatch):
    """pytest-socket cannot intercept C-level sockets; opt in only for local peers."""
    if "socket_enabled" not in request.fixturenames:
        monkeypatch.setattr(
            "curl_cffi.requests.AsyncSession.request",
            AsyncMock(side_effect=CannotConnect("Network disabled in synthetic test")),
        )


@pytest.fixture(autouse=True)
def unread_account_flags(request, monkeypatch):
    """Live video reads the account's feature flags; HA tests opt into specific flags."""
    if "hass" in request.fixturenames:
        monkeypatch.setattr(
            "custom_components.myq.api.video.Video.account_features",
            AsyncMock(return_value=None),
        )


@pytest.fixture(autouse=True)
def isolate_optional_settings_reads(request, monkeypatch):
    """HA tests opt into specific setting payloads; never contact new cloud routes."""
    if "hass" in request.fixturenames:
        monkeypatch.setattr(
            "custom_components.myq.api.garage.GarageSettings.read",
            AsyncMock(return_value={}),
        )
        monkeypatch.setattr(
            "custom_components.myq.api.management.Management.account_access",
            AsyncMock(return_value={}),
        )
        monkeypatch.setattr(
            "custom_components.myq.api.alerts.Alerts.rules",
            AsyncMock(return_value=()),
        )
        monkeypatch.setattr(
            "custom_components.myq.api.history.History.read",
            AsyncMock(return_value=HistoryPage(())),
        )
        monkeypatch.setattr(
            "custom_components.myq.api.recordings.Recordings.door_clips",
            AsyncMock(return_value=()),
        )
        for method in ("start", "resume", "pause", "close"):
            monkeypatch.setattr(
                f"custom_components.myq.api.status.CameraStatusMonitor.{method}",
                AsyncMock(),
            )


def app_check_jwt(expires_at=None, *, label="synthetic"):
    """Synthetic unsigned JWT shape; never accepted as authentic by these tests."""

    def encode(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    expiry = time.time() + 3600 if expires_at is None else expires_at
    return ".".join((encode({"alg": "RS256"}), encode({"exp": expiry, "sub": label}), "fake"))


@pytest.fixture
def tokens():
    return Tokens("test-access", "test-refresh", time.time() + 3600)


@pytest.fixture
def http():
    return AsyncMock()


@pytest.fixture
def door_payload():
    return {
        "serial_number": "TEST-GDO-1",
        "name": "Test garage",
        "device_type": "wifigaragedooropener",
        "device_family": "garagedoor",
        "device_model": "87504R-267",
        "capabilities": [],
        "state": {
            "online": True,
            "door_state": "closed",
            "monitor_only_mode": False,
            "is_unattended_open_allowed": True,
            "is_unattended_close_allowed": True,
            "absolute_cycle_count": 120,
            "battery_backup_state": "test-state",
        },
    }


@pytest.fixture
def door(door_payload):
    return Device.parse("test-account", copy.deepcopy(door_payload))


@pytest.fixture
def camera_device():
    return Device.parse(
        "test-account",
        {
            "serial_number": "TEST-CAM123",
            "name": "Test camera",
            "device_type": "hawkeye-camera",
            "device_family": "camera",
            "state": {"online": True},
            "capabilities": [],
        },
    )


@pytest.fixture
def camera_details():
    return {
        "id": "CAM123",
        "status": {"online": True},
        "cap": {
            "cam": {"enableSwitch": True, "motion": True},
            "mic": {},
            "spk": {"volMin": 0, "volMax": 10},
        },
        "settings": {
            "cam": {"enabled": True, "motionEnabled": True},
            "mic": {"enabled": True},
            "spk": {"vol": 5},
        },
    }


SIGN_IN_CHOICES = ["password", "browser_start"]


async def choose(hass, result, choice="password"):
    """Pick how to sign in from the setup or reconnect menu."""
    assert result["type"] == "menu" and result["menu_options"] == SIGN_IN_CHOICES
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": choice}
    )


async def start_sign_in(hass, choice="password"):
    """Start setup and choose how to sign in."""
    result = await hass.config_entries.flow.async_init("myq", context={"source": "user"})
    return await choose(hass, result, choice)


def json_response(data, status=200):
    import json

    return Response(status, {"Content-Type": "application/json"}, json.dumps(data).encode())
