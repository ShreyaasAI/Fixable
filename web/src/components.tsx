// Shared UI pieces: status badge, confirm dialog, error alert (m10a §2, §3).

import { Component, useEffect, useRef, type ReactNode } from "react";

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge badge-${statusTone(status)}`} data-status={status}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

export function statusTone(status: string): string {
  if (status === "completed") return "ok";
  if (status === "failed" || status === "rejected" || status === "interrupted") return "bad";
  if (status === "cancelled") return "muted";
  if (status.startsWith("awaiting")) return "warn";
  return "info";
}

export function AppError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="alert" role="alert" data-testid="app-error">
      {message}
    </div>
  );
}

/** A render error shows as an alert in the page instead of unmounting the whole app. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div className="alert" role="alert" data-testid="page-error">
        This page failed to show: {this.state.error.message}. Reload the page; if it keeps
        happening, check that the API is the same version as this UI.
      </div>
    );
  }
}

interface ConfirmProps {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** A modal `<dialog>`: focus moves in on open and back to the opener on close. */
export function ConfirmDialog(props: ConfirmProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const opener = useRef<Element | null>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (props.open && !dialog.open) {
      opener.current = document.activeElement;
      dialog.showModal();
    } else if (!props.open && dialog.open) {
      dialog.close();
      if (opener.current instanceof HTMLElement) opener.current.focus();
    }
  }, [props.open]);
  return (
    <dialog
      ref={ref}
      className="dialog"
      data-testid="confirm-dialog"
      aria-labelledby="confirm-title"
      onCancel={(e) => {
        e.preventDefault();
        props.onCancel();
      }}
    >
      <h2 id="confirm-title">{props.title}</h2>
      <div className="dialog-body">{props.children}</div>
      <div className="row end">
        <button
          type="button"
          className="btn"
          data-testid="confirm-no"
          onClick={props.onCancel}
          autoFocus
        >
          Keep
        </button>
        <button
          type="button"
          className={props.danger ? "btn btn-danger" : "btn btn-primary"}
          data-testid="confirm-yes"
          onClick={props.onConfirm}
        >
          {props.confirmLabel}
        </button>
      </div>
    </dialog>
  );
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function timeAgo(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)} s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}
