# Recordings

How MyQ's recorded camera events are listed and played: event queries and
ownership, pagination, the advertised HLS and MP4 resources and their lifetimes,
and how the integration serves them through Home Assistant. Using recordings is
described in the [camera guide](../cameras.md#recordings). Nothing here has been
checked against a live account.

## Protocol evidence

| Behavior | Native source | Implemented behavior |
| --- | --- | --- |
| Event discovery | `h61/i.java`, `y51/y.java` (query builder), `m61/l.java` | `GET /cxs/api/events` with `myqAccountId`, `srcId`, `v=2`, `limit`, `hideUnplayable=true`, `includeHidden=true`, and `endDttm` for later pages. `m61/l.java:293` sends `includeHidden=true` for door-linked clips. Hidden clips may otherwise be left out. |
| Door-linked clips | `m61/l.java:293`, `tu/j.java:355-385` | The **Door events** folder adds `tag=accessEventID*`. It lists `VIDEO` events that carry `accessEventID.GD_OPEN` or `accessEventID.GD_CLOSE`, the timestamp of the door event they belong to. `Recordings.door_clips()` reads one window with `startDttm` and `endDttm`, for matching clips to door history. |
| Event fields | `j61/TendMotionEvent.java`, `j61/TendEventTags.java` | Reads `id`, `srcId`, `dttm`, `type` (null reads as ""), `status`, `duration` and `tags`. Rows lacking a usable `id` or `dttm`, belonging to another camera or account, duplicated, or newer than the page boundary are skipped, as `EventsApiResponse` drops rows it cannot place. One bad row no longer hides the folder. |
| Listed events | `bf1/a.java` (`FilteredEventsView`) | Events reporting a duration of zero are not listed. |
| Labels | `x71/u.java`, `EventsMasterService.calcEventTagsGarageDoor`, `f61/l.java` | These tags become labels: `delivery`, `grg.door.<source>.open\|close`, `accessEventID.GD_OPEN/GD_CLOSE`, `peopleCount`, `movingVehicleCount`, `carCount`, `movingPackageCount`, `animalCount`, `dogCount`/`catCount`, `objectCount` and `autoSecureEventId`. |
| Playability | `l71/f.java`, `a81/d.java:462-480`, `l71/a.java:119-143` | No status gate; every listed event can be played, as in the app. The status (`y51/r.java`) only explains a failure, for example "This recording is still processing". |
| Media resources | `Events.retrieveMediaDownloadInfo`, `n71/h.java:108`, `a81/d.java:306-335` | Reads `/cxs/api/events/mediaDownloadInfo` (cached for ten minutes). Uses `video.m3u8.urlPattern`. If HLS is not advertised or its playlist fails to load, uses `videoCdn.urlPattern` as progressive `video/mp4`, as `EventPlayerEngine.k()` does. The playlist fetched while resolving is reused for the player's first request. |
| Thumbnails | `bf1/a.java:119` (`thumbnailUrl`), `hj0/m.java:433-437`, `sl/u0.java:156` | Prefers `mediaDownloadInfo["thumbnail"].urlPattern`; otherwise `/cxs/api/events/{id}/media/thumbnail?includeHidden=true`. JPEG or PNG only, up to 2 MiB. |
| Playback headers | `m61/o.java:37-63` | The app sends `Authorization: Bearer …` and `AntiCSRF` with every media request. HA sends them to the MyQ event-media origin and Tend (`*.tendplatform.com`) hosts, never to CloudFront, S3 or other CDN hosts. `partnerId`, locale and correlation headers are omitted. |
| Event host migration | `nt0/b.java`, `nt0/c.java` | Accepts `media.hubs.tend-us.tendplatform.com` and `events-media.prod.video.myq.com`. `api.video.myq.com` is the smart-search API, not a media host, and is refused. |
| Account grants | `rm0/AccountPermissions.java`, `cn0/a.java` | Requires explicit `devices/View` and `history/View` (or `All`) plus current device membership. This is a conservative integration policy. |

## Pagination and media lifetime

**Pages.** A page holds at most 50 events.

- **Boundary overlap.** The next request overlaps the oldest timestamp and suppresses IDs already returned at that timestamp, so same-time events are not skipped.
- **Page with nothing usable.** If a page has no displayable event but more are available, the next request starts one millisecond before its oldest dated row.
- **Missing `moreAvailable`.** A null value falls back to the row count.
- **Too many ties.** More than 500 IDs at one timestamp still causes a visible error.

**Playback grants.** Each playback holds an in-memory grant.

- **Expiry.** The grant expires after ten idle minutes. Every request extends it, up to two hours in total, so a paused player can resume.
- **Limits.** One grant can cover at most 512 media resources, and at most 32 grants are kept.
- **Revalidation.** Every request rechecks account grants, camera membership, camera options, entry load state and coordinator identity.

**The proxy.** HA signs each proxy path for at most the grant's lifetime, and the media player never sees a MyQ token, cloud URL or recording identifier.

- **Playlists.** The proxy rewrites HLS variants, segments, encryption keys and initialization maps.
- **Unusable entries.** A variant, segment or alternate rendition with an unusable address is dropped along with its own tags. An unusable key or initialization map still fails the playlist, and so does a playlist where nothing usable remains.
- **Segment types.** Accepted segment types include MPEG-TS, MP4 and fMP4 (`video/iso.segment`, `application/mp4`), audio, octet streams, and keys labelled `text/plain`.
- **Byte ranges.** The proxy supports single HTTP byte ranges.
- **Redirects.** Validated redirects are followed by hand, up to four requests. Credentials are recomputed for every destination.

**Size limits.** HLS resources use the shared transport and its 8 MiB response ceiling; playlists are limited to 1 MiB. Progressive MP4 is streamed in chunks without buffering, passes byte ranges through, and stops after 1 GiB. Download, deletion, favorites, sharing, feedback and other recording writes are not exposed.

## Open questions

Real-account playback remains unverified. It needs an account with cloud recordings, to establish:

- service entitlement
- the actual URL patterns and CDN hosts
- whether door clips are hidden
- pagination semantics and native event statuses
- player compatibility
