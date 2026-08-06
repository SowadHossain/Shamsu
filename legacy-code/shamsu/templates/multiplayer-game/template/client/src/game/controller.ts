// Local player input. WASD and arrow keys map to four movement intents.
export interface InputState {
  left: boolean;
  right: boolean;
  forward: boolean;
  back: boolean;
}

export function createInput(): InputState {
  return { left: false, right: false, forward: false, back: false };
}

const KEY_MAP: Record<string, keyof InputState> = {
  ArrowLeft: "left",
  ArrowRight: "right",
  ArrowUp: "forward",
  ArrowDown: "back",
  a: "left",
  d: "right",
  w: "forward",
  s: "back",
};

// Wire keyboard events to an input object. Returns a cleanup function.
export function attachInput(input: InputState): () => void {
  const set = (event: KeyboardEvent, value: boolean) => {
    const key = KEY_MAP[event.key];
    if (key) {
      input[key] = value;
      event.preventDefault();
    }
  };
  const down = (event: KeyboardEvent) => set(event, true);
  const up = (event: KeyboardEvent) => set(event, false);
  window.addEventListener("keydown", down);
  window.addEventListener("keyup", up);
  return () => {
    window.removeEventListener("keydown", down);
    window.removeEventListener("keyup", up);
  };
}
