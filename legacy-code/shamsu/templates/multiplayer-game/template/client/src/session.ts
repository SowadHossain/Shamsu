// App-wide singletons: one network client, one game loop, one telemetry object.
import { NetClient } from "./net/client";
import { GameLoop } from "./game/loop";
import { createTelemetry } from "./game/world";

export const net = new NetClient();
export const loop = new GameLoop(net);
export const telemetry = createTelemetry();

// Debug/test hook: lets the smoke suite drive score/end deterministically and
// inspect state from the page. Harmless in production.
if (typeof window !== "undefined") {
  (window as unknown as { __shamsu?: unknown }).__shamsu = { net, loop, telemetry };
}
