import { useState, type FormEvent } from "react";
import { DEV_AUTH, devSignIn, signInWithGitHub } from "../auth";
import { Hero3D } from "../hero/Hero3D";
import { messageFor } from "../messages";
import { AppError, Button, Footer, Input, Label, Panel, Textarea, Wordmark } from "../ui";

const STEPS = [
  ["01", "Pick a repo and the test that fails."],
  ["02", "Watch it work. Approve anything risky."],
  ["03", "Review the PR. The evidence comes with it."],
] as const;

export function SignIn({
  error,
  onError,
}: {
  error: string | null;
  onError: (message: string | null) => void;
}) {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="border-b border-line">
        <div className="mx-auto flex w-full max-w-[1100px] items-center justify-between gap-4 px-5 py-3.5 md:px-8 md:py-5">
          <Wordmark />
          <span className="eyebrow hidden md:inline">Fixes failing tests, with receipts</span>
        </div>
      </header>
      <main
        id="main"
        className="mx-auto grid w-full max-w-[1100px] flex-1 items-center gap-8 pb-12 md:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] md:gap-16 md:px-8 md:py-16"
      >
        <Hero3D className="order-first h-[260px] rounded-none border-x-0 border-t-0 md:order-last md:h-[560px] md:rounded-sm md:border" />
        <section aria-labelledby="signin-title" className="flex flex-col gap-7 px-5 md:px-0">
          <p className="eyebrow !text-accent">failing test → pull request</p>
          <h1
            id="signin-title"
            className="font-display text-[44px] font-extrabold leading-[1.04] tracking-[-0.01em] md:text-[68px]"
          >
            Red test in.
            <br />
            <em className="font-extrabold text-ok">Green PR out.</em>
          </h1>
          <p className="max-w-[30rem] text-base leading-relaxed text-muted md:text-[17px]">
            Point Fixable at a test that fails. It works on a copy of your repo in a sandbox, shows
            you every step as it happens, stops to ask before anything risky, and opens a pull
            request with the passing run attached.
          </p>
          <AppError message={error} />
          <div className="flex flex-col gap-3">
            <Button
              kind="primary"
              size="lg"
              data-testid="github-signin"
              className="w-full md:w-fit"
              onClick={() => {
                onError(null);
                signInWithGitHub().catch((err: unknown) =>
                  onError(err instanceof Error ? err.message : String(err)),
                );
              }}
            >
              <GitHubIcon /> Sign in with GitHub
            </Button>
            <p className="max-w-[30rem] text-[13px] text-muted">
              GitHub will ask for the <code className="font-mono text-fg">repo</code> scope, so
              the agent can clone your code and push a branch for the pull request.
            </p>
          </div>
          <ol className="border-t border-line">
            {STEPS.map(([n, text]) => (
              <li key={n} className="flex gap-4 border-b border-line py-3">
                <span className="font-mono text-[13px] font-medium text-accent">{n}</span>
                <span className="text-[15px]">{text}</span>
              </li>
            ))}
          </ol>
          {DEV_AUTH && <DevSignIn onError={onError} />}
        </section>
      </main>
      <Footer />
    </div>
  );
}

function DevSignIn({ onError }: { onError: (message: string | null) => void }) {
  const [token, setToken] = useState("");
  const [provider, setProvider] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    onError(null);
    try {
      await devSignIn(token, provider);
    } catch (err) {
      onError(messageFor(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel className="border-dashed p-5">
      <form className="flex flex-col gap-3" data-marker="swf-dev-signin" onSubmit={(e) => void submit(e)}>
        <h2 className="eyebrow">Dev sign-in</h2>
        <p className="text-[13px] text-muted">Development builds only. Paste a test JWT and a provider token.</p>
        <Label htmlFor="dev-token">JWT</Label>
        <Textarea
          id="dev-token"
          data-testid="dev-token"
          rows={3}
          className="min-h-20 font-mono text-xs"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          required
        />
        <Label htmlFor="dev-provider-token">GitHub provider token</Label>
        <Input
          id="dev-provider-token"
          data-testid="dev-provider-token"
          className="font-mono text-sm"
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        />
        <Button type="submit" kind="secondary" data-testid="dev-signin" disabled={busy} className="w-fit">
          Sign in (dev)
        </Button>
      </form>
    </Panel>
  );
}

function GitHubIcon() {
  return (
    <svg aria-hidden="true" width="18" height="18" viewBox="0 0 16 16" fill="currentColor">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}
