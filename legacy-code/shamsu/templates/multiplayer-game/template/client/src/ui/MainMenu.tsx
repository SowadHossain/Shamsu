import { useStore } from "../store";

export function MainMenu() {
  const setScreen = useStore((state) => state.setScreen);
  return (
    <div className="panel" data-testid="main-menu">
      <h1>SHAMSU Arena</h1>
      <p>A multiplayer 3D browser game template.</p>
      <div className="menu-buttons">
        <button data-testid="play-button" onClick={() => setScreen("lobby")}>
          Play
        </button>
        <button onClick={() => setScreen("settings")}>Settings</button>
        <button onClick={() => setScreen("leaderboard")}>Leaderboard</button>
        <button onClick={() => setScreen("credits")}>Credits</button>
      </div>
    </div>
  );
}
