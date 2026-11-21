"""App-matched recordings: lenient lists, door clips, thumbnails, MP4 fallback, proxy limits."""

from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.components.media_source import MediaSourceItem, Unresolvable
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.api import recordings as rec
from custom_components.myq.api.account_access import AccountAccess
from custom_components.myq.api.client import Client
from custom_components.myq.api.errors import AccessDenied, ProtocolError
from custom_components.myq.api.http import Http, Response
from custom_components.myq.api.recordings import (
    EVENTS,
    HLS_TYPE,
    MP4_TYPE,
    PLAYBACK_MAX,
    Playback,
    Recording,
    Recordings,
    event_labels,
)
from custom_components.myq.api.video import VIDEO
from custom_components.myq.const import DOMAIN
from custom_components.myq.media_source import (
    async_get_media_source,
    recording_content_id,
    title,
)

from .conftest import json_response

JPEG = b"\xff\xd8\xffsynthetic-thumbnail"
PLAYLIST = b"#EXTM3U\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n"


def event(event_id="event-1", **kwargs):
    return {
        "id": event_id,
        "srcId": "CAM123",
        "dttm": "2026-11-20T12:00:00Z",
        "type": "motion",
        "status": "FINALIZED",
        **kwargs,
    }


def info(*, hls=True, cdn=True, thumbnail=False):
    data = {}
    if hls:
        data["video.m3u8"] = {"urlPattern": VIDEO + EVENTS + "/{eventId}/media/video.m3u8"}
    if cdn:
        data["videoCdn"] = {"urlPattern": VIDEO + EVENTS + "/{eventId}/media/video.mp4"}
    if thumbnail:
        data["thumbnail"] = {"urlPattern": VIDEO + EVENTS + "/{eventId}/media/thumb.jpg"}
    return data


def api(http, tokens):
    return Recordings(Client(transport=http, tokens=tokens))


# --- Listing ------------------------------------------------------------------------------


async def test_rows_the_app_drops_do_not_hide_the_rest(http, tokens, camera_device):
    # EventsApiResponse skips rows without id or dttm; a null type reads as "".
    rows = [
        event("ok:1.a", type=None),
        event("missing-time", dttm=None),
        event("../escape"),
        {"srcId": "CAM123", "dttm": "2026-11-20T11:00:00Z"},
        event("other", srcId="OTHER"),
        event("ok:1.a"),  # Duplicate of the first.
        event("zero", duration=0),  # FilteredEventsView requires a positive duration.
        "not an object",
    ]
    http.request.return_value = json_response({"items": rows, "moreAvailable": None})
    page = await api(http, tokens).page(camera_device)
    assert [item.id for item in page.items] == ["ok:1.a"]
    assert page.items[0].kind == ""
    assert page.next_cursor is None  # moreAvailable null: fewer rows than the limit.


async def test_rows_newer_than_the_boundary_are_skipped(http, tokens, camera_device):
    first = await _first_page(http, tokens, camera_device)
    http.request.reset_mock(return_value=True, side_effect=True)
    http.request.return_value = json_response(
        {
            "items": [
                event("newer", dttm="2026-11-21T00:00:00Z"),
                event("older", dttm="2026-11-01T00:00:00Z"),
            ]
        }
    )
    page = await api(http, tokens).page(camera_device, first.next_cursor)
    assert [item.id for item in page.items] == ["older"]


async def _first_page(http, tokens, camera_device):
    http.request.return_value = json_response(
        {"items": [event(f"e-{i:02}") for i in range(50)], "moreAvailable": True}
    )
    return await api(http, tokens).page(camera_device)


async def test_a_page_with_nothing_displayable_still_advances(http, tokens, camera_device):
    rows = [event(f"bad/{i}") for i in range(50)]
    http.request.return_value = json_response({"items": rows, "moreAvailable": True})
    page = await api(http, tokens).page(camera_device)
    assert page.items == ()
    http.request.return_value = json_response({"items": [], "moreAvailable": False})
    await api(http, tokens).page(camera_device, page.next_cursor)
    assert http.request.await_args.kwargs["params"]["endDttm"] == "2026-11-20T11:59:59.999Z"


