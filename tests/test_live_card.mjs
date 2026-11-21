/* Synthetic browser/WebSocket tests; no network, camera, or microphone is used. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../custom_components/myq/www/myq-live-card.js", import.meta.url), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function fixture() {
  class Element extends EventTarget {
    constructor() { super(); this.attrs = new Map(); this.children = new Map(); }
    attachShadow() { this.shadowRoot = new Element(); return this.shadowRoot; }
    querySelector(key) {
      if (!this.children.has(key)) this.children.set(key, new Element());
      return this.children.get(key);
    }
    setAttribute(key, value) { this.attrs.set(key, value); }
    append(...nodes) { (this.nodes ||= []).push(...nodes); }
    replaceChildren() { this.nodes = []; }
    play() { return Promise.resolve(); }
  }
  class Track { enabled = true; stopped = false; stop() { this.stopped = true; } }
  class Stream {
    constructor(tracks = []) { this.tracks = tracks; }
    getTracks() { return this.tracks; }
    getAudioTracks() { return this.tracks; }
    addTrack(track) { this.tracks.push(track); }
  }
  const peers = [], calls = [], tracks = [], registry = new Map();
  class Peer {
    constructor(configuration) { this.configuration = configuration; this.transceivers = []; peers.push(this); }
    addTransceiver(kind, options) { this.transceivers.push({ kind, ...options }); }
    addTrack(track) { this.input = track; }
    async createOffer() { return { type: "offer", sdp: "synthetic-offer" }; }
    async setLocalDescription(value) { this.localDescription = value; }
    async setRemoteDescription(value) { this.remoteDescription = value; }
    async addIceCandidate(value) { this.candidate = value; }
    close() { this.closed = true; }
  }
  const document = new Element(); document.hidden = false;
  document.createElement = () => new Element();
  const window = new Element();
  let microphoneRequests = 0, unsubscribed = 0, deliver;
  const navigator = { mediaDevices: { async getUserMedia() {
    microphoneRequests++;
    const track = new Track(); tracks.push(track); return new Stream([track]);
  } } };
  const connection = new EventTarget();
  connection.subscribeMessage = async (callback, message) => {
    calls.push(message); deliver = callback;
    callback({ type: "session", session_id: "synthetic-session" });
    callback({ type: "answer", answer: "synthetic-answer" });
    return () => { unsubscribed++; };
  };
  const hass = {
    connection,
    async callWS(value) { calls.push(value); return { configuration: { iceServers: [] } }; },
    async callService(domain, service, value) { calls.push({ domain, service, ...value }); },
  };
  const context = vm.createContext({
    HTMLElement: Element, MediaStream: Stream, RTCPeerConnection: Peer,
    customElements: {
      // Browsers throw when a name is defined twice.
      define: (name, value) => {
        if (registry.has(name)) throw new Error(`${name} is already defined`);
        registry.set(name, value);
      },
      get: (name) => registry.get(name),
    },
    window, document, navigator, setTimeout, clearTimeout, Promise,
  });
  const load = () => vm.runInContext(source, context);
  load();
  const card = new (registry.get("myq-live-card"))();
  card.setConfig({ entity: "camera.garage", microphone_on: "button.garage_talk", microphone_off: "button.garage_mute" });
  card.hass = hass;
  card.connectedCallback();
  return { card, hass, peers, calls, tracks, navigator, window, document, registry, load,
    get microphoneRequests() { return microphoneRequests; },
    get unsubscribed() { return unsubscribed; },
    deliver: (message) => deliver(message),
  };
}

test("view-only never asks for a microphone and unsubscribes on stop", async () => {
  const f = fixture();
  await f.card._start(false); await tick();
  assert.equal(f.microphoneRequests, 0);
  assert.deepEqual(f.peers[0].transceivers.map((x) => [x.kind, x.direction]),
    [["video", "recvonly"], ["audio", "recvonly"]]);
  assert.equal(f.card._current.ready, true);
  await f.card._stop(); await tick();
  assert.equal(f.unsubscribed, 1);
  assert.equal(f.peers[0].closed, true);
});

test("microphone is muted until backend permission; release and blur immediately mute", async () => {
  const f = fixture();
  await f.card._start(true); await tick();
  assert.equal(f.microphoneRequests, 1);
  assert.equal(f.tracks[0].enabled, false);
  await f.card._talk(true);
  assert.equal(f.tracks[0].enabled, true);
  assert.equal(f.calls.at(-1).entity_id, "button.garage_talk");
  f.window.dispatchEvent(new Event("blur"));
  assert.equal(f.tracks[0].enabled, false);
  await tick();
  assert.equal(f.calls.at(-1).entity_id, "button.garage_mute");
  f.card.disconnectedCallback(); await tick();
  assert.equal(f.tracks[0].stopped, true);
  assert.equal(f.unsubscribed, 1);
});

test("release during delayed permission never enables audio afterward", async () => {
  const f = fixture();
  await f.card._start(true); await tick();
  const pending = deferred();
  f.hass.callService = async (_, __, value) => {
    f.calls.push(value);
    if (value.entity_id === "button.garage_talk") await pending.promise;
  };
  const enabling = f.card._talk(true);
  await f.card._talk(false);
  pending.resolve(); await enabling;
  assert.equal(f.tracks[0].enabled, false);
  assert.equal(f.calls.at(-1).entity_id, "button.garage_mute");
  await f.card._stop();
});

test("denied backend permission cleans up peer, tracks and server subscription", async () => {
  const f = fixture();
  await f.card._start(true); await tick();
  f.hass.callService = async () => { throw new Error("denied"); };
  await f.card._talk(true); await tick();
  assert.equal(f.card._current, null);
  assert.equal(f.tracks[0].enabled, false);
  assert.equal(f.tracks[0].stopped, true);
  assert.equal(f.peers[0].closed, true);
  assert.equal(f.unsubscribed, 1);
});

test("stop during browser permission prompt closes tracks when permission resolves", async () => {
  const f = fixture(); const pending = deferred();
  f.navigator.mediaDevices.getUserMedia = () => pending.promise;
  const starting = f.card._start(true);
  await f.card._stop();
  const track = { enabled: true, stopped: false, stop() { this.stopped = true; } };
  pending.resolve({ getTracks: () => [track] }); await starting;
  assert.equal(track.enabled, false);
  assert.equal(track.stopped, true);
  assert.equal(f.peers.length, 0);
});

test("stop during subscription setup releases late subscription", async () => {
  const f = fixture(); const pending = deferred(); let unsubscribed = 0;
  f.hass.connection.subscribeMessage = () => pending.promise;
  const starting = f.card._start(true); await tick();
  await f.card._stop();
  pending.resolve(() => { unsubscribed++; }); await starting;
  assert.equal(unsubscribed, 1);
  assert.equal(f.tracks[0].stopped, true);
});

test("signaling disconnect ends media and clears microphone tracks", async () => {
  const f = fixture();
  await f.card._start(true); await tick();
  await f.card._talk(true);
  f.hass.connection.dispatchEvent(new Event("disconnected")); await tick();
  assert.equal(f.tracks[0].enabled, false);
  assert.equal(f.tracks[0].stopped, true);
  assert.equal(f.peers[0].closed, true);
  assert.equal(f.unsubscribed, 1);
});

test("a brief WebRTC disconnect recovers without ending the view", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fixture();
  await f.card._start(false); await tick();
  const peer = f.peers[0];
  peer.connectionState = "disconnected"; peer.onconnectionstatechange();
  t.mock.timers.tick(4000);
  peer.connectionState = "connected"; peer.onconnectionstatechange();
  t.mock.timers.tick(10000);
  assert.notEqual(f.card._current, null);
  assert.equal(peer.closed, undefined);
  await f.card._stop();
});

test("a WebRTC disconnect that lasts past the grace period ends the view", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fixture();
  await f.card._start(false); await tick();
  const peer = f.peers[0];
  peer.connectionState = "disconnected"; peer.onconnectionstatechange();
  t.mock.timers.tick(4999);
  assert.notEqual(f.card._current, null);
  t.mock.timers.tick(1); await tick();
  assert.equal(f.card._current, null);
  assert.equal(peer.closed, true);
  assert.equal(f.unsubscribed, 1);
});

test("a failed WebRTC connection ends the view immediately", async () => {
  const f = fixture();
  await f.card._start(false); await tick();
  f.peers[0].connectionState = "failed"; f.peers[0].onconnectionstatechange(); await tick();
  assert.equal(f.card._current, null);
  assert.equal(f.peers[0].closed, true);
});

test("editor keeps its pickers when Home Assistant pushes new state", () => {
  const f = fixture();
  const editor = new (f.registry.get("myq-live-card-editor"))();
  editor.setConfig({ entity: "camera.garage" });
  editor.hass = f.hass;
  const pickers = [...editor.shadowRoot.nodes];
  assert.equal(pickers.length, 3);
  const update = { ...f.hass };
  editor.hass = update;
  assert.deepEqual(editor.shadowRoot.nodes, pickers);
  assert.ok(pickers.every((picker) => picker.hass === update));
  editor.setConfig({ entity: "camera.other", microphone_on: "button.talk" });
  assert.deepEqual(editor.shadowRoot.nodes, pickers);
  assert.deepEqual(pickers.map((picker) => picker.value), ["camera.other", "button.talk", ""]);
});

test("loading the module a second time neither throws nor lists the card twice", () => {
  const f = fixture();
  f.load();
  assert.equal(f.window.customCards.length, 1);
});
