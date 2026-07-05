import type { GameState } from "../game/entities";

type HudProps = {
  state: GameState;
};

export function Hud({ state }: HudProps) {
  const local = state.players.find((player) => player.id === state.localPlayerId);
  const winner = state.players.find((player) => player.id === state.endState.winnerId);

  return (
    <aside className="sidebar" data-testid="hud">
      <h2>HUD</h2>
      {/* HOLE:ui.hud_fields */}
      <p>Phase: {state.phase}</p>
      <p>Players: {state.players.length}</p>
      <p>Score: {Math.floor(local?.score ?? 0)}</p>
      <p>Time: {state.elapsed.toFixed(1)}s</p>
      {state.endState.ended ? (
        <p data-testid="end-state">Winner: {winner?.name ?? "unknown"}</p>
      ) : null}
    </aside>
  );
}