async def test_door_folder_requests_door_linked_clips(http, tokens, camera_device):
    rows = [
        event("clip", type="VIDEO", tags={"accessEventID.GD_OPEN": "2026-11-20T11:59:58Z"}),
        event("door-record", type="GD_OPEN", tags={"accessEventID": "2026-11-20T11:59:58Z"}),
        event("motion", type="VIDEO", tags={"peopleCount": 1}),
    ]
    http.request.return_value = json_response({"items": rows, "moreAvailable": False})
    page = await api(http, tokens).page(camera_device, door=True)
    assert [item.id for item in page.items] == ["clip"]
    params = http.request.await_args.kwargs["params"]
    assert params["tag"] == "accessEventID*"
    assert params["includeHidden"] == "true" and params["hideUnplayable"] == "true"


async def test_door_clips_window_and_match(http, tokens, camera_device):
    rows = [
        event(
            "late",
            type="video",
            dttm="2026-11-20T12:00:05Z",
            tags={"accessEventID.GD_CLOSE": "2026-11-20T12:00:00Z"},
        ),
        event(
            "early",
            type="VIDEO",
            dttm="2026-11-20T11:00:05Z",
            tags={"accessEventID.GD_OPEN": "2026-11-20T11:00:00Z"},
        ),
        event("no-door", type="VIDEO"),
        event(
            "foreign",
            srcId="OTHER",
            type="VIDEO",
            tags={"accessEventID.GD_OPEN": "2026-11-20T11:00:00Z"},
        ),
    ]
    http.request.return_value = json_response({"items": rows})
    clips = await api(http, tokens).door_clips(
        camera_device, "2026-11-20T10:00:00Z", "2026-11-20T13:00:00+00:00"
    )
    assert [(c.id, c.door, c.door_time) for c in clips] == [
        ("early", "open", "2026-11-20T11:00:00Z"),
        ("late", "close", "2026-11-20T12:00:00Z"),
    ]
    params = http.request.await_args.kwargs["params"]
    assert params["startDttm"] == "2026-11-20T10:00:00.000Z"
    assert params["endDttm"] == "2026-11-20T13:00:00.000Z"
    assert params["tag"] == "accessEventID*" and params["v"] == "2"
    assert params["srcId"] == "CAM123"


def test_labels_follow_the_app_tags():
    found, door, door_time = event_labels(
        {
            "peopleCount": 2,
            "movingVehicleCount": "1",
            "movingPackageCount": 0,
            "delivery": "true",
            "grg.door.myQ.open": "2026-11-20T12:00:00Z",
            "accessEventID.GD_OPEN": "2026-11-20T12:00:00Z",
            "dogCount": 1,
            "autoSecureEventId": "x",
        }
    )
    assert found == ("delivery", "garage door opened", "person", "vehicle", "pet", "auto secure")
    assert (door, door_time) == ("open", "2026-11-20T12:00:00Z")
    assert event_labels(None) == ((), None, None)


async def test_titles_use_local_time_duration_and_labels(hass, camera_device):
    await hass.config.async_set_time_zone("America/Chicago")
    recording = Recording.parse(
        event(duration=23.6, tags={"peopleCount": 1}, status="processing"), camera_device
    )
    assert title(recording) == "2026-11-20 06:00:00 · 24 s · Person (is still processing)"
    plain = Recording.parse(event(type="motion"), camera_device)
    assert title(plain).endswith("· motion")


# --- Playback -----------------------------------------------------------------------------


async def test_processing_status_does_not_block_playback(http, tokens, camera_device):
    # l71/f loads the clip without a status check.
    http.request.side_effect = [
        json_response(event(status="PROCESSING")),
        json_response(info()),
        Response(200, {"Content-Type": HLS_TYPE}, PLAYLIST),
    ]
    playback = await api(http, tokens).playback(camera_device, "event-1")
    assert playback.content_type == HLS_TYPE


