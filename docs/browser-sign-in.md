# Retired browser sign-in helper

Browser sign-in with app verification is no longer offered in Home Assistant.
**Connect MyQ** now opens directly to email and password; see the current
[sign-in guide](sign-in.md).

The standalone `scripts/browser_handoff.py` and `api/handoff.py` remain as
research utilities for the former flow. Home Assistant no longer creates their
public requests or accepts their private result files. They are not a supported
setup or reconnect path.

The former design kept the OAuth verifier in Home Assistant while a browser
helper returned a callback and externally supplied App Check proof. It never
demonstrated an accepted native token exchange and could not generate the proof.
Historical findings remain in the [sign-in investigation](research/sign-in-investigation.md).
Current browser experiments use the standalone [account probe](research/probe.md).
