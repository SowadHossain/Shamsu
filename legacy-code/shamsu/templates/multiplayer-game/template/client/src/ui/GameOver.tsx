import { useStore } from "../store";
import { net } from "../session";

export function GameOver() {
  const setScreen = useStore((state) => state.setScreen);
  const snapshot = net.snapshot();
  const winner = snapshot?.players.find((player) => player.id === snapshot.winnerId);
  const me = snapshot?.players.find((player) => player.id === net.sessionId);
  const isHost = snapshot?.hostId === net.sessionId;
  const won = snapshot?.winnerId === net.sessionId;

  return (
    <div className="panel" data-testid="game-over">
      <h1>{won ? "You Win" : "Game Over"}</h1>
      <p data-testid="winner">Winner: {winner?.name ?? "unknown"}</p>
      <p>Your score: {me?.score ?? 0}</p>
      <div className="menu-buttons">
        {isHost && <button onClick={() => net.backToLobby()}>Back to Lobby</button>}
        <button
          onClick={() => {
            net.leave();
            setScreen("menu");
          }}
        >
          Back to Menu
        </button>
      </div>
    </div>
  );
}
