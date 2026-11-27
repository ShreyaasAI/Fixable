// Static isometric drawing of the hero: shown while the 3D chunk loads and when WebGL is missing.

import { BLOCKS, HOVER_Z, SLOT, type Cell } from "./blocks";

const S = 58;
const C30 = Math.cos(Math.PI / 6);
const OX = 240;
const OY = 260;

const pt = (x: number, y: number, z: number) =>
  `${(OX + (x - y) * C30 * S).toFixed(1)},${(OY + (x + y) * 0.5 * S - z * S).toFixed(1)}`;

function Cube({ cell, amber = false }: { cell: Cell; amber?: boolean }) {
  const [x, y, z] = cell;
  const stroke = amber ? "var(--on-accent)" : "var(--muted)";
  const faces: [string, string][] = [
    [`${pt(x, y, z + 1)} ${pt(x + 1, y, z + 1)} ${pt(x + 1, y + 1, z + 1)} ${pt(x, y + 1, z + 1)}`, amber ? "var(--accent)" : "var(--raised)"],
    [`${pt(x, y + 1, z)} ${pt(x + 1, y + 1, z)} ${pt(x + 1, y + 1, z + 1)} ${pt(x, y + 1, z + 1)}`, amber ? "color-mix(in srgb, var(--accent) 80%, black)" : "var(--surface)"],
    [`${pt(x + 1, y, z)} ${pt(x + 1, y + 1, z)} ${pt(x + 1, y + 1, z + 1)} ${pt(x + 1, y, z + 1)}`, amber ? "color-mix(in srgb, var(--accent) 62%, black)" : "var(--bg)"],
  ];
  return (
    <g stroke={stroke} strokeWidth="1.2" strokeLinejoin="round">
      {faces.map(([points, fill], i) => (
        <polygon key={i} points={points} fill={fill} />
      ))}
    </g>
  );
}

export function HeroStatic() {
  const sorted = [...BLOCKS].sort((a, b) => a[0] + a[1] + a[2] - (b[0] + b[1] + b[2]) || a[2] - b[2]);
  const [sx, sy] = [SLOT[0] + 0.5, SLOT[1] + 0.5];
  return (
    <svg viewBox="0 0 480 480" className="size-full" role="presentation">
      {sorted.map((c) => (
        <Cube key={c.join()} cell={c} />
      ))}
      <polyline
        points={`${pt(sx, sy, SLOT[2] + 1)} ${pt(sx, sy, HOVER_Z)}`}
        stroke="var(--accent)"
        strokeWidth="1.5"
        strokeDasharray="4 5"
        fill="none"
      />
      <Cube cell={[SLOT[0], SLOT[1], HOVER_Z]} amber />
    </svg>
  );
}
