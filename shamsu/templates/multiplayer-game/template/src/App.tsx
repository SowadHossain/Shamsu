import { Canvas } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import { createInitialState, createInputState, updateGameState } from "./game/rules";
import type { GameState, InputState, Player } from "./game/entities";
import { RoomClient } from "./net/room";
import { Hud } from "./ui/Hud";

function PlayerCube({ player, testId }: { player: Player; testId: string }) {
  return (
    <mesh position={player.position} data-testid={testId}>
      <boxGeometry args={[0.8, 0.8, 0.8]} />
      <meshStandardMaterial color={player.color} />
    </mesh>
  );
}

function GameScene({ state }: { state: GameState }) {
  const local = state.players.find((player) => player.id === state.localPlayerId);
  const remotes = state.players.filter((player) => player.id !== state.localPlayerId);

  return (
    <div className="viewport" data-testid="game-scene">
      <Canvas camera={{ position: [0, 6, 8], fov: 50 }}>
        <ambientLight intensity={0.7} />
        <directionalLight position={[3, 5, 4]} intensity={1.2} />
        {local ? <PlayerCube player={local} testId="local-player" /> : null}
        {remotes.map((player) => (
          <PlayerCube key={player.id} player={player} testId="remote-player" />
        ))}
        {state.obstacles.map((obstacle) => (
          <mesh key={obstacle.id} position={obstacle.position}>
            <sphereGeometry args={[obstacle.radius, 16, 16]} />
            <meshStandardMaterial color="#a78bfa" />
          </mesh>
        ))}
      </Canvas>
    </div>
  );
}

export default function App() {
  const room = useMemo(() => new RoomClient(), []);
  const [state, setState] = useState<GameState>(() => createInitialState());
  const input = useRef<InputState>(createInputState());

  useEffect(() => {
    room.connect("default");
    return room.onEvent((event) => {
      if (event.type === "state") {
        setState(event.state);
      }
    });
  }, [room]);

  useEffect(() => {
    const keyMap: Record<string, keyof InputState> = {
      ArrowLeft: "left",
      ArrowRight: "right",
      ArrowUp: "forward",
      ArrowDown: "back",
      a: "left",
      d: "right",
      w: "forward",
      s: "back",
    };
    const down = (event: KeyboardEvent) => {
      const key = keyMap[event.key];
      if (key) input.current[key] = true;
    };
    const up = (event: KeyboardEvent) => {
      const key = keyMap[event.key];
      if (key) input.current[key] = false;
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, []);

  useEffect(() => {
    let previous = performance.now();
    let frame = 0;
    const tick = (now: number) => {
      const dt = Math.min(0.05, (now - previous) / 1000);
      previous = now;
      setState((current) => {
        const next = updateGameState(current, input.current, dt);
        room.publish(next);
        return next;
      });
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [room]);

  if (state.phase === "menu") {
    return (
      <main className="panel" data-testid="main-menu">
        <h1>Multiplayer Game</h1>
        <p>Host or join a local relay room, then start the match.</p>
        <button onClick={() => setState((current) => ({ ...current, phase: "lobby" }))}>Start</button>
        <button>Join Room</button>
        <button>Settings</button>
      </main>
    );
  }

  return (
    <main className="shell">
      <header className="topbar">
        <strong>SHAMSU Multiplayer Game</strong>
        <button onClick={() => setState((current) => ({ ...current, phase: "playing" }))}>Start Game</button>
      </header>
      <section className="game">
        <GameScene state={state} />
        <Hud state={state} />
        <section className="sidebar" data-testid="player-list">
          <h2>Players</h2>
          {state.players.map((player) => (
            <div className="player-row" key={player.id}>
              <span>
                <span className="swatch" style={{ background: player.color }} /> {player.name}
              </span>
              <span>{player.ready ? "ready" : "waiting"}</span>
            </div>
          ))}
        </section>
      </section>
    </main>
  );
}
