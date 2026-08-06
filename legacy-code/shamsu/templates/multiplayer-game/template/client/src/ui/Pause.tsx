import { useStore } from "../store";
import { net } from "../session";

export function Pause() {
  const setPaused = useStore((state) => state.setPaused);
  const setScreen = useStore((state) => state.setScreen);
  return (
    <div className="overlay" data-testid="pause">
      <div className="panel">
        <h2>Paused</h2>
        <div className="menu-buttons">
          <button onClick={() => setPaused(false)}>Resume</button>
          <button
            onClick={() => {
              setPaused(false);
              net.leave();
              setScreen("menu");
            }}
          >
            Quit to Menu
          </button>
        </div>
      </div>
    </div>
  );
}
