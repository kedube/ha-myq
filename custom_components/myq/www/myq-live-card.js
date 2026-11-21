/* MyQ live view: HA-authenticated signaling; microphone consent stays in this browser. */
// Block scope: a second load (module or classic script) must not redeclare classes.
{
  class MyQLiveCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `
        <style>
          ha-card { padding: 16px; } video { width: 100%; background: #111; border-radius: 8px; }
          .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
          button { font: inherit; padding: 10px 14px; border-radius: 6px; cursor: pointer; }
          button:disabled { cursor: default; opacity: .5; } .talk { touch-action: none; }
          .talk[aria-pressed="true"] { background: #b71c1c; color: white; }
          p { color: var(--secondary-text-color); margin-bottom: 0; }
        </style>
        <ha-card header="MyQ live view">
          <video autoplay playsinline controls aria-label="MyQ camera live view"></video>
          <div class="actions">
            <button class="start">Start view</button>
            <button class="microphone">Start with microphone</button>
            <button class="talk" aria-pressed="false" disabled>Hold to talk</button>
            <button class="stop" disabled>Stop view</button>
          </div>
          <p role="status" aria-live="polite">Ready. Microphone is off.</p>
        </ha-card>`;
      this._video = this.shadowRoot.querySelector("video");
      this._status = this.shadowRoot.querySelector("p");
      this._startButton = this.shadowRoot.querySelector(".start");
      this._micButton = this.shadowRoot.querySelector(".microphone");
      this._talkButton = this.shadowRoot.querySelector(".talk");
      this._stopButton = this.shadowRoot.querySelector(".stop");
      this._startButton.onclick = () => this._start(false);
      this._micButton.onclick = () => this._start(true);
      this._stopButton.onclick = () => this._stop();
      this._talkButton.onpointerdown = (event) => {
        event.preventDefault();
        if (event.button !== 0) return;
        this._talkButton.setPointerCapture?.(event.pointerId);
        this._talk(true);
      };
      for (const name of ["pointerup", "pointercancel", "lostpointercapture"])
        this._talkButton.addEventListener(name, () => this._talk(false));
      this._talkButton.onkeydown = (event) => {
        if ([" ", "Enter"].includes(event.key)) {
          event.preventDefault();
          if (!event.repeat) this._talk(true);
        }
      };
      this._talkButton.onkeyup = (event) => {
        if ([" ", "Enter"].includes(event.key)) {
          event.preventDefault();
          this._talk(false);
        }
      };
      this._onBlur = () => this._talk(false);
      this._onVisibility = () => { if (document.hidden) this._talk(false); };
      this._generation = 0;
      this._current = null;
    }

    set hass(value) { this._hass = value; }
    setConfig(config) {
      if (!config || !/^camera\.[a-z0-9_]+$/.test(config.entity || ""))
        throw new Error("Select a MyQ camera entity");
      for (const key of ["microphone_on", "microphone_off"])
        if (config[key] && !/^button\.[a-z0-9_]+$/.test(config[key]))
          throw new Error("Microphone controls must be button entities");
      if (this._current) this._stop();
      this._config = { ...config };
      this._buttons();
    }
    getCardSize() { return 5; }
    static getConfigElement() { return document.createElement("myq-live-card-editor"); }
    static getStubConfig() { return { entity: "" }; }
    connectedCallback() {
      window.addEventListener("blur", this._onBlur);
      document.addEventListener("visibilitychange", this._onVisibility);
    }
    disconnectedCallback() {
      window.removeEventListener("blur", this._onBlur);
      document.removeEventListener("visibilitychange", this._onVisibility);
      this._stop();
    }
    _buttons() {
      const current = this._current;
      this._startButton.disabled = !!current;
      this._micButton.disabled = !!current || !this._config?.microphone_on || !this._config?.microphone_off;
      this._stopButton.disabled = !current;
      this._talkButton.disabled = !current?.ready || !current?.microphone;
      this._talkButton.setAttribute("aria-pressed", current?.pressed ? "true" : "false");
    }

    async _start(withMicrophone) {
      if (this._current || !this._config || !this._hass) return;
      if (withMicrophone && (!this._config.microphone_on || !this._config.microphone_off)) return;
      const current = {
        generation: ++this._generation, config: { ...this._config }, hass: this._hass,
        microphone: null, peer: null, unsubscribe: null, session: null,
        candidates: [], remoteCandidates: [], ready: false, pressed: false,
        messages: Promise.resolve(),
      };
      this._current = current;
      current.startTimer = setTimeout(() => this._failure(current), 45000);
      current.disconnected = () => this._failure(current);
      current.hass.connection.addEventListener?.("disconnected", current.disconnected);
      this._buttons();
      this._status.textContent = "Connecting. Microphone is off.";
      const active = () => this._current === current;
      try {
        // Browser permission is requested only by this explicit user action.
        if (withMicrophone) {
          const microphone = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
          for (const track of microphone.getTracks()) track.enabled = false;
          if (!active()) { microphone.getTracks().forEach((track) => track.stop()); return; }
          current.microphone = microphone;
        }
        const response = await current.hass.callWS({
          type: "camera/webrtc/get_client_config", entity_id: current.config.entity,
        });
        if (!active()) return;
        const peer = new RTCPeerConnection(response.configuration || {});
        current.peer = peer;
        peer.addTransceiver("video", { direction: "recvonly" });
        if (current.microphone) {
          for (const track of current.microphone.getAudioTracks()) peer.addTrack(track, current.microphone);
        } else peer.addTransceiver("audio", { direction: "recvonly" });
        const output = new MediaStream();
        this._video.srcObject = output;
        peer.ontrack = (event) => {
          if (active()) output.addTrack(event.track);
          else event.track.stop();
        };
        peer.onicecandidate = (event) => {
          if (!active() || !event.candidate) return;
          const candidate = event.candidate.toJSON();
          if (current.session) this._candidate(current, candidate);
          else if (current.candidates.length < 128) current.candidates.push(candidate);
          else this._failure(current);
        };
        peer.onconnectionstatechange = () => {
          if (!active()) return;
          clearTimeout(current.graceTimer);
          if (["failed", "closed"].includes(peer.connectionState)) this._failure(current);
          // "disconnected" often recovers by itself (Wi-Fi roam, brief loss); allow 5 s.
          else if (peer.connectionState === "disconnected")
            current.graceTimer = setTimeout(() => {
              if (active() && peer.connectionState !== "connected") this._failure(current);
            }, 5000);
        };
        await peer.setLocalDescription(await peer.createOffer());
        if (!active()) return;
        const unsubscribe = await current.hass.connection.subscribeMessage((message) => {
          current.messages = current.messages.then(async () => {
            if (!active()) return;
            if (message.type === "session") {
              current.session = message.session_id;
              for (const candidate of current.candidates.splice(0))
                await this._candidate(current, candidate);
            } else if (message.type === "answer") {
              await peer.setRemoteDescription({ type: "answer", sdp: message.answer });
              if (!active()) return;
              for (const candidate of current.remoteCandidates.splice(0)) await peer.addIceCandidate(candidate);
              current.ready = true;
              clearTimeout(current.startTimer);
              current.viewTimer = setTimeout(() => this._stop(), 600000);
              this._status.textContent = current.microphone
                ? "Live. Hold to talk; release to mute. Microphone is off."
                : "Live. Microphone is off.";
              this._buttons();
              this._video.play().catch(() => {});
            } else if (message.type === "candidate") {
              if (peer.remoteDescription) await peer.addIceCandidate(message.candidate);
              else if (current.remoteCandidates.length < 128) current.remoteCandidates.push(message.candidate);
              else throw new Error("Too many candidates");
            } else if (message.type === "error") throw new Error("Camera session failed");
          }).catch(() => this._failure(current));
        }, { type: "camera/webrtc/offer", entity_id: current.config.entity, offer: peer.localDescription.sdp });
        // A stop while subscribing must still release the server-side session.
        if (!active()) { await unsubscribe(); return; }
        current.unsubscribe = unsubscribe;
      } catch (_) { this._failure(current); }
    }

    async _candidate(current, candidate) {
      if (this._current !== current) return;
      try {
        await current.hass.callWS({ type: "camera/webrtc/candidate", entity_id: current.config.entity,
          session_id: current.session, candidate });
      } catch (_) { this._failure(current); }
    }

    async _press(current, enabled) {
      await current.hass.callService("button", "press", {
        entity_id: enabled ? current.config.microphone_on : current.config.microphone_off,
      });
    }

    async _talk(enabled) {
      const current = this._current;
      if (!current?.microphone || !current.ready) return;
      if (!enabled) {
        const wasPressed = current.pressed;
        current.pressed = false;
        clearTimeout(current.talkTimer);
        current.microphone.getTracks().forEach((track) => { track.enabled = false; });
        this._buttons();
        this._status.textContent = "Live. Microphone is off.";
        if (wasPressed) {
          try { await this._press(current, false); } catch (_) { this._failure(current); }
        }
        return;
      }
      if (current.pressed || document.hidden) return;
      current.pressed = true;
      this._buttons();
      try {
        await this._press(current, true);
        if (this._current !== current || !current.pressed) {
          // Release may win the permission check; never enable the browser track afterward.
          await this._press(current, false);
          return;
        }
        current.microphone.getTracks().forEach((track) => { track.enabled = true; });
        current.talkTimer = setTimeout(() => this._talk(false), 30000);
        this._status.textContent = "Microphone is on. Release to mute.";
      } catch (_) { this._failure(current); }
    }

    _failure(current) {
      if (this._current !== current) return;
      this._stop();
      this._status.textContent = "Could not continue live view. Check the camera and your permissions.";
    }

    async _stop() {
      const current = this._current;
      this._current = null;
      this._generation++;
      if (current) {
        const wasPressed = current.pressed;
        current.pressed = false;
        current.hass.connection.removeEventListener?.("disconnected", current.disconnected);
        for (const timer of [current.startTimer, current.talkTimer, current.viewTimer, current.graceTimer])
          clearTimeout(timer);
        current.microphone?.getTracks().forEach((track) => { track.enabled = false; track.stop(); });
        current.peer?.close();
        if (wasPressed) this._press(current, false).catch(() => {});
        // HA closes the native camera session when this offer subscription is removed.
        if (current.unsubscribe) Promise.resolve().then(current.unsubscribe).catch(() => {});
      }
      this._video.srcObject = null;
      this._buttons();
      this._status.textContent = "View stopped. Microphone is off.";
    }
  }

  class MyQLiveCardEditor extends HTMLElement {
    constructor() { super(); this.attachShadow({ mode: "open" }); this._selectors = null; }
    // HA assigns hass on every state change; rebuilding would close an open picker.
    set hass(value) {
      this._hass = value;
      if (this._selectors) for (const selector of this._selectors.values()) selector.hass = value;
      else this._render();
    }
    setConfig(value) {
      this._config = { ...value };
      if (this._selectors)
        for (const [key, selector] of this._selectors) selector.value = this._config[key] || "";
      else this._render();
    }
    _render() {
      if (!this._config || !this._hass) return;
      this._selectors = new Map();
      this.shadowRoot.replaceChildren();
      for (const [key, domain, label] of [
        ["entity", "camera", "Camera"],
        ["microphone_on", "button", "Enable microphone button"],
        ["microphone_off", "button", "Mute microphone button"],
      ]) {
        const selector = document.createElement("ha-selector");
        selector.hass = this._hass;
        selector.selector = { entity: { domain, integration: "myq" } };
        selector.label = label;
        selector.value = this._config[key] || "";
        selector.addEventListener("value-changed", (event) => {
          this._config = { ...this._config, [key]: event.detail.value };
          this.dispatchEvent(new CustomEvent("config-changed", {
            detail: { config: this._config }, bubbles: true, composed: true,
          }));
        });
        this._selectors.set(key, selector);
        this.shadowRoot.append(selector);
      }
    }
  }

  // The integration auto-loads this module; a manual dashboard resource may load it again.
  if (!customElements.get("myq-live-card")) {
    customElements.define("myq-live-card", MyQLiveCard);
    customElements.define("myq-live-card-editor", MyQLiveCardEditor);
    window.customCards = window.customCards || [];
    window.customCards.push({ type: "myq-live-card", name: "MyQ live view", preview: false,
      description: "Live camera view with explicit press-to-talk microphone controls." });
  }
}
