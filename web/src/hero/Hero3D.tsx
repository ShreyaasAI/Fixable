// The sign-in hero's frame: cutting-mat grid, crop marks, caption. The WebGL scene loads lazily
// on top of the static drawing; reduced motion renders one still frame (m11 §2).

import { lazy, Suspense, useMemo } from "react";
import { useReducedMotion } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { HeroStatic } from "./HeroStatic";

const Scene = lazy(() => import("./Scene"));

function hasWebGL(): boolean {
  try {
    const c = document.createElement("canvas");
    return Boolean(c.getContext("webgl2") ?? c.getContext("webgl"));
  } catch {
    return false;
  }
}

function Crop({ className }: { className: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" className={cn("absolute", className)}>
      <path d="M8 0v16M0 8h16" stroke="var(--muted)" strokeWidth="1" />
    </svg>
  );
}

export function Hero3D({ className }: { className?: string }) {
  const reduce = useReducedMotion();
  const webgl = useMemo(hasWebGL, []);
  return (
    <div
      data-testid="hero-3d"
      data-motion={reduce ? "static" : "animated"}
      data-renderer={webgl ? "webgl" : "svg"}
      aria-hidden="true"
      className={cn("mat relative overflow-hidden rounded-sm border border-line", className)}
    >
      <div className="absolute inset-[8%]">
        {webgl ? (
          <Suspense fallback={<HeroStatic />}>
            <Scene animated={!reduce} />
          </Suspense>
        ) : (
          <HeroStatic />
        )}
      </div>
      <Crop className="left-4 top-4" />
      <Crop className="right-4 top-4" />
      <Crop className="bottom-4 left-4" />
      <Crop className="bottom-4 right-4" />
      <p className="absolute bottom-3.5 left-10 font-mono text-xs tracking-[0.02em] text-muted">
        fig. 1 — one block out of place
      </p>
    </div>
  );
}
