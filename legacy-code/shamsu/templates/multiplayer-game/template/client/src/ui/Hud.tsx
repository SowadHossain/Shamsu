import { NetSnapshot } from "../net/client";
import { net } from "../session";

// The HUD frame is pre-built. Binding the slots to real game-state fields is the
// hole the model fills.
export function Hud({ snapshot }: { snapshot: NetSnapshot | null }) {
  const me = snapshot?.players.find((player) => player.id === net.sessionId);
  return (
    <aside className="hud" data-testid="hud">
      {/* HOLE:ui.hud - bind these slots to your game state fields */}
      <div className="hud-slot">
        <span>Score</span>
        <strong data-testid="hud-score">{me?.score ?? 0}</strong>
      </div>
      <div className="hud-slot">
        <span>Players</span>
        <strong>{snapshot?.players.length ?? 0}</strong>
      </div>
      <div className="hud-slot">
        <span>Goal</span>
        <strong>{snapshot?.winScore ?? 10}</strong>
      </div>
      <div className="hud-slot">
        <span>Phase</span>
        <strong>{snapshot?.phase ?? ""}</strong>
      </div>
    </aside>
  );
}
