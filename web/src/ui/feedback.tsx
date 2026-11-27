// Alerts, the error boundary and the confirm dialog (m10a §2, m11 A2: shadcn AlertDialog).

import { Component, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Button } from "./button";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from "./primitives";

export function Alert({
  children,
  testId,
  className,
}: {
  children: ReactNode;
  testId: string;
  className?: string;
}) {
  return (
    <div
      role="alert"
      data-testid={testId}
      className={cn(
        "rounded-sm border border-danger/60 border-l-[3px] border-l-danger bg-raised px-4 py-3 text-[15px] text-fg",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function AppError({ message }: { message: string | null }) {
  if (!message) return null;
  return <Alert testId="app-error">{message}</Alert>;
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
      <Alert testId="page-error">
        This page failed to show: {this.state.error.message}. Reload the page; if it keeps
        happening, check that the API is the same version as this UI.
      </Alert>
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

/** Focus opens on Keep (the safe choice); Escape keeps; focus returns to the opener on close. */
export function ConfirmDialog(props: ConfirmProps) {
  return (
    <AlertDialog open={props.open} onOpenChange={(open) => !open && props.onCancel()}>
      <AlertDialogContent data-testid="confirm-dialog">
        <AlertDialogTitle className="font-display text-2xl font-extrabold tracking-[-0.01em]">
          {props.title}
        </AlertDialogTitle>
        <AlertDialogDescription asChild>
          <div className="text-[15px] leading-relaxed text-muted [&_.mono]:font-mono [&_.mono]:text-fg">
            {props.children}
          </div>
        </AlertDialogDescription>
        <div className="flex flex-wrap justify-end gap-3 pt-2">
          <AlertDialogCancel asChild>
            <Button kind="secondary" data-testid="confirm-no">
              Keep
            </Button>
          </AlertDialogCancel>
          <Button
            kind={props.danger ? "danger" : "primary"}
            data-testid="confirm-yes"
            onClick={props.onConfirm}
          >
            {props.confirmLabel}
          </Button>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  );
}
