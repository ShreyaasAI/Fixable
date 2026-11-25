# M10a manual checklist (DoD)

- **Purpose:** The plan §18 M10 DoD: every spec §3 action works in the browser. Run it against the Supabase Free project with real GitHub sign-in and the real worker (Portkey saved guardrail config on).
- **Last updated:** 2026-11-24
- **Status:** Not run yet.
- **Setup:** `docs/software-factory/m10-runbook.md` §1–§4.
- **Screenshots:** save them under `docs/software-factory/m10a-shots/` as `<nn>-<name>-<width>-<mode>.png`. Take them at 375 px and 1440 px, in dark and light mode (browser devtools → Rendering → `prefers-color-scheme`).

| # | Action (spec §3) | Expected | Result | Shot |
|---|---|---|---|---|
| 1 | Open `http://localhost:5173` signed out | Redirects to sign-in; `repo` scope stated | | |
| 2 | Sign in with GitHub | Dashboard; your login in the header; quota "N left" ×3 | | |
| 3 | New task → repo list | Only pushable repos enabled; base branch prefilled | | |
| 4 | Check (pre-flight) on `swf-pr-sandbox` | Image, memory, CPU, time limit; no task created | | |
| 5 | Check with a bad branch | Error next to the branch field; summary focused | | |
| 6 | Start | Task page; status live; plan approval card | | |
| 7 | Approve the plan | Timeline continues; tool calls and outputs; long output collapsed | | |
| 8 | Answer a question (if asked) | Card disappears after answering | | |
| 9 | Reload mid-run | Same timeline, no duplicates, stream continues | | |
| 10 | Noise toggle | Internal events hidden by default ("N hidden"); shown when on | | |
| 11 | Finish | Status completed; evidence listed; PR link opens the PR | | |
| 12 | Cancel a second task | Confirm dialog names the task; status cancelled | | |
| 13 | Delete a finished task | Gone from the list | | |
| 14 | Admin: pause all / resume all | New task Check shows "paused"; resume works | | |
| 15 | Admin: kill / resume a user (test account) | Their tasks cancelled; they see "disabled"; resume restores | | |
| 16 | Keyboard only (Tab/Enter/arrows) | Every action reachable; focus ring always visible | | |
| 17 | 375 px width | No horizontal page scroll; long output scrolls inside its box | | |

**Live smoke PR:** record the URL here, then close the PR and delete the branch.

- PR URL:
- Closed and branch deleted (date):
