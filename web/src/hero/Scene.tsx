// The sign-in hero in WebGL (react-three-fiber, kept small): an orthographic isometric view of
// the block bench. The amber block hovers, drops into its slot, holds, and lifts again; the
// bench sways a few degrees. Colours come from the CSS tokens, so the theme toggle restyles it.
// Lazy-loaded: three.js never ships in the entry chunk.

import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { BLOCKS, HOVER_Z, SLOT } from "./blocks";

interface Palette {
  face: string;
  edge: string;
  accent: string;
  edgeOnAccent: string;
}

function readPalette(): Palette {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim() || "#888888";
  return { face: v("--raised"), edge: v("--muted"), accent: v("--accent"), edgeOnAccent: v("--on-accent") };
}

/** Re-read the tokens whenever <html data-theme> changes. */
function usePalette(): Palette {
  const [palette, setPalette] = useState(readPalette);
  useEffect(() => {
    const obs = new MutationObserver(() => setPalette(readPalette()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);
  return palette;
}

const BOX = new THREE.BoxGeometry(1, 1, 1);
const EDGES = new THREE.EdgesGeometry(BOX);
const at = ([x, y, z]: readonly number[]) => new THREE.Vector3(x - 1, z + 0.5, y - 1);

function Block({ pos, face, edge }: { pos: THREE.Vector3; face: string; edge: string }) {
  return (
    <group position={pos}>
      <mesh geometry={BOX}>
        <meshStandardMaterial color={face} roughness={0.9} />
      </mesh>
      <lineSegments geometry={EDGES}>
        <lineBasicMaterial color={edge} />
      </lineSegments>
    </group>
  );
}

// 0..1 of a 6 s loop: hover, drop (ease), hold seated, lift back.
function dropAt(t: number): number {
  const p = t % 6;
  if (p < 1.6) return 0;
  if (p < 2.3) return easeOutBack((p - 1.6) / 0.7);
  if (p < 4.6) return 1;
  if (p < 5.6) return 1 - easeInOut((p - 4.6) / 1.0);
  return 0;
}
const easeInOut = (x: number) => (x < 0.5 ? 2 * x * x : 1 - (-2 * x + 2) ** 2 / 2);
const easeOutBack = (x: number) => 1 + 2.2 * (x - 1) ** 3 + 1.2 * (x - 1) ** 2;

function Bench({ animated, palette }: { animated: boolean; palette: Palette }) {
  const bench = useRef<THREE.Group>(null);
  const amber = useRef<THREE.Group>(null);
  const top = useMemo(() => at([SLOT[0], SLOT[1], HOVER_Z]), []);
  const seat = useMemo(() => at(SLOT), []);
  useFrame(({ clock }) => {
    if (!animated || !bench.current || !amber.current) return;
    const t = clock.getElapsedTime();
    bench.current.rotation.y = Math.sin(t * 0.35) * 0.14;
    amber.current.position.lerpVectors(top, seat, dropAt(t));
  });
  return (
    <group ref={bench}>
      {BLOCKS.map((c) => (
        <Block key={c.join()} pos={at(c)} face={palette.face} edge={palette.edge} />
      ))}
      <group ref={amber} position={top}>
        <Block pos={new THREE.Vector3()} face={palette.accent} edge={palette.edgeOnAccent} />
      </group>
    </group>
  );
}

/** Redraw at 30 fps instead of every display frame: the motion is slow, and it halves the GPU and main-thread cost. */
function Ticker({ running }: { running: boolean }) {
  const invalidate = useThree((s) => s.invalidate);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => invalidate(), 1000 / 30);
    return () => clearInterval(timer);
  }, [running, invalidate]);
  return null;
}

export default function Scene({ animated }: { animated: boolean }) {
  const palette = usePalette();
  const host = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    if (!host.current) return;
    const io = new IntersectionObserver(([e]) => setVisible(e.isIntersecting));
    io.observe(host.current);
    return () => io.disconnect();
  }, []);
  return (
    <div ref={host} className="absolute inset-0">
      <Canvas
        orthographic
        dpr={[1, 1.5]}
        frameloop="demand"
        camera={{ position: [8, 7.2, 8], zoom: 62, near: 0.1, far: 100 }}
        gl={{ antialias: true, alpha: true }}
        onCreated={({ camera }) => camera.lookAt(0, 1.2, 0)}
      >
        <ambientLight intensity={1.6} />
        <directionalLight position={[4, 10, 6]} intensity={1.4} />
        <directionalLight position={[-6, 3, -2]} intensity={0.35} />
        <Ticker running={animated && visible} />
        <Bench animated={animated} palette={palette} />
      </Canvas>
    </div>
  );
}
