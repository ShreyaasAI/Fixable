// Small layout pieces: Panel, Field, Ledger, PageHead, Wordmark.

import type { ComponentProps, ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Label } from "./primitives";

export function Panel({ className, ...props }: ComponentProps<"section">) {
  return (
    <section className={cn("rounded-sm border border-line bg-surface", className)} {...props} />
  );
}

/** Label, control, hint and the inline error the e2e tests read (`field-error`, `data-field`). */
export function Field({
  id,
  label,
  hint,
  field,
  error,
  children,
}: {
  id: string;
  label: string;
  hint?: ReactNode;
  field: string;
  error?: string | null;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && <p className="text-[13px] text-muted">{hint}</p>}
      {error && (
        <p
          className="flex items-start gap-1.5 text-[13px] text-danger"
          id={`${field}-error`}
          data-testid="field-error"
          data-field={field}
        >
          <span aria-hidden="true" className="font-mono">
            ✕
          </span>
          {error}
        </p>
      )}
    </div>
  );
}

export function PageHead({
  title,
  lede,
  action,
}: {
  title: ReactNode;
  lede?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
      <div className="flex min-w-0 flex-col gap-2">
        <h1 className="font-display text-[32px] font-extrabold leading-[1.1] tracking-[-0.01em] md:text-[44px]">
          {title}
        </h1>
        {lede && <p className="max-w-prose text-muted">{lede}</p>}
      </div>
      {action}
    </div>
  );
}

export interface LedgerCell {
  testId: string;
  label: string;
  value: string;
  of: string;
}

/** The quota ledger: three ruled cells, a big serif number and "left". */
export function Ledger({ cells, label }: { cells: LedgerCell[]; label: string }) {
  return (
    <Panel
      aria-label={label}
      className="grid grid-cols-1 divide-y divide-line sm:grid-cols-3 sm:divide-x sm:divide-y-0"
    >
      {cells.map((c) => (
        <div
          key={c.testId}
          data-testid={c.testId}
          className="flex items-center justify-between gap-3 px-4 py-3 sm:flex-col sm:items-start sm:justify-start sm:gap-1.5 sm:px-6 sm:py-5"
        >
          <span className="eyebrow whitespace-nowrap">{c.label}</span>
          <span className="flex flex-col items-end gap-0.5 sm:items-start sm:gap-1.5">
            <span className="flex items-baseline gap-2">
              <span className="font-display text-2xl font-extrabold tracking-[-0.01em] sm:text-4xl">
                {c.value}
              </span>
              <span className="font-mono text-sm">left</span>
            </span>
            <span className="font-mono text-xs text-muted">{c.of}</span>
          </span>
        </div>
      ))}
    </Panel>
  );
}

export function Wordmark() {
  return (
    <span className="inline-flex items-center gap-2">
      <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true" className="shrink-0">
        <g fill="none" stroke="var(--fg)" strokeWidth="1.5">
          <rect x="1" y="1" width="9" height="9" rx="1" />
          <rect x="1" y="12" width="9" height="9" rx="1" />
          <rect x="12" y="12" width="9" height="9" rx="1" />
        </g>
        <rect x="12" y="1" width="9" height="9" rx="1" fill="var(--accent)" />
      </svg>
      <span className="font-display text-[22px] font-extrabold leading-none tracking-[-0.01em]">
        Fixable
      </span>
    </span>
  );
}
