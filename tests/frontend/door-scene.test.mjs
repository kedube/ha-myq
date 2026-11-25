import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { importFrontend, installPage } from "./support.mjs";

installPage();
// Animation frames run only when a test says how much time has passed.
const frames = new Map();
let nextFrame = 0;
globalThis.requestAnimationFrame = (callback) => {
  frames.set(++nextFrame, callback);
  return nextFrame;
};
globalThis.cancelAnimationFrame = (id) => frames.delete(id);
const { DoorScene } = await importFrontend("door-scene.js");

/** Run the pending frame as if `ms` had passed since the animation began. */
function elapse(ms) {
  const now = performance.now() + ms;
  const due = [...frames.values()];
  frames.clear();
  for (const callback of due) callback(now);
}

const near = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-3, `${actual} ≈ ${expected}`);

beforeEach(() => frames.clear());

test("a door first shown stopped is half open", () => {
  const scene = new DoorScene();
  scene.setState("stopped");
  near(scene.position, 0.5);
  assert.equal(scene.el.dataset.state, "stopped");
});

test("a door that stops while it is drawn moving stays where it is", () => {
  const scene = new DoorScene();
  scene.setState("closed");
  scene.setState("opening");
  elapse(0.88 * 12000 * 0.5); // Halfway through the drawn travel.
  near(scene.position, 0.44);
  scene.setState("stopped");
  near(scene.position, 0.44);
  assert.equal(frames.size, 0, "no further movement");
});

test("a stopped door is kept clearly between open and closed", () => {
  const scene = new DoorScene();
  scene.setState("closed");
  scene.setState("opening");
  elapse(20000); // The drawing waits nearly open for MyQ to report the door open.
  near(scene.position, 0.88);
  scene.setState("stopped");
  elapse(450);
  near(scene.position, 0.75);

  scene.setState("closed");
  elapse(2600);
  near(scene.position, 0);
  scene.setState("stopped");
  elapse(650);
  near(scene.position, 0.25);
});
