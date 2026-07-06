import { useEffect, useState } from "react";
import { useStore } from "../store";
import { net } from "../session";

export function Lobby() {
  const settings = useStore((state) => state.settings);
  const setScreen = useStore((state) => state.setScreen);
  const [code, setCode] = useState("");
  const [status, setStatus] = useState("");
  const [connected, setConnected] = useState(net.connected);
  // A counter we bump to re-read the live snapshot while in the lobby.
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!connected) return;
    const interval = setInterval(() => setTick((value) => value + 1), 300);
    return () => clearInterval(interval);
  }, [connected]);

  const create = async () => {
    setStatus("Creating room...");
    try {
      await net.createRoom(settings.name, 10);
      setConnected(true);
      setStatus("");
    } catch (error) {
      setStatus(`Could not create room: ${(error as Error).message}`);
    }
  };

  const join = async () => {
    if (!code.trim()) return;
    setStatus("Joining room...");
    try {
      await net.joinByCode(code, settings.name);
      setConnected(true);
      setStatus("");
    } catch (error) {
      setStatus(`Could not join: ${(error as Error).message}`);
    }
  };

  if (!connected) {
    return (
      <div className="panel" data-testid="lobby-setup">
        <h2>Play</h2>
        <button data-testid="create-room" onClick={create}>
          Create Room
        </button>
        <div className="join-row">
          <input
            data-testid="code-input"
            placeholder="Room code"
            value={code}
            onChange={(event) => setCode(event.target.value.toUpperCase())}
            maxLength={5}
          />
          <button data-testid="join-room" onClick={join}>
            Join
          </button>
        </div>
        {status && <p className="status">{status}</p>}
        <button onClick={() => setScreen("menu")}>Back</button>
      </div>
    );
  }

  const snapshot = net.snapshot();
  const players = snapshot?.players ?? [];
  const isHost = snapshot?.hostId === net.sessionId;
  const me = players.find((player) => player.id === net.sessionId);
  const everyoneReady = players.length > 0 && players.every((player) => player.ready);

  return (
    <div className="panel" data-testid="lobby">
      <h2>Lobby</h2>
      <p className="room-code">
        Room code: <strong data-testid="room-code">{snapshot?.roomCode}</strong>
      </p>
      <div className="player-list" data-testid="player-list">
        {players.map((player) => (
          <div className="player-entry" data-testid="player-entry" key={player.id}>
            <span>
              {player.name}
              {player.id === snapshot?.hostId ? " (host)" : ""}
            </span>
            <span>{player.ready ? "ready" : "waiting"}</span>
          </div>
        ))}
      </div>
      <div className="menu-buttons">
        <button data-testid="ready-button" onClick={() => net.setReady(!me?.ready)}>
          {me?.ready ? "Not ready" : "Ready"}
        </button>
        {isHost && (
          <button data-testid="start-button" disabled={!everyoneReady} onClick={() => net.startMatch()}>
            Start
          </button>
        )}
        <button
          onClick={() => {
            net.leave();
            setScreen("menu");
          }}
        >
          Leave
        </button>
      </div>
    </div>
  );
}
