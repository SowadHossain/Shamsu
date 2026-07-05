You are building a MULTIPLAYER 3D BROWSER GAME from a working template.

NON-NEGOTIABLE: a multiplayer game is not done until ALL of these exist and work:
- A main menu screen with start, join, and settings affordances.
- A lobby that lists connected players and exposes a start control.
- A live relay-style network connection that syncs player state.
- A game loop that updates and renders every frame.
- The local player plus remote players rendered as distinct objects.
- A HUD showing score/state.
- A reachable win, lose, or end condition.

Forced stack:
- React + React-Three-Fiber + Three.js
- Vite + TypeScript
- Colyseus relay room for multiplayer
- Rapier physics dependency is available for collisions/physics

The template already provides the menu shell, lobby wiring, Colyseus relay
client/server, render loop, and HUD shell. Do not rewrite these systems. Do not
replace Colyseus with raw WebSockets or another networking stack. Do not
collapse the game into a single object.

Only fill marked holes for entity definitions, game rules, spawn/collision
logic, win/end condition, and HUD bindings. If the request is a cube runner,
the cube is one entity type; the menu, lobby, connection, remote players, loop,
HUD, and end condition still remain required.
