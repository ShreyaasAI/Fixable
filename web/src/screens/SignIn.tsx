import { useState, type FormEvent } from "react";
import { DEV_AUTH, devSignIn, signInWithGitHub } from "../auth";
import { messageFor } from "../messages";

export function SignIn({ onError }: { onError: (message: string | null) => void }) {
  return (
    <section className="card stack" aria-labelledby="signin-title">
      <h1 id="signin-title">Sign in to SWF</h1>
      <p>
        SWF fixes a failing test in your GitHub repository and opens a pull request with the
        evidence.
      </p>
      <p className="muted">
        Signing in asks GitHub for the <code>repo</code> scope: read and write access to your
        repositories, so the agent can clone the code and push a branch for the pull request. It
        never merges.
      </p>
      <button
        type="button"
        className="btn btn-primary btn-lg"
        data-testid="github-signin"
        onClick={() => {
          onError(null);
          signInWithGitHub().catch((err: unknown) =>
            onError(err instanceof Error ? err.message : String(err)),
          );
        }}
      >
        <GitHubIcon /> Sign in with GitHub
      </button>
      {DEV_AUTH && <DevSignIn onError={onError} />}
    </section>
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
    <form className="stack dev" data-marker="swf-dev-signin" onSubmit={(e) => void submit(e)}>
      <h2>Dev sign-in</h2>
      <p className="muted">Development builds only. Paste a test JWT and a provider token.</p>
      <label htmlFor="dev-token">JWT</label>
      <textarea
        id="dev-token"
        data-testid="dev-token"
        rows={3}
        value={token}
        onChange={(e) => setToken(e.target.value)}
        required
      />
      <label htmlFor="dev-provider-token">GitHub provider token</label>
      <input
        id="dev-provider-token"
        data-testid="dev-provider-token"
        value={provider}
        onChange={(e) => setProvider(e.target.value)}
      />
      <button type="submit" className="btn" data-testid="dev-signin" disabled={busy}>
        Sign in (dev)
      </button>
    </form>
  );
}

function GitHubIcon() {
  return (
    <svg aria-hidden="true" width="18" height="18" viewBox="0 0 16 16" fill="currentColor">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}
