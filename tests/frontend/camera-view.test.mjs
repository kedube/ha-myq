import assert from "node:assert/strict";
import { test } from "node:test";
import { decoded, importFrontend, installClock, installPage, setOnScreen } from "./support.mjs";

installPage();
customElements.define("ha-camera-stream", class extends HTMLElement {});
const clock = installClock();
const { CameraView } = await importFrontend("camera-view.js");
const { loadTranslations } = await importFrontend("i18n.js");
await loadTranslations("en");

let token = 0;
const hass = (state) => ({
  states: {
    "camera.garage": {
      entity_id: "camera.garage",
      state,
      attributes:
        state === "unavailable"
          ? {}
          : { supported_features: 2, entity_picture: `/api/camera_proxy/camera.garage?token=t${++token}` },
    },
  },
});

test("snapshots resume after live video, an outage, and recovery", async () => {
  const host = document.createElement("div");
  document.body.append(host);
  const camera = new CameraView(host);
  host.append(camera.el);
  camera.setEntity("camera.garage");
  camera.update(hass("idle"));
  camera.activate();
  await clock.advance(0);
  assert.equal(decoded.length, 1, "the first snapshot loads when the camera is shown");

  await camera.toggleLive();
  assert.ok(camera.live);
  await clock.advance(5000);
  camera.update(hass("unavailable"));
  assert.ok(!camera.live, "live video stops when the camera goes away");
  await clock.advance(5000);
  camera.update(hass("idle"));
  const before = decoded.length;
  await clock.advance(61000);
  assert.equal(decoded.length - before, 2, "two snapshots in the next 61 seconds");
  camera.destroy();
});

test("HA snapshot fallback exits live mode and resumes polling until an explicit retry", async (test) => {
  const host = document.createElement("div");
  document.body.append(host);
  const camera = new CameraView(host);
  host.append(camera.el);
  test.after(() => camera.destroy());
  camera.setEntity("camera.garage");
  camera.update(hass("idle"));
  camera.activate();
  await clock.advance(0);
  await camera.toggleLive();
  const player = camera.el.querySelector("ha-camera-stream");

  for (const type of [undefined, "web_rtc", "hls"]) {
    player.dispatchEvent(new CustomEvent("stream-type-changed", { detail: { type } }));
    assert.ok(camera.live, "setup and real video streams keep the live player");
  }
  player.dispatchEvent(new CustomEvent("stream-type-changed", { detail: { type: "mjpeg" } }));
  assert.ok(!camera.live, "HA's MJPEG fallback is not labeled as live video");
  assert.equal(camera.el.querySelector("ha-camera-stream"), null);
  assert.equal(camera.liveButton.getAttribute("aria-pressed"), "false");
  assert.equal(camera.el.querySelector(".camera-message").textContent, "Live video failed. Showing snapshots.");
  const before = decoded.length;
  await clock.advance(61000);
  assert.equal(decoded.length - before, 2, "snapshot polling resumes after the failure");
  assert.equal(camera.el.querySelector("ha-camera-stream"), null, "polling does not retry live video");

  await camera.toggleLive();
  const retryPlayer = camera.el.querySelector("ha-camera-stream");
  assert.notEqual(retryPlayer, player);
  assert.equal(camera.el.querySelector(".camera-message").textContent, "");
  player.dispatchEvent(new CustomEvent("stream-type-changed", { detail: { type: "mjpeg" } }));
  assert.ok(camera.live, "a late event from the previous player cannot stop a retry");

  setOnScreen(camera.el, false);
  retryPlayer.dispatchEvent(new CustomEvent("stream-type-changed", { detail: { type: "mjpeg" } }));
  const offscreen = decoded.length;
  await clock.advance(31000);
  assert.equal(decoded.length, offscreen, "fallback respects offscreen snapshot suspension");
  setOnScreen(camera.el, true);
  await clock.advance(0);
  assert.equal(decoded.length, offscreen + 1, "snapshots resume when the camera becomes visible");
});

test("closing or switching cameras cancels a pending live-player load", async (test) => {
  const host = document.createElement("div");
  const camera = new CameraView(host);
  test.after(() => camera.destroy());
  camera.setEntity("camera.garage");
  camera.update(hass("idle"));
  camera.activate();

  const closing = camera.toggleLive();
  camera.deactivate();
  camera.activate();
  await closing;
  assert.ok(!camera.live, "reopening does not revive the previous live request");

  const switching = camera.toggleLive();
  camera.setEntity("camera.other");
  camera.setEntity("camera.garage");
  camera.update(hass("idle"));
  await switching;
  assert.ok(!camera.live, "returning to the same entity does not revive its previous live request");
  assert.equal(camera.el.querySelector("ha-camera-stream"), null);
});
