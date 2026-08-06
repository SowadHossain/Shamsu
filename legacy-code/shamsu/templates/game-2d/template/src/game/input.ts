// Keyboard input. `createInput` tracks which key codes are currently down;
// `readActions` maps those raw codes to named game actions the update step reads.

export interface Input {
  [code: string]: boolean;
}

export interface Actions {
  [name: string]: boolean;
}

export function createInput(): Input {
  const input: Input = {};
  window.addEventListener("keydown", (event) => {
    input[event.code] = true;
  });
  window.addEventListener("keyup", (event) => {
    input[event.code] = false;
  });
  return input;
}

export function readActions(input: Input): Actions {
  const actions: Actions = {};
  // HOLE:input
  // Placeholder: no controls are needed for the bouncing box.
  actions.up = input["ArrowUp"] === true;
  actions.down = input["ArrowDown"] === true;
  // END:input
  return actions;
}
