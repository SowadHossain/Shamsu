import { useEffect } from "react";
import { useStore } from "./store";
import { net } from "./session";
import { MainMenu } from "./ui/MainMenu";
import { Settings } from "./ui/Settings";
import { Leaderboard } from "./ui/Leaderboard";
import { Credits } from "./ui/Credits";
import { Lobby } from "./ui/Lobby";
import { GameScreen } from "./ui/GameScreen";
import { GameOver } from "./ui/GameOver";

export default function App() {
  const screen = useStore((state) => state.screen);
  const setScreen = useStore((state) => state.setScreen);

  // The server owns the match phase. Watch it and move every client between
  // lobby, game, and game-over screens together.
  useEffect(() => {
    const interval = setInterval(() => {
      const snapshot = net.snapshot();
      if (!snapshot) return;
      const current = useStore.getState().screen;
      if (snapshot.phase === "playing" && current === "lobby") setScreen("game");
      else if (snapshot.phase === "ended" && (current === "game" || current === "lobby"))
        setScreen("gameover");
      else if (snapshot.phase === "lobby" && (current === "game" || current === "gameover"))
        setScreen("lobby");
    }, 150);
    return () => clearInterval(interval);
  }, [setScreen]);

  return (
    <div className="app" data-testid="app">
      {screen === "menu" && <MainMenu />}
      {screen === "settings" && <Settings />}
      {screen === "leaderboard" && <Leaderboard />}
      {screen === "credits" && <Credits />}
      {screen === "lobby" && <Lobby />}
      {screen === "game" && <GameScreen />}
      {screen === "gameover" && <GameOver />}
    </div>
  );
}