async def test_hls_failure_falls_back_to_progressive_cdn(http, tokens, camera_device):
    # a81/d.k(): swap to the videoCdn progressive source when HLS fails.
    http.request.side_effect = [
        json_response(event()),
        json_response(info()),
        Response(404, {}, b""),
    ]
    playback = await api(http, tokens).playback(camera_device, "event-1")
    assert playback.progressive and playback.content_type == MP4_TYPE
    assert playback.url == VIDEO + EVENTS + "/event-1/media/video.mp4"


async def test_no_media_with_a_status_hint_explains_why(http, tokens, camera_device):
    http.request.side_effect = [
        json_response(event(status="FINALIZING")),
        json_response(info(hls=False, cdn=False)),
    ]
    with pytest.raises(ProtocolError, match="still processing"):
        await api(http, tokens).playback(camera_device, "event-1")


async def test_media_info_is_cached_between_plays(http, tokens, camera_device):
    recordings = api(http, tokens)
    http.request.side_effect = [
        json_response(event()),
        json_response(info(hls=False)),
        json_response(event()),
    ]
    await recordings.playback(camera_device, "event-1")
    await recordings.playback(camera_device, "event-1")
    assert http.request.await_count == 3


@pytest.mark.parametrize(
    "url,authenticated",
    [
        ("https://h1.hubs.tend-us.tendplatform.com/media/segment.ts", True),
        (VIDEO + EVENTS + "/event-1/media/segment.ts", True),
        ("https://events-media.prod.video.myq.com" + EVENTS + "/event-1/media/a.ts", True),
        ("https://synthetic.cloudfront.net/segment.ts", False),
    ],
)
async def test_credentials_go_only_to_myq_and_tend_media_hosts(http, tokens, url, authenticated):
    http.request.return_value = Response(200, {}, b"segment")
    playback = Playback(Client(transport=http, tokens=tokens), "event-1", url)
    await playback.fetch("0")
    headers = http.request.await_args.kwargs["headers"]
    assert ("Authorization" in headers) is authenticated
    assert ("AntiCSRF" in headers) is authenticated


def test_unconfirmed_video_myq_hosts_are_refused():
    with pytest.raises(ProtocolError):
        rec.validate_media_url("https://api.video.myq.com/anything", "event-1")


def test_unusable_rendition_is_dropped_but_keys_fail_closed(http, tokens):
    base = VIDEO + EVENTS + "/event-1/media/video.m3u8"
    playback = Playback(Client(transport=http, tokens=tokens), "event-1", base)
    manifest = (
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttps://evil.invalid/a.m3u8\n"
        b'#EXT-X-MEDIA:TYPE=AUDIO,URI="https://evil.invalid/audio.m3u8"\n'
        b"#EXT-X-STREAM-INF:BANDWIDTH=2\nvariant.m3u8\n"
    )
    result = playback.rewrite_manifest(manifest, base, lambda key: "/ha/" + key).decode()
    assert result == "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2\n/ha/1\n"
    with pytest.raises(ProtocolError):
        playback.rewrite_manifest(
            b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://evil.invalid/k"\nsegment.ts\n',
            base,
            lambda key: "/ha/" + key,
        )


async def test_grant_expiry_slides_within_an_absolute_bound(http, tokens, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rec.time, "monotonic", lambda: clock[0])
    http.request.return_value = Response(200, {}, b"x")
    playback = Playback(
        Client(transport=http, tokens=tokens), "event-1", VIDEO + EVENTS + "/event-1/media/a.ts"
    )
    for _ in range(20):
        clock[0] += rec.PLAYBACK_TTL - 1  # A long pause, then the player continues.
        if clock[0] >= 1000 + PLAYBACK_MAX:
            break
        await playback.fetch("0")
    clock[0] = 1000 + PLAYBACK_MAX + 1
    with pytest.raises(AccessDenied):
        await playback.fetch("0")


