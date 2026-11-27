// Theme preference (m11 §2): "system" follows prefers-color-scheme live; "dark"/"light" stick.
// index.html applies the saved choice before first paint; this module keeps it in sync after.

import { useEffect, useState } from "react";

export type ThemePref = "system" | "dark" | "light";
const KEY = "fixable-theme";
const query = () => window.matchMedia("(prefers-color-scheme: light)");

export function readPref(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    return v === "dark" || v === "light" ? v : "system";
  } catch {
    return "system";
  }
}

function apply(pref: ThemePref): void {
  const light = pref === "light" || (pref === "system" && query().matches);
  document.documentElement.dataset.theme = light ? "light" : "dark";
}

export function useTheme(): [ThemePref, (pref: ThemePref) => void] {
  const [pref, setPref] = useState<ThemePref>(readPref);
  useEffect(() => {
    apply(pref);
    if (pref !== "system") return;
    const mq = query();
    const on = () => apply("system");
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [pref]);
  const choose = (next: ThemePref) => {
    try {
      if (next === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, next);
    } catch {
      /* private mode: the choice lasts for this page only */
    }
    setPref(next);
  };
  return [pref, choose];
}

/** Live `prefers-reduced-motion`, so the hero and UI motion react to a mid-session change. */
export function useReducedMotion(): boolean {
  const mq = () => window.matchMedia("(prefers-reduced-motion: reduce)");
  const [reduce, setReduce] = useState(() => mq().matches);
  useEffect(() => {
    const m = mq();
    const on = (e: MediaQueryListEvent) => setReduce(e.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return reduce;
}
