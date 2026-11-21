"""Recorded-event protocol and HA media proxy with synthetic media only."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import pytest
from homeassistant.components.media_source import MediaSourceError, MediaSourceItem, Unresolvable
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api.account_access import AccountAccess
from custom_components.myq.api.client import Client
from custom_components.myq.api.errors import (
    AccessDenied,
    AuthenticationError,
    CannotConnect,
    ProtocolError,
)
from custom_components.myq.api.http import Response
from custom_components.myq.api.recordings import (
    EVENTS,
    HLS_TYPE,
    Playback,
    Recordings,
    validate_media_url,
)
from custom_components.myq.api.video import VIDEO
from custom_components.myq.const import DOMAIN
from custom_components.myq.media_source import async_get_media_source

from .conftest import json_response


def event(event_id="event-1", **kwargs):
    return {
        "id": event_id,
        "srcId": "CAM123",
        "dttm": "2026-11-20T12:00:00Z",
        "type": "motion",
        "status": "FINALIZED",
        **kwargs,
    }


PLAYLIST = b"#EXTM3U\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n"


def resource():
    return {
        "video.m3u8": {
            "urlPattern": VIDEO + EVENTS + "/{eventId}/media/video.m3u8",
            "contentType": HLS_TYPE,
            "isCdnRedirect": True,
        }
    }


async def test_recordings_wire_pagination_preserves_timestamp_ties(http, tokens, camera_device):
    api = Recordings(Client(transport=http, tokens=tokens))
    rows = [event(f"e-{index:02}") for index in range(50)]
    http.request.side_effect = [
        json_response({"items": rows, "moreAvailable": True}),
        json_response(
            {"items": rows + [event("older", dttm="2026-11-19T12:00:00Z")], "moreAvailable": False}
        ),
    ]
    first = await api.page(camera_device)
    assert len(first.items) == 50
    second = await api.page(camera_device, first.next_cursor)
    assert [item.id for item in second.items] == ["older"]
    assert second.next_cursor is None
    assert http.request.await_args_list[0].args == ("GET", VIDEO + EVENTS)
    assert http.request.await_args_list[0].kwargs == {
        "headers": {"AntiCSRF": "AntiCSRF", "Authorization": "Bearer test-access"},
        "params": {
            "myqAccountId": "test-account",
            "srcId": "CAM123",
            "v": "2",
            "hideUnplayable": "true",
            "includeHidden": "true",
            "limit": "50",
        },
    }
    assert http.request.await_args_list[1].kwargs["params"]["endDttm"] == (
        "2026-11-20T12:00:00.000Z"
    )
    assert http.request.await_args_list[1].kwargs["params"]["limit"] == "100"


@pytest.mark.parametrize("wrong", [{"srcId": "OTHER"}, {"myqAccountId": "OTHER"}])
async def test_cross_camera_and_account_events_never_shown(http, tokens, camera_device, wrong):
    http.request.return_value = json_response({"items": [event(**wrong), event("mine")]})
    page = await Recordings(Client(transport=http, tokens=tokens)).page(camera_device)
    assert [item.id for item in page.items] == ["mine"]


async def test_playback_verifies_event_then_discovers_hls(http, tokens, camera_device):
    http.request.side_effect = [
        json_response(event()),
        json_response(resource()),
        Response(200, {"Content-Type": HLS_TYPE}, PLAYLIST),
    ]
    playback = await Recordings(Client(transport=http, tokens=tokens)).playback(
        camera_device, "event-1"
    )
    assert playback.url == VIDEO + EVENTS + "/event-1/media/video.m3u8"
    assert http.request.await_args_list[0].args == ("GET", VIDEO + EVENTS + "/event-1")
    assert http.request.await_args_list[0].kwargs["params"] == {"v": "2"}
    assert http.request.await_args_list[1].args == ("GET", VIDEO + EVENTS + "/mediaDownloadInfo")
    # The playlist is probed once at resolve; the player's first read reuses it.
    assert http.request.await_args_list[2].args == ("GET", playback.url)
    response, _ = await playback.fetch("0")
    assert response.body == PLAYLIST and http.request.await_count == 3
    assert "event-1" not in repr(playback) and VIDEO not in repr(playback)


@pytest.mark.parametrize(
    "changes", [{"srcId": "OTHER"}, {"myqAccountId": "OTHER"}, {"id": "other"}]
)
async def test_unavailable_event_does_not_request_media(http, tokens, camera_device, changes):
    http.request.return_value = json_response(event(**changes))
    with pytest.raises(AccessDenied):
        await Recordings(Client(transport=http, tokens=tokens)).playback(camera_device, "event-1")
    http.request.assert_awaited_once()


@pytest.mark.parametrize(
    "url",
    [
        "http://media.hubs.tend-us.tendplatform.com/cxs/api/events/event-1/media/video.m3u8",
        "https://127.0.0.1/video.m3u8",
        "https://example.com/video.m3u8",
        "https://cdn.cloudfront.net.evil.invalid/video.m3u8",
        "https://user:password@cdn.cloudfront.net/video.m3u8",
        "https://cdn.cloudfront.net:8443/video.m3u8",
        VIDEO + "/cxs/api/devices/CAM123/settings",
        VIDEO + EVENTS + "/other-event/media/video.m3u8",
        VIDEO + EVENTS + "/event-1/media/%2e%2e/settings",
        VIDEO + EVENTS + "/event-1/media/%5c..%5csettings",
        VIDEO + EVENTS + "/event-1/media/%00",
        "https://bucket.s3.amazonaws.com/file?X-Amz-Date=20000101T000000Z&X-Amz-Expires=60",
        "https://cdn.cloudfront.net/file?Expires=1",
        "https://cdn.cloudfront.net/file?Expires=nan",
    ],
)
def test_media_urls_fail_closed_without_secret_output(url):
    with pytest.raises(ProtocolError) as err:
        validate_media_url(url, "event-1")
    assert url not in str(err.value)


async def test_cdn_redirects_never_receive_myq_credentials(http, tokens):
    source = VIDEO + EVENTS + "/event-1/media/video.m3u8"
    cdn = "https://synthetic.cloudfront.net/event-1/index.m3u8?Signature=private"
    http.request.side_effect = [
        Response(302, {"Location": cdn}, b""),
        Response(200, {}, b"#EXTM3U\nsegment.ts\n"),
    ]
    playback = Playback(Client(transport=http, tokens=tokens), "event-1", source)
    response, final_url = await playback.fetch("0")
    assert final_url == cdn
    assert response.body.startswith(b"#EXTM3U")
    assert (
        http.request.await_args_list[0].kwargs["headers"]["Authorization"] == "Bearer test-access"
    )
    assert http.request.await_args_list[1].kwargs["headers"] == {}
    body = playback.rewrite_manifest(response.body, final_url, lambda key: "/local/" + key)
    assert body == b"#EXTM3U\n/local/1\n"
    assert "Signature" not in body.decode()


async def test_redirect_to_unknown_host_never_requested(http, tokens):
    http.request.return_value = Response(302, {"Location": "https://evil.invalid/private"}, b"")
    playback = Playback(
        Client(transport=http, tokens=tokens),
        "event-1",
        VIDEO + EVENTS + "/event-1/media/video.m3u8",
    )
    with pytest.raises(ProtocolError):
        await playback.fetch("0")
    http.request.assert_awaited_once()


def test_hls_rewrites_variants_keys_and_maps_without_upstream_urls(http, tokens):
    base = VIDEO + EVENTS + "/event-1/media/video.m3u8"
    playback = Playback(Client(transport=http, tokens=tokens), "event-1", base)
    manifest = (
        b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
        b'#EXT-X-MAP:URI="init.mp4"\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n'
        b"variant.m3u8\n#EXTINF:1,\nsegment.ts\n"
    )
    result = playback.rewrite_manifest(manifest, base, lambda key: "/ha/" + key)
    assert b'URI="/ha/1"' in result and b'URI="/ha/2"' in result
    assert b"/ha/3\n" in result and b"/ha/4\n" in result
    assert len(playback.resources) == 5
    assert b"key.bin" not in result and b"tendplatform" not in result


@pytest.mark.parametrize(
    "manifest",
    [
        b"not hls",
        b"#EXTM3U\nhttp://127.0.0.1/a\n",
        b"#EXTM3U\n#EXT-X-KEY:URI=file:///etc/passwd\n",
        b'#EXTM3U\n#EXT-X-KEY:URI="key.bin",URI=https://evil.invalid/key\n',
    ],
)
def test_bad_manifest_fails_closed(http, tokens, manifest):
    base = VIDEO + EVENTS + "/event-1/media/video.m3u8"
    playback = Playback(Client(transport=http, tokens=tokens), "event-1", base)
    with pytest.raises(ProtocolError):
        playback.rewrite_manifest(manifest, base, lambda key: "/ha/" + key)


async def test_expired_and_unknown_resources_not_requested(http, tokens):
    playback = Playback(
        Client(transport=http, tokens=tokens),
        "event-1",
        VIDEO + EVENTS + "/event-1/media/video.m3u8",
    )
    with pytest.raises(AccessDenied):
        await playback.fetch("https://evil.invalid/")
    playback.expires_at = time.monotonic() - 1
    with pytest.raises(AccessDenied):
        await playback.fetch("0")
    http.request.assert_not_awaited()


async def test_non_camera_cannot_request_recordings(http, tokens, door):
    api = Recordings(Client(transport=http, tokens=tokens))
    with pytest.raises(AccessDenied):
        await api.page(door)
    with pytest.raises(AccessDenied):
        await api.playback(door, "event-1")
    http.request.assert_not_awaited()


@pytest.fixture
async def media_source(hass, hass_client, camera_device, http, tokens):
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN, title="MyQ test", state=ConfigEntryState.LOADED)
    entry.add_to_hass(hass)
    access = AccountAccess(
        "test-account", "owner", {"devices": frozenset({"View"}), "history": frozenset({"View"})}
    )
    entry.runtime_data = SimpleNamespace(
        data=SimpleNamespace(
            devices={camera_device.key: camera_device}, account_access={"test-account": access}
        ),
        recordings=Recordings(Client(transport=http, tokens=tokens)),
    )
    source = await async_get_media_source(hass)
    # Media players have no HA bearer header: the signed path must authenticate.
    client = await hass_client(None)
    return source, entry, client


def item(hass, identifier):
    return MediaSourceItem(hass, DOMAIN, identifier, None)


async def test_ha_browse_resolve_and_signed_playlist_segment(hass, media_source, http):
    source, entry, web_client = media_source
    root = await source.async_browse_media(item(hass, ""))
    assert root.children[0].title == "MyQ test"
    accounts = await source.async_browse_media(item(hass, entry.entry_id))
    cameras = await source.async_browse_media(item(hass, accounts.children[0].identifier))
    http.request.return_value = json_response({"items": [event()], "moreAvailable": False})
    events = await source.async_browse_media(item(hass, cameras.children[0].identifier))
    assert events.children[0].title == "Door events" and events.children[0].can_expand
    assert events.children[1].can_play
    http.request.side_effect = [
        json_response(event()),
        json_response(resource()),
        Response(
            200, {"Content-Type": HLS_TYPE}, b"#EXTM3U\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n"
        ),
        Response(200, {"Content-Type": "video/mp2t"}, b"synthetic segment"),
    ]
    resolved = await source.async_resolve_media(item(hass, events.children[1].identifier))
    assert resolved.mime_type == HLS_TYPE
    assert "authSig=" in resolved.url
    assert "test-access" not in resolved.url and "event-1" not in resolved.url
    playlist = await web_client.get(resolved.url)
    assert playlist.status == 200
    text = await playlist.text()
    segment_url = next(line for line in text.splitlines() if not line.startswith("#"))
    segment = await web_client.get(segment_url)
    assert segment.status == 200
    assert await segment.read() == b"synthetic segment"
    assert segment.headers["Cache-Control"] == "no-store"
    assert "tendplatform" not in text and "test-access" not in text


async def test_ha_proxy_requires_auth(hass, media_source, hass_client_no_auth):
    source, _, _ = media_source
    unauthenticated = await hass_client_no_auth()
    response = await unauthenticated.get("/api/myq/recordings/unknown/0")
    assert response.status == 401


async def test_ha_grants_revoked_on_permission_loss(hass, media_source, http):
    source, entry, web_client = media_source
    http.request.side_effect = [
        json_response(event()),
        json_response(resource()),
        Response(200, {"Content-Type": HLS_TYPE}, PLAYLIST),
    ]
    resolved = await source.async_resolve_media(
        item(hass, f"{entry.entry_id}/test-account/TEST-CAM123/event/event-1")
    )
    entry.runtime_data.data.account_access = {}
    response = await web_client.get(resolved.url)
    assert response.status == 410
    assert http.request.await_count == 3
    with pytest.raises(MediaSourceError):
        await source.async_browse_media(item(hass, f"{entry.entry_id}/test-account/TEST-CAM123"))


@pytest.mark.parametrize("reason", ["expired", "reload", "removed"])
async def test_ha_playback_grants_expire_and_invalidate(hass, media_source, http, reason):
    source, entry, web_client = media_source
    http.request.side_effect = [
        json_response(event()),
        json_response(resource()),
        Response(200, {"Content-Type": HLS_TYPE}, PLAYLIST),
    ]
    resolved = await source.async_resolve_media(
        item(hass, f"{entry.entry_id}/test-account/TEST-CAM123/event/event-1")
    )
    token = urlsplit(resolved.url).path.split("/")[-2]
    if reason == "expired":
        source.grants[token].playback.expires_at = 0
    elif reason == "reload":
        entry.runtime_data = SimpleNamespace(data=entry.runtime_data.data)
    else:
        entry.runtime_data.data.devices = {}
    assert (await web_client.get(resolved.url)).status == 410
    assert http.request.await_count == 3


async def test_ha_foreign_account_cannot_resolve(hass, media_source, http):
    source, entry, _ = media_source
    with pytest.raises(MediaSourceError):
        await source.async_resolve_media(
            item(hass, f"{entry.entry_id}/foreign-account/TEST-CAM123/event/event-1")
        )
    http.request.assert_not_awaited()


async def test_ha_proxy_byte_range_preserved_without_forwarding_ha_auth(hass, media_source, http):
    source, entry, web_client = media_source
    http.request.side_effect = [
        json_response(event()),
        json_response(resource()),
        Response(200, {"Content-Type": HLS_TYPE}, b"#EXTM3U\n#EXT-X-BYTERANGE:4@0\nsegment.mp4\n"),
        Response(206, {"Content-Type": "video/mp4", "Content-Range": "bytes 0-3/12"}, b"test"),
    ]
    resolved = await source.async_resolve_media(
        item(hass, f"{entry.entry_id}/test-account/TEST-CAM123/event/event-1")
    )
    playlist = await web_client.get(resolved.url)
    segment_url = next(
        line for line in (await playlist.text()).splitlines() if not line.startswith("#")
    )
    segment = await web_client.get(segment_url, headers={"Range": "bytes=0-3"})
    assert segment.status == 206 and await segment.read() == b"test"
    assert segment.headers["Content-Range"] == "bytes 0-3/12"
    assert http.request.await_args.kwargs["headers"] == {
        "Range": "bytes=0-3",
        "AntiCSRF": "AntiCSRF",
        "Authorization": "Bearer test-access",
    }


async def test_modified_ha_signature_does_not_fetch_media(hass, media_source, http):
    source, entry, web_client = media_source
    http.request.side_effect = [
        json_response(event()),
        json_response(resource()),
        Response(200, {"Content-Type": HLS_TYPE}, PLAYLIST),
    ]
    resolved = await source.async_resolve_media(
        item(hass, f"{entry.entry_id}/test-account/TEST-CAM123/event/event-1")
    )
    response = await web_client.get(resolved.url + "tampered")
    assert response.status == 401
    assert http.request.await_count == 3


@pytest.mark.parametrize(
    "identifier",
    [
        "x" * 160001,
        "{entry}/test-account/TEST-CAM123/extra",
        "{entry}/test-account/TEST-CAM123/next/cursor",
    ],
)
async def test_ha_malformed_recording_folders_are_refused(hass, media_source, identifier):
    source, entry, _ = media_source
    with pytest.raises(MediaSourceError):
        await source.async_browse_media(item(hass, identifier.format(entry=entry.entry_id)))


async def test_ha_resolve_requires_an_event_selection(hass, media_source):
    source, entry, _ = media_source
    with pytest.raises(Unresolvable, match="Select a recording"):
        await source.async_resolve_media(item(hass, f"{entry.entry_id}/test-account/TEST-CAM123"))


async def test_ha_account_without_accessible_cameras_is_refused(hass, media_source):
    source, entry, _ = media_source
    with pytest.raises(MediaSourceError, match="no accessible cameras"):
        await source.async_browse_media(item(hass, f"{entry.entry_id}/other-account"))


async def test_ha_root_hides_entries_with_cameras_disabled(hass, media_source):
    source, entry, _ = media_source
    hass.config_entries.async_update_entry(entry, options={"enable_cameras": False})
    root = await source.async_browse_media(item(hass, ""))
    assert root.children == []


@pytest.mark.parametrize(
    "error,reauth", [(CannotConnect("lost"), False), (AuthenticationError("expired"), True)]
)
async def test_ha_browse_and_resolve_failures_are_reported(hass, media_source, error, reauth):
    source, entry, _ = media_source
    camera = f"{entry.entry_id}/test-account/TEST-CAM123"
    recordings = entry.runtime_data.recordings
    with (
        patch.object(entry, "async_start_reauth") as start_reauth,
        patch.object(recordings, "page", AsyncMock(side_effect=error)),
        patch.object(recordings, "playback", AsyncMock(side_effect=error)),
    ):
        with pytest.raises(MediaSourceError):
            await source.async_browse_media(item(hass, camera))
        with pytest.raises(Unresolvable):
            await source.async_resolve_media(item(hass, camera + "/event/event-1"))
    assert start_reauth.call_count == (2 if reauth else 0)
    assert not source.grants
