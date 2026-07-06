import { useEffect, useRef, useState } from "react";
import { net, loop, telemetry } from "../session";
import { GameCanvas } from "../game/world";
import { NetSnapshot } from "../net/client";
import { useStore } from "../store";
import { Hud } from "./Hud";
import { Pause } from "./Pause";

export function GameScreen() {
  const [ready, setReady] = useState(false);
  const [snapshot, setSnapshot] = useState<NetSnapshot | null>(net.snapshot());
  const paused = useStore((state) => state.paused);
  const setPaused = useStore((state) => state.setPaused);

  // Start the physics + game loop for this match.
  useEffect(() => {
    let alive = true;
    const winScore = net.snapshot()?.winScore ?? 10;
    loop.init(net.sessionId, winScore).then(() => {
      if (alive) setReady(true);
    });
    return () => {
      alive = false;
      loop.dispose();
    };
  }, []);

  // Refresh the DOM overlays (HUD, player list) a few times per second.
  useEffect(() => {
    const interval = setInterval(() => setSnapshot(net.snapshot()), 120);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPaused(!useStore.getState().paused);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      clearInterval(interval);
      window.removeEventListener("keydown", onKey);
    };
  }, [setPaused]);

  const players = snapshot?.players ?? [];

  return (
    <div className="game-screen" data-testid="game-scene">
      {ready && <GameCanvas net={net} loop={loop} telemetry={telemetry} />}
      <Hud snapshot={snapshot} />
      <section className="ingame-players" data-testid="player-list">
        <h3>Players</h3>
        {players.map((player) => (
          <div className="player-entry" data-testid="player-entry" key={player.id}>
            <span>{player.name}</span>
            <span data-testid="player-score">{player.score}</span>
          </div>
        ))}
      </section>
      <NetDebug />
      <button className="pause-btn" onClick={() => setPaused(true)}>
        Pause
      </button>
      {paused && <Pause />}
    </div>
  );
}

// Hidden element that mirrors the live loop/net readout into DOM data
// attributes, so headless tests can observe that the loop is advancing.
function NetDebug() {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      if (ref.current) {
        ref.current.dataset.tick = String(telemetry.tick);
        ref.current.dataset.x = String(telemetry.x);
        ref.current.dataset.z = String(telemetry.z);
        ref.current.dataset.players = String(telemetry.players);
        ref.current.dataset.phase = telemetry.phase;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return <div ref={ref} data-testid="net-debug" style={{ display: "none" }} />;
}
