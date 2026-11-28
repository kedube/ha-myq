"""The setup form must load without curl_cffi's version-specific internal helper."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_config_flow_without_curl_internal_resolver(tmp_path):
    # A fresh interpreter exercises the entire import chain, including __init__,
    # Client and AndroidHttp. In-process tests have already imported those modules.
    result = subprocess.run(
        [sys.executable, "-", str(tmp_path)],
        input="""
import asyncio
import sys

import curl_cffi.requests.impersonate as impersonate

# 0.15 exports this helper; 0.17 does not. Hide it when testing with the old pin.
if hasattr(impersonate, "normalize_browser_type"):
    del impersonate.normalize_browser_type

from homeassistant.config_entries import HANDLERS
from homeassistant.core import HomeAssistant
from custom_components.myq import config_flow
from custom_components.myq.api.curl_http import CurlHttp

async def check():
    hass = HomeAssistant(sys.argv[1])
    flow = HANDLERS["myq"]()
    assert isinstance(flow, config_flow.MyQFlow)
    flow.hass = hass
    flow.context = {"source": "user"}
    result = await flow.async_step_user()
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert list(result["data_schema"].schema) == [
        "username", "password", "debug_token", "browser"
    ]
    for profile in ("chrome_android", "chrome"):
        async with CurlHttp(profile):
            pass

asyncio.run(check())
""",
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
