#!/usr/bin/env python3
"""Complete an HA-owned sign-in in local Chrome and save a private result file.

The helper never receives the PKCE verifier or calls the MyQ token endpoint.
It needs genuine App Check proof from an external source; it cannot mint it.
"""

import argparse
import asyncio
import getpass
import importlib.util
import os
import sys
from pathlib import Path

_api = Path(__file__).resolve().parents[1] / "custom_components/myq/api"
_spec = importlib.util.spec_from_file_location("myq_handoff", _api / "__init__.py")
_module = importlib.util.module_from_spec(_spec)
sys.modules["myq_handoff"] = _module
_spec.loader.exec_module(_module)

from myq_handoff.app_check import FileAppCheckProvider, SuppliedAppCheckProvider  # noqa: E402
from myq_handoff.browser import BrowserLogin  # noqa: E402
from myq_handoff.errors import MyQError  # noqa: E402
from myq_handoff.handoff import BrowserAuthorization, create_result  # noqa: E402
from myq_handoff.har import read_callback  # noqa: E402


def write_private_result(path: Path, result: str) -> None:
    """Create a new owner-readable file; never overwrite a prior credential file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(result + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def main(output: Path, token_file: Path | None, use_har: bool) -> None:
    if await asyncio.to_thread(output.exists) or not await asyncio.to_thread(output.parent.is_dir):
        raise FileExistsError
    # No browser/background task is running during these terminal prompts.
    raw_request = input("Paste the sign-in request from Home Assistant: ")  # noqa: ASYNC250
    request = BrowserAuthorization(raw_request.strip())
    provider = FileAppCheckProvider(token_file) if token_file else None
    # Preflight avoids password/MFA when the source is already unavailable.
    # Acquire again after authorization to catch expiry or a renewed file.
    if provider is None:
        token = getpass.getpass("Current MyQ App Check token: ").strip()
        provider = SuppliedAppCheckProvider(token)
        del token
    try:
        await provider.acquire()
        if use_har:
            print("Open this URL in your normal browser with Network / Preserve log enabled:")
            print(request.authorize_url, flush=True)
            path_text = input("Path to the new HAR after the app redirect: ")  # noqa: ASYNC250
            path = Path(path_text.strip().strip("\"'"))
            callback = await asyncio.to_thread(read_callback, path, request)
        else:
            callback = await BrowserLogin(print, oauth=request).authorize()
        proof = await provider.acquire()
        result = create_result(request, callback, proof)
        await asyncio.to_thread(write_private_result, output, result)
        print(f"Saved private sign-in result to {output}")
        print("Paste that file's contents into Home Assistant, then delete the file.")
    finally:
        provider.invalidate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New private result file")
    parser.add_argument("--app-check-file", type=Path, help='External JSON file: {"token": "JWT"}')
    parser.add_argument("--har", action="store_true", help="Use a fresh HAR from normal Chrome")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.output, args.app_check_file, args.har))
    except MyQError as err:
        print(str(err), file=sys.stderr)
        raise SystemExit(1) from None
    except OSError:
        print("Could not create a new private output file; choose an unused path.", file=sys.stderr)
        raise SystemExit(1) from None
    except (KeyboardInterrupt, EOFError):
        print("Sign-in cancelled.", file=sys.stderr)
        raise SystemExit(130) from None
