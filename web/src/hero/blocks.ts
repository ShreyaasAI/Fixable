// The hero's arrangement (m11 §3, Figma "fig. 1"): a 3×3 workbench of blocks with one slot
// empty at (2, 0, 1); the amber block hovers above it and settles in. x, y run along the floor,
// z is height. Shared by the WebGL scene and the static SVG fallback.

export type Cell = readonly [x: number, y: number, z: number];

export const SLOT: Cell = [2, 0, 1];
export const HOVER_Z = 3.1;

export const BLOCKS: Cell[] = (() => {
  const cells: Cell[] = [];
  for (let x = 0; x < 3; x++) for (let y = 0; y < 3; y++) cells.push([x, y, 0]);
  for (const [x, y] of [[0, 0], [1, 0], [0, 1], [1, 1], [0, 2], [2, 1], [1, 2], [2, 2]] as const)
    cells.push([x, y, 1]);
  cells.push([0, 0, 2], [0, 1, 2]);
  return cells;
})();