# --- Thumbnails ---------------------------------------------------------------------------


async def test_thumbnail_prefers_the_advertised_pattern(http, tokens, camera_device):
    http.request.side_effect = [json_response(info(thumbnail=True)), Response(200, {}, JPEG)]
    body, kind = await api(http, tokens).thumbnail(camera_device, "event-1")
    assert (body, kind) == (JPEG, "image/jpeg")
    assert http.request.await_args.args == ("GET", VIDEO + EVENTS + "/event-1/media/thumb.jpg")
    assert http.request.await_args.kwargs["headers"]["AntiCSRF"] == "AntiCSRF"


async def test_thumbnail_falls_back_to_the_event_route(http, tokens, camera_device):
    http.request.side_effect = [json_response(info()), Response(200, {}, b"\x89PNG\r\n\x1a\nimg")]
    _, kind = await api(http, tokens).thumbnail(camera_device, "event-1")
    assert kind == "image/png"
    assert http.request.await_args.args == (
        "GET",
        VIDEO + EVENTS + "/event-1/media/thumbnail?includeHidden=true",
    )


@pytest.mark.parametrize("body", [b"<html>", b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024)])
async def test_thumbnail_must_be_a_small_image(http, tokens, camera_device, body):
    http.request.side_effect = [json_response(info()), Response(200, {}, body)]
    with pytest.raises(ProtocolError):
        await api(http, tokens).thumbnail(camera_device, "event-1")


# --- Home Assistant media source ----------------------------------------------------------


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
    return source, entry, await hass_client()


def item(hass, identifier):
    return MediaSourceItem(hass, DOMAIN, identifier, None)


async def test_browse_has_door_folder_thumbnails_and_paging(hass, media_source, http):
    source, entry, client = media_source
    camera = f"{entry.entry_id}/test-account/TEST-CAM123"
    rows = [
        event(
            f"c-{i:02}",
            type="VIDEO",
            tags={"accessEventID.GD_CLOSE": "2026-11-20T11:59:00Z"},
        )
        for i in range(50)
    ]
    http.request.return_value = json_response({"items": rows, "moreAvailable": True})
    doors = await source.async_browse_media(item(hass, camera + "/doors"))
    assert doors.title == "Door events"
    assert http.request.await_args.kwargs["params"]["tag"] == "accessEventID*"
    assert doors.children[-1].title == "Older recordings"
    assert doors.children[-1].identifier.startswith(camera + "/doors/page/")
    clip = doors.children[0]
    assert clip.can_play and "Garage door closed" in clip.title
    http.request.reset_mock(return_value=True)
    http.request.side_effect = [json_response(info()), Response(200, {}, JPEG)]
    response = await client.get(clip.thumbnail)
    assert response.status == 200 and await response.read() == JPEG
    assert response.headers["Content-Type"] == "image/jpeg"


async def test_thumbnail_view_rejects_bad_refs_and_lost_access(hass, media_source, http):
    source, entry, client = media_source
    assert (await client.get("/api/myq/recording_thumbnails/!!")).status == 404
    http.request.return_value = json_response({"items": [event()], "moreAvailable": False})
    folder = await source.async_browse_media(
        item(hass, f"{entry.entry_id}/test-account/TEST-CAM123")
    )
    entry.runtime_data.data.account_access = {}
    assert (await client.get(folder.children[1].thumbnail)).status == 404


async def test_content_id_resolves_and_progressive_streams_with_ranges(
    hass, media_source, http, camera_device
):
    source, entry, client = media_source
    content_id = recording_content_id(entry.entry_id, camera_device, "event:1")
    assert content_id == (
        f"media-source://{DOMAIN}/{entry.entry_id}/test-account/TEST-CAM123/event/event%3A1"
    )
    http.request.side_effect = [
        json_response(event("event:1")),
        json_response(info(hls=False)),
        Response(
            206,
            {"Content-Type": "video/mp4", "Content-Range": "bytes 0-3/10", "Content-Length": "4"},
            b"mp4!",
        ),
    ]
    identifier = content_id.removeprefix(f"media-source://{DOMAIN}/")
    resolved = await source.async_resolve_media(item(hass, identifier))
    assert resolved.mime_type == MP4_TYPE
    response = await client.get(resolved.url, headers={"Range": "bytes=0-3"})
    assert response.status == 206 and await response.read() == b"mp4!"
    assert response.headers["Content-Range"] == "bytes 0-3/10"
    assert http.request.await_args.kwargs["headers"]["Range"] == "bytes=0-3"


