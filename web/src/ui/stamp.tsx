import { motion } from "motion/react";
import { cn } from "@/lib/utils";

export type Tone = "ok" | "danger" | "warn" | "accent" | "muted" | "info";

export function statusTone(status: string): Tone {
  if (status === "completed") return "ok";
  if (status === "failed" || status === "rejected" || status === "interrupted") return "danger";
  if (status === "cancelled" || status === "queued" || status === "loading") return "muted";
  if (status.startsWith("awaiting")) return "warn";
  return "accent";
}

const TONE: Record<Tone, string> = {
  ok: "border-ok text-ok",
  danger: "border-danger text-danger",
  warn: "border-warn text-warn",
  accent: "border-accent text-accent",
  muted: "border-muted text-muted",
  info: "border-info text-info",
};
const DOT: Record<Tone, string> = {
  ok: "bg-ok",
  danger: "bg-danger",
  warn: "bg-warn",
  accent: "bg-accent",
  muted: "bg-muted",
  info: "bg-info",
};

/** A rubber-stamp label: mono caps in a ruled box. Always carries text, never colour alone. */
export function Stamp({
  label,
  tone,
  className,
  animate = false,
  ...rest
}: {
  label: string;
  tone: Tone;
  className?: string;
  animate?: boolean;
  "data-status"?: string;
}) {
  return (
    <motion.span
      key={animate ? label : undefined}
      initial={animate ? { scale: 1.35, rotate: -6, opacity: 0 } : false}
      animate={{ scale: 1, rotate: 0, opacity: 1 }}
      transition={{ type: "spring", stiffness: 520, damping: 22 }}
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-xs border-[1.25px] px-2 py-[3px] font-mono text-xs font-medium uppercase leading-none tracking-[0.08em]",
        TONE[tone],
        className,
      )}
      {...rest}
    >
      <span aria-hidden="true" className={cn("size-1.5 rounded-full", DOT[tone])} />
      {label}
    </motion.span>
  );
}

export function StatusStamp({ status, animate }: { status: string; animate?: boolean }) {
  return (
    <Stamp
      label={status.replace(/_/g, " ")}
      tone={statusTone(status)}
      animate={animate}
      data-status={status}
    />
  );
}

export function ExitStamp({ code }: { code: number }) {
  return <Stamp label={`exit ${code}`} tone={code === 0 ? "ok" : "danger"} />;
}
