import { ThemeToggle } from "./theme-toggle";

export function Footer() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto flex w-full max-w-[1100px] flex-wrap items-center justify-between gap-4 px-5 py-5 md:px-8">
        <p className="font-mono text-xs tracking-[0.02em] text-muted">
          Opens a pull request. Never merges it.
        </p>
        <ThemeToggle />
      </div>
    </footer>
  );
}