async def test_segment_content_types_from_other_origins_are_accepted(hass, media_source, http):
    source, entry, client = media_source
    http.request.side_effect = [
        json_response(event()),
        json_response(info()),
        Response(200, {"Content-Type": HLS_TYPE}, b"#EXTM3U\n#EXTINF:1,\nseg.m4s\n"),
        Response(200, {"Content-Type": "video/iso.segment"}, b"fmp4"),
    ]
    resolved = await source.async_resolve_media(
        item(hass, f"{entry.entry_id}/test-account/TEST-CAM123/event/event-1")
    )
    playlist = await (await client.get(resolved.url)).text()
    segment = next(line for line in playlist.splitlines() if not line.startswith("#"))
    response = await client.get(segment)
    assert response.status == 200 and await response.read() == b"fmp4"


async def test_resolve_failure_reports_the_status_hint(hass, media_source, http):
    source, entry, _ = media_source
    http.request.side_effect = [
        json_response(event(status="ERROR_PROCESSING")),
        json_response(info(hls=False, cdn=False)),
    ]
    with pytest.raises(Unresolvable, match="failed to process"):
        await source.async_resolve_media(
            item(hass, f"{entry.entry_id}/test-account/TEST-CAM123/event/event-1")
        )


# --- Streaming over a real aiohttp session -------------------------------------------------


async def test_progressive_stream_relays_chunks_and_follows_cdn_redirect(
    tokens, monkeypatch, socket_enabled
):
    payload = b"0123456789" * 20000

    async def origin(request):
        assert request.headers["Authorization"] == "Bearer test-access"
        raise web.HTTPFound(f"http://{request.host}/cdn/video.mp4")

    async def cdn(request):
        assert "Authorization" not in request.headers
        response = web.StreamResponse(headers={"Content-Type": "video/mp4"})
        await response.prepare(request)
        for start in range(0, len(payload), 65536):
            await response.write(payload[start : start + 65536])
        return response

    async def headers(client, url, extra):
        # The loopback server stands in for both the Tend origin and an HTTPS CDN.
        result = dict(extra)
        if "/cdn/" not in url:
            result["Authorization"] = "Bearer " + await client.auth.access_token()
        return result

    monkeypatch.setattr(rec, "validate_media_url", lambda url, event_id: url)
    monkeypatch.setattr(rec, "_media_headers", headers)
    app = web.Application()
    app.router.add_get("/cxs/api/events/event-1/media/video.mp4", origin)
    app.router.add_get("/cdn/video.mp4", cdn)
    server = TestServer(app)
    await server.start_server()
    base = f"http://{server.host}:{server.port}"
    try:
        async with aiohttp.ClientSession() as session:
            client = Client(transport=Http(session), tokens=tokens)
            playback = Playback(
                client, "event-1", base + EVENTS + "/event-1/media/video.mp4", content_type=MP4_TYPE
            )
            stream = await playback.open_stream("0")
            body = b"".join([chunk async for chunk in stream.chunks])
            await stream.close()
        assert stream.status == 200 and body == payload
    finally:
        await server.close()


def test_status_values_match_the_app_enum():
    assert set(rec.STATUS_HINTS) <= {
        "OPEN",
        "PREFINALIZING",
        "PROCESSING",
        "FINALIZING",
        "ERROR_PROCESSING",
        "ERROR_FINALIZING",
        "DELETED",
    }
    assert rec.DOOR_TAG == "accessEventID*"
