#!/usr/bin/env python3
"""Install and import the integration inside a disposable Home Assistant container.

Run this in the official HA image, with only custom_components/ and this script
mounted. It uses HA's requirement installer and opens the setup form without
credentials or MyQ network requests. Never run it against a live HA installation.
"""

import asyncio
import importlib
import json
import platform
import socket
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

# The checkout is mounted at /work in CI; scripts/ is not a package installation.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def check_native_http(curl_http: type) -> None:
    """Send one loopback request through each native pool.

    curl_cffi applies the Android ja3, HTTP/2 and extra fingerprint options to a
    request, not when a session is created, so only a request shows this build
    accepts them. The server listens on loopback only; nothing leaves the host.
    """
    from aiohttp import web

    app = web.Application()
    app.router.add_get("/", lambda _request: web.Response(text="ok"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.SockSite(runner, sock).start()
        async with curl_http() as http:
            for pool in (http.native_session, http.native_single_send):
                response = await pool.get(url, timeout=10)
                if response.status_code != 200 or response.text != "ok":
                    raise RuntimeError("Native HTTP loopback request failed")
    finally:
        await runner.cleanup()
        sock.close()


async def check(config_dir: str) -> None:
    from homeassistant.core import HomeAssistant
    from homeassistant.requirements import async_process_requirements

    manifest = json.loads((ROOT / "custom_components/myq/manifest.json").read_text())
    hass = HomeAssistant(config_dir)
    try:
        await async_process_requirements(hass, "myq", manifest["requirements"], is_built_in=False)

        # Import after installation so shared-library/ABI errors fail this job.
        from aiortc import RTCPeerConnection
        from av import CodecContext

        from custom_components.myq.api.curl_http import CurlHttp
        from custom_components.myq.config_flow import MyQFlow
        from custom_components.myq.const import PLATFORMS

        for name in PLATFORMS:
            await hass.async_add_executor_job(
                importlib.import_module, f"custom_components.myq.{name}"
            )
        for codec in ("h264", "opus", "pcm_alaw", "pcm_mulaw"):
            CodecContext.create(codec, "r")
        peer = RTCPeerConnection()
        await peer.close()
        await check_native_http(CurlHttp)
        flow = MyQFlow()
        flow.hass = hass
        flow.context = {"source": "user"}
        result = await flow.async_step_user()
        if result["type"] != "menu" or result["step_id"] != "user":
            raise RuntimeError("MyQ setup menu did not load")
        for step in ("password", "browser_start"):
            result = await getattr(flow, f"async_step_{step}")()
            if result["type"] != "form" or result["step_id"] != step:
                raise RuntimeError(f"MyQ {step} form did not load")
        print(
            f"Home Assistant {version('homeassistant')}, {platform.machine()}: "
            "setup and native imports passed"
        )
        for package in ("aiortc", "av", "curl_cffi"):
            print(f"{package}=={version(package)}")
    finally:
        await hass.async_stop(force=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="myq-runtime-") as config_dir:
        asyncio.run(check(config_dir))
