// R3F scene: canvas, lighting, follow camera, and meshes for the local player,
// remote players (interpolated), and pickups. Positions update imperatively in
// useFrame from the live game/net data so React does not re-render every frame.
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { GameLoop } from "./loop";
import { NetClient } from "../net/client";
import { RemoteInterpolator } from "../net/interpolation";
import { attachInput } from "./controller";
import { Entity, bagNumber, bagString } from "./entities";

// Live game readout mirrored into the DOM for tests and debugging.
export interface Telemetry {
  tick: number;
  x: number;
  z: number;
  players: number;
  phase: string;
}

export function createTelemetry(): Telemetry {
  return { tick: 0, x: 0, z: 0, players: 0, phase: "" };
}

export function GameCanvas(props: { net: NetClient; loop: GameLoop; telemetry: Telemetry }) {
  return (
    <Canvas camera={{ position: [0, 9, 11], fov: 50 }} style={{ position: "absolute", inset: 0 }}>
      <color attach="background" args={["#0b1020"]} />
      <ambientLight intensity={0.6} />
      <directionalLight position={[6, 10, 6]} intensity={1.1} />
      <gridHelper args={[16, 16, "#334155", "#1e293b"]} />
      <mesh rotation={[-Math.PI / 2, 0, 0]}>
        <planeGeometry args={[16, 16]} />
        <meshStandardMaterial color="#0f172a" />
      </mesh>
      <Scene {...props} />
    </Canvas>
  );
}

function Scene({ net, loop, telemetry }: { net: NetClient; loop: GameLoop; telemetry: Telemetry }) {
  const interp = useRef(new RemoteInterpolator());
  const [remoteIds, setRemoteIds] = useState<string[]>([]);
  const remoteKey = useRef("");
  const { camera } = useThree();

  useEffect(() => attachInput(loop.input), [loop]);

  useFrame((_state, delta) => {
    loop.update(delta);
    const snapshot = net.snapshot();
    const localId = net.sessionId;

    const ids = new Set<string>();
    if (snapshot) {
      for (const player of snapshot.players) {
        if (player.id === localId) continue;
        ids.add(player.id);
        interp.current.setTarget(player.id, { x: player.x, y: player.y, z: player.z, ry: player.ry });
      }
    }
    interp.current.keep(ids);
    interp.current.step(Math.min(1, delta * 12));

    const key = [...ids].sort().join(",");
    if (key !== remoteKey.current) {
      remoteKey.current = key;
      setRemoteIds([...ids]);
    }

    const local = loop.world?.entities.get(localId);
    if (local) {
      camera.position.lerp(new THREE.Vector3(local.x, 9, local.z + 11), 0.08);
      camera.lookAt(local.x, 0, local.z);
      telemetry.x = round(local.x);
      telemetry.z = round(local.z);
    }
    telemetry.tick = loop.tick;
    telemetry.players = snapshot ? snapshot.players.length : 0;
    telemetry.phase = snapshot ? snapshot.phase : "";
  });

  const world = loop.world;
  const localId = net.sessionId;
  const pickups = world ? [...world.entities.values()].filter((e) => e.type === "pickup") : [];

  return (
    <group>
      <LocalPlayer getEntity={() => world?.entities.get(localId)} />
      {pickups.map((pickup) => (
        <PickupMesh key={pickup.id} entity={pickup} />
      ))}
      {remoteIds.map((id) => (
        <RemotePlayer key={id} id={id} interp={interp.current} />
      ))}
    </group>
  );
}

function LocalPlayer({ getEntity }: { getEntity: () => Entity | undefined }) {
  const ref = useRef<THREE.Group>(null);
  useFrame(() => {
    const entity = getEntity();
    if (entity && ref.current) {
      ref.current.position.set(entity.x, entity.y, entity.z);
      ref.current.rotation.y = entity.ry;
    }
  });
  return (
    <group ref={ref}>
      <mesh>
        <boxGeometry args={[0.8, 0.8, 0.8]} />
        <meshStandardMaterial color="#38bdf8" />
      </mesh>
    </group>
  );
}

function RemotePlayer({ id, interp }: { id: string; interp: RemoteInterpolator }) {
  const ref = useRef<THREE.Group>(null);
  useFrame(() => {
    const transform = interp.get(id);
    if (transform && ref.current) {
      ref.current.position.set(transform.x, transform.y, transform.z);
      ref.current.rotation.y = transform.ry;
    }
  });
  return (
    <group ref={ref}>
      <mesh>
        <boxGeometry args={[0.8, 0.8, 0.8]} />
        <meshStandardMaterial color="#f97316" />
      </mesh>
    </group>
  );
}

function PickupMesh({ entity }: { entity: Entity }) {
  const ref = useRef<THREE.Mesh>(null);
  useFrame((state) => {
    if (ref.current) {
      ref.current.position.set(
        entity.x,
        entity.y + Math.sin(state.clock.elapsedTime * 2) * 0.15,
        entity.z,
      );
      ref.current.rotation.y += 0.02;
    }
  });
  const color = bagString(entity, "color", "#fbbf24");
  const radius = bagNumber(entity, "radius", 0.45);
  return (
    <mesh ref={ref}>
      <icosahedronGeometry args={[radius, 0]} />
      <meshStandardMaterial color={color} emissive={color} emissiveIntensity={0.3} />
    </mesh>
  );
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}
