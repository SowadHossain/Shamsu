import { createState } from "./game/state";
import { createInput, readActions } from "./game/input";
import { update } from "./game/update";
import { render } from "./game/render";
import { renderHud } from "./ui/hud";

const canvas = document.getElementById("game") as HTMLCanvasElement;
const ctx = canvas.getContext("2d");
const hud = document.getElementById("hud") as HTMLElement;

if (ctx === null) {
  throw new Error("2D canvas context is not available");
}

const context = ctx;
const state = createState(canvas.width, canvas.height);
const input = createInput();

let last = performance.now();

function frame(now: number): void {
  const dt = Math.min((now - last) / 1000, 0.05);
  last = now;
  update(state, readActions(input), dt);
  render(context, state);
  renderHud(hud, state);
  requestAnimationFrame(frame);
}

requestAnimationFrame(frame);
