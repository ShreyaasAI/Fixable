# M2 contract — sandbox, egress proxy, pre-flight

- **Purpose:** The single interface contract for the parallel M2 build. The coder implements it, the test writer tests it, the checker verifies both against it. If something here is wrong, report it; do not silently diverge.
- **Last updated:** 2026-11-20
- **Status:** Working contract for M2 (worktree `feature/m2-sandbox-preflight-1120a`)

## 0. Decisions (user, 2026-11-19/20) [D]

1. Egress proxy = **Squid**, image `ubuntu/squid:6.6-24.04_edge` (multi-arch incl. arm64). We write only `squid.conf`.
2. 1-day package cache **deferred to M9** (pull-through, read-only to tasks). M2 = allowlist only, `cache deny all`.
3. Estimate above the per-task limit → **reject at creation**: task row is created with status `rejected`, before any container; it does **not** count toward the daily task quota.
4. **1 vCPU per task** (`--cpus 1`), 4 GB. Spec §12's "2 vCPU" line is stale → fixed by the planner.
5. Host capacity for admission: **2 concurrent tasks, 8192 MB, 2.0 CPUs** (12 GB host leaves 4 GB for OS/API/worker/proxy).
6. Registry allowlist (SSRF guard: `image:` comes from the user's repo): `registry-1.docker.io` (`docker.io`, `index.docker.io` normalise to it), `ghcr.io`, `quay.io`, `public.ecr.aws`, `mcr.microsoft.com`, `registry.gitlab.com`.
7. Sandbox containers get **no DNS** (`--dns 127.0.0.1`) and reach the proxy **by its IP** on the internal network. Squid resolves names. This closes DNS exfiltration regardless of Docker's embedded-DNS behaviour on internal networks.
8. Pre-flight runs inside `POST /v1/tasks` via the GitHub API (repo, branch, `AGENTS.md`), so the repo/branch access check moves from M6 to M2.
9. Seeds: `setup` becomes `python -m pip install --quiet pytest==9.2.0` (through the proxy).
10. Test runs set `PYTHONDONTWRITEBYTECODE=1` (host and container).

## 1. `swf.images` (tests already exist: `tests/test_images.py`)

- `@dataclass(frozen=True) ImageRef(registry: str, repository: str, reference: str)` (positional order as listed).
- `parse_image_ref(ref: str) -> ImageRef`. First path part is a registry iff it contains `.` or `:` or equals `localhost`; otherwise registry = `registry-1.docker.io`; a single-component Docker Hub name gets `library/`. Default tag `latest`; `name:tag@sha256:<64 hex>` → reference = the digest. Repository components `[a-z0-9]+(?:[._-][a-z0-9]+)*`; tag `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`; digest `sha256:[a-f0-9]{64}`; total length ≤ 255; anything else (`""`, spaces, uppercase repo, `python:`, `:tag`, `http://…`, short digest) → `InvalidImageRef`.
- Exceptions: `ImageError` base; `InvalidImageRef`, `RegistryNotAllowed`, `ImageNotFound`, `RegistryError` subclass it.
- `ALLOWED_REGISTRIES: frozenset[str]` per decision 6.
- `RegistryClient(*, transport: httpx.BaseTransport | None = None, allowed_registries: frozenset[str] = ALLOWED_REGISTRIES, timeout_seconds: float = 15.0)`
  - `platforms(image: str) -> frozenset[str]` of `"os/arch"` (variant dropped; entries with os `unknown` skipped).
  - `supports(image: str, platform: str = "linux/arm64") -> bool`.
  - Allowlist check **before any request** → `RegistryNotAllowed`.
  - `GET https://<registry>/v2/<repo>/manifests/<ref>` with `Accept` = OCI index, Docker manifest list, OCI manifest, Docker manifest v2.
  - On 401 with `WWW-Authenticate: Bearer realm=…,service=…,scope=…`: realm must be `https` (else `RegistryError`); `GET realm?service=…&scope=…` anonymously; read `token` (or `access_token`); retry with `Authorization: Bearer <token>`. Token reused for the blob fetch.
  - Index/list → platforms from `manifests[].platform`. Single manifest → `GET /v2/<repo>/blobs/<config.digest>` → `os`/`architecture`.
  - 404 → `ImageNotFound`; other ≥ 400, bad JSON, `httpx.HTTPError` → `RegistryError`. No redirects to other hosts with the token.

## 2. `swf.preflight` (tests already exist: `tests/test_preflight.py`)

- `@dataclass(frozen=True) Limits`: `task_memory_mb=4096, task_cpus=1.0, host_memory_mb=8192, host_cpus=2.0, max_concurrent=2, max_repo_mb=1024, time_limit_minutes=60, default_image="python:3.12-slim"`.
- `@dataclass(frozen=True) Reservation(memory_mb: int, cpus: float)`; `@dataclass(frozen=True) Capacity(active: tuple[Reservation, ...], queued: int)`; `@dataclass(frozen=True) RepoFacts(size_kb: int, agents_md: str | None)`.
- `PreflightWarning(BaseModel): code: str, message: str`.
- `PreflightResult(BaseModel): decision: Literal["admit","queue","reject"], image: str, memory_mb: int, cpus: float, time_limit_minutes: int, queue_position: int | None, warnings: list[PreflightWarning]`.
- `evaluate(facts, capacity, image_check: Callable[[str], bool], limits: Limits = Limits()) -> PreflightResult`:
  1. `agents_md is None` → image = `limits.default_image`, memory/cpus = `swf.config.Resources()` defaults (2048 / 1.0), warning `agents_md_missing`.
  2. Else `swf.config.parse_agents_md`; any `AgentsFileError` → **reject** `agents_md_invalid` (message = loader message), image = default image, defaults for memory/cpus, **image_check not called**.
  3. Reject checks (collect all): memory > task limit or cpus > task limit → `over_task_limit` (message names requested and limit values, e.g. "8192 MB requested, limit 4096 MB"); `size_kb > max_repo_mb*1024` → `repo_too_large`; image check: `False` → `image_not_arm64`, `ImageNotFound` → `image_not_found`, `RegistryNotAllowed` → `registry_not_allowed`, `InvalidImageRef` → `image_invalid`, other `ImageError` → `registry_error`. Exactly at the limit is fine.
  4. Any reject → `decision="reject"`, `queue_position=None`, no capacity check, no `host_busy`.
  5. Else fits iff `len(active) < max_concurrent` and `Σmem + mem ≤ host_memory_mb` and `Σcpu + cpus ≤ host_cpus + 1e-9` and `queued == 0` → `admit`; otherwise `queue`, `queue_position = queued + 1`, warning `host_busy`.
- `PreflightInputError(Exception)`; `RepoNotFound`, `BranchNotFound` subclass it.
- `PreflightService(github: GitHubClient, registry: RegistryClient, limits: Limits = Limits(), platform: str = "linux/arm64")`
  - `check(token: str, repo: str, branch: str, capacity: Capacity) -> PreflightResult`: `github.get_repo` (`GitHubNotFound` → `RepoNotFound`), `github.branch_exists` False → `BranchNotFound`, `github.get_file(token, repo, "AGENTS.md", ref=branch)`, then `evaluate(RepoFacts(meta.size_kb, text), capacity, lambda img: registry.supports(img, platform), limits)`. `GitHubAuthError`/`GitHubError` propagate.

## 3. `swf.github` additions

- `GitHubNotFound(GitHubError)`.
- `@dataclass(frozen=True) RepoMeta(full_name: str, default_branch: str, private: bool, can_push: bool, size_kb: int)`.
- `get_repo(token, full_name) -> RepoMeta` (`GET /repos/{full_name}`; 404 → `GitHubNotFound`).
- `branch_exists(token, full_name, branch) -> bool` (`GET /repos/{full_name}/branches/{branch}`; 404 → False).
- `get_file(token, full_name, path, *, ref) -> str | None` (`GET /repos/{full_name}/contents/{path}?ref=…`; 404 → None; requires `type == "file"`, base64 → UTF-8; content > 1 MiB → `GitHubError`).
- Existing behaviour (Bearer header, 401 → `GitHubAuthError`, never follow links to other hosts) unchanged.

## 4. `swf.sandbox`

- `LABEL_MANAGED = "swf.managed"` (value `"1"`), `LABEL_TASK = "swf.task"`.
- `SandboxSpec(BaseModel, frozen, extra="forbid")`: `task_id: str` (`^[A-Za-z0-9_.-]{1,64}$`), `image: str`, `workspace: Path` (absolute, existing dir), `proxy_url: str` (required), `memory_mb: int = 4096` (> 0), `cpus: float = 1.0` (> 0), `pids_limit: int = 512` (> 0), `network: str = "swf-sandbox"`, `user: str = "1000:1000"`, `runtime: str | None = None`, `command_timeout: int = 300`, `container_timeout: str = "2h"`.
- `sandbox_run_args(spec) -> list[str]`, containing (as separate argv items):
  `--rm`, `--init`, `--label swf.managed=1`, `--label swf.task=<id>`, `--network <network>`, `--dns 127.0.0.1`, `--memory <mb>m`, `--memory-swap <mb>m`, `--cpus <cpus>`, `--pids-limit <n>`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--user <user>`, `--mount type=bind,source=<abs workspace>,target=/repo`, and `-e` for `HOME=/tmp/home`, `PYTHONDONTWRITEBYTECODE=1`, `PIP_DISABLE_PIP_VERSION_CHECK=1`, `PIP_NO_INPUT=1`, `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, `https_proxy` (= proxy_url), `NO_PROXY=`, `no_proxy=`; plus `--runtime <r>` only if set. Never `--privileged`, `-v`, `docker.sock`, `--network host`, `--cap-add`.
- `SandboxEnv(DockerEnvironment)`: `__init__(self, spec: SandboxSpec, **kwargs)` sets `self.spec` **before** `super().__init__(image=spec.image, cwd="/repo", run_args=sandbox_run_args(spec), env={}, forward_env=[], timeout=spec.command_timeout, container_timeout=spec.container_timeout, **kwargs)` (upstream `__init__` starts the container). `cleanup()` overrides upstream (whose shell line breaks under cmd.exe): synchronous `docker rm -f <id>` (argv list, `timeout=60`, errors ignored), then `container_id = None`; idempotent.
- `cleanup_orphans(active_task_ids: Collection[str], *, docker: str = "docker") -> list[str]`: list `docker ps -a --filter label=swf.managed=1 --format '{{.ID}}\t{{.Label "swf.task"}}'`, `rm -f` those whose task id is not active, return removed container ids.

## 5. `swf.egress`

- `EgressError(Exception)`.
- `EgressConfig(BaseModel, frozen)`: `network="swf-sandbox"`, `proxy_name="swf-egress-proxy"`, `image="ubuntu/squid:6.6-24.04_edge"`, `port=3128`, `allow_domains: tuple[str, ...] = ("pypi.org", "files.pythonhosted.org")`, each matching `^\.?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$` (else validation error).
- `render_squid_conf(cfg) -> str` with, one per line: `http_port <port>`, `acl allowed_domains dstdomain -n <domains…>`, `acl SSL_ports port 443`, `acl CONNECT method CONNECT`, `http_access deny !CONNECT`, `http_access deny CONNECT !SSL_ports`, `http_access allow CONNECT allowed_domains`, `http_access deny all`, `cache deny all`, `via off`, `forwarded_for delete`, `httpd_suppress_version_string on`, `shutdown_lifetime 1 seconds`. Coder verifies it with `squid -k parse` in the image.
- `@dataclass(frozen=True) EgressInfo(container_id: str, proxy_ip: str, proxy_url: str)` (`proxy_url = http://<proxy_ip>:<port>`).
- `ensure_egress(cfg, *, docker="docker") -> EgressInfo`: network missing → `docker network create --internal --label swf.egress=1 <network>`; exists but not `Internal` → `EgressError`. Proxy container labelled `swf.egress=1` and `swf.egress.conf-sha256=<sha256 of conf>` (**never** `swf.managed=1`, or orphan cleanup would kill it). Reuse if running with the same hash; else `rm -f` and recreate: `docker create --name <proxy_name> --label … --restart unless-stopped <image>` (default bridge, **no published ports**), `docker cp <tmp conf> <name>:/etc/squid/squid.conf`, `docker network connect <network> <name>`, `docker start <name>`, wait ≤ 30 s for "Accepting HTTP Socket connections" in `docker logs` (else `EgressError` with the log tail). Proxy IP = its address on `<network>`.
- `teardown_egress(cfg, *, docker="docker") -> None`: remove proxy and network (tests).

## 6. DB, store, API

- Migration `supabase/migrations/20261120000000_m2_preflight.sql`: `alter table public.tasks add column preflight jsonb;` (existing grants/RLS cover it).
- `store.ACTIVE_STATUSES` = `preparing, investigating, awaiting_plan_approval, executing, awaiting_command_approval, awaiting_input, verifying, confirming, opening_pr`.
- `store.capacity_snapshot(conn) -> preflight.Capacity`: active = `(coalesce(reservation_mb, 4096), coalesce(reservation_cpu, 1.0))` of tasks in ACTIVE_STATUSES (all users); queued = count of `queued`.
- `store.Task` gains `preflight: PreflightResult | None`.
- `store.create_task(conn, user_id, *, repo, base_branch, description, failing_test, day, status: str, reservation_mb: int | None, reservation_cpu: float | None, preflight: PreflightResult | None) -> Task`; increments `quotas_usage.tasks` **only if status != "rejected"**; audit payload `{"repo", "base_branch", "preflight": <decision or None>}`.
- `AppDeps` gains `preflight: PreflightService`. `POST /v1/tasks`: token blob missing → 409 `github_token_missing`; decrypt fails → 409 `github_token_invalid`; capacity from `capacity_snapshot`; `RepoNotFound` → 422 `repo_not_found`; `BranchNotFound` → 422 `branch_not_found`; `GitHubAuthError` → 409 `github_token_invalid`; other `GitHubError` → 502 `github_unavailable`. Reject → status `rejected`, reservations null; admit/queue → status `queued`, reservation = estimate. Response 201 `{"task": Task, "preflight": PreflightResult}`. No task row and no audit row on 4xx/5xx.
- `swf.api.main.create_app_from_env` wires `RegistryClient()` + `PreflightService`.

## 7. Tests (test writer)

- `pyproject.toml` `[tool.pytest.ini_options]` registers markers `docker` (needs Docker; auto-skipped with a reported reason when `docker info` fails) and `network` (needs internet; skipped when `SWF_SKIP_NETWORK=1`).
- `conftest.py`: migrate once into a `swf_template` database; session `db` uses a clone; function fixture `fresh_db` (clone, dropped afterwards) for capacity-sensitive tests; `registry` fixture (`FakeRegistry` with `python:3.12-slim` multi-arch on `registry-1.docker.io`); `app_deps` with a real `PreflightService` over `FakeGitHub` + `FakeRegistry`; session `egress` fixture (`EgressConfig(network="swf-test-sandbox", proxy_name="swf-test-egress")`, `teardown_egress` at the end).
- M1 API tests keep passing: a module-level `make_user` override in `test_api.py` links a GitHub token by raw SQL (no audit row), `linked=True` by default; tests that need an unlinked user pass `linked=False`. Task status after create is now `queued`; with the shared DB the decision may be `admit` or `queue`.
- New: `test_sandbox_unit.py` (run args, spec validation, squid conf), `test_sandbox_docker.py` (hardening via `docker inspect`, uid 1000, no host token/env in the container, OOM at a 128 MB limit, pids cap, orphan cleanup, sync cleanup, internal network, egress allow/deny incl. IP literal, plain HTTP, direct route, DNS, `ensure_egress` idempotent + recreate on config change), `test_seeded_repos_container.py` (all 10 seeds: setup via proxy, target fails with exit 1, rest passes, reference fix → all pass), API pre-flight integration (`fresh_db`), `capacity_snapshot` DB test, `github` additions (mocked).

## 8. Working rules for all agents

- Work only in this worktree. Do **not** commit, push, stash, or touch `main`. LF endings (`newline="\n"` in Python).
- File ownership is exclusive: planner → `docs/` (except this file); coder → `src/`, `supabase/`, `seeded_repos/*/repo/AGENTS.md`; test writer → `tests/`, `pyproject.toml` `[tool.pytest.ini_options]` only. No `uv add`/dependency changes (report instead).
- Docker names: tests use `swf-test-*`; the coder's manual experiments use `swf-coder-*` and clean them up.
- Done markers (worktree root, untracked): coder writes `.m2-done-code`, test writer writes `.m2-done-tests`.

## 9. Amendments v2 (2026-11-20, from the planner's review) — these override §1–§8 where they differ

- **A1 Per-task network (security: tasks must not reach each other, spec §7).** `EgressConfig` drops `network`, gains `network_prefix: str = "swf-task-"`. `ensure_egress(cfg) -> EgressInfo(container_id: str)`: the proxy sits on the default bridge only. New `@dataclass(frozen=True) TaskNetwork(name: str, proxy_ip: str, proxy_url: str)`; `attach_task_network(cfg, task_id, *, docker="docker") -> TaskNetwork`: `docker network create --internal --label swf.managed=1 --label swf.task=<id> <prefix><id>` (reuse if it exists and is internal; exists but not internal → `EgressError`), `docker network connect` the proxy (already connected = fine), proxy IP on that network. `detach_task_network(cfg, task_id, *, docker="docker") -> None`: disconnect proxy, remove network; idempotent. `cleanup_orphans(active_task_ids, *, egress: EgressConfig | None = None, docker="docker") -> list[str]` also removes `swf.managed=1` networks of inactive tasks (disconnecting the proxy first when `egress` is given) and returns removed container ids and network names. `teardown_egress(cfg)` removes the proxy only. `SandboxSpec.network` becomes required (no default). Test: two sandboxes on two task networks cannot connect to each other (by IP).
- **A2 Registry blob redirects.** Docker Hub answers config-blob GETs with 307 to a CDN. For **blob** GETs only, follow `https` redirects (≤ 3) and drop `Authorization` when the host changes; manifests are never redirected. `FakeRegistry` gets a redirect case.
- **A3 Interpreter.** `SandboxEnv` passes `interpreter=["bash", "-c"]` (upstream `bash -lc` resets PATH to the login default and breaks venv/Go/Rust images). Test: the image's PATH survives.
- **A4 Admission counts queued tasks.** `Capacity(active: tuple[Reservation, ...], queued: tuple[Reservation, ...])`. demand = active + queued (FIFO). Admit iff `len(demand) < max_concurrent` and `Σmem(demand) + mem ≤ host_memory_mb` and `Σcpu(demand) + cpus ≤ host_cpus + 1e-9`; else queue with `queue_position = max(1, len(demand) + 1 - max_concurrent)` and `host_busy`. `capacity_snapshot` returns coalesced reservations for both. API capacity tests on `fresh_db` assert the exact decision.
- **A5 SandboxSpec hard checks (defence in depth at execution time).** `image` must parse with `parse_image_ref` and its registry must be in `ALLOWED_REGISTRIES`; `memory_mb ≤ 4096`, `cpus ≤ 1.0`; `workspace` must not contain `,` or `"` (they break `--mount` parsing). Violations → validation error.
- **A6 Proxy image + hardening.** Image `ubuntu/squid:6.6-24.04_edge@sha256:d9c34c5ab74c6dd41cf1070d9b4221aac072e67e983527ca2d41512958f77506`; conf-hash label = sha256 of `image + "\n" + conf`. Proxy container: `--cap-drop ALL --cap-add SETUID --cap-add SETGID --security-opt no-new-privileges --memory 256m --pids-limit 256` (coder verifies Squid starts; add the minimum extra caps if it does not and report them). `docker cp` from a `TemporaryDirectory`. "Name already in use" on create → re-inspect and reuse.
- **A7 Push access.** `PreflightService.check` raises `RepoNotWritable(PreflightInputError)` when `can_push` is false → API 422 `repo_not_writable`, no task row.
- **A8 Registry cache.** `RegistryClient(..., cache_ttl_seconds: float = 3600)` caches `platforms()` per image (0 disables) to protect the Docker Hub pull quota.
- **A9 Oversized AGENTS.md.** `GitHubFileTooLarge(GitHubError)` for > 1 MiB. `RepoFacts` gains `agents_md_error: str | None = None`; when set, `evaluate` rejects with `agents_md_invalid` and that message (no image check). `PreflightService` maps `GitHubFileTooLarge` to it.
- **A10 Test isolation.** Docker names in tests carry a per-session suffix (`swf-test-<8 hex>-…`); the `cleanup_orphans` test lists the currently managed task ids first and passes them as active, so it only removes its own victim. The Postgres template database name carries the same suffix.
- **Deferred:** record the branch head SHA at pre-flight and clone exactly it (M6); test that a sandbox cannot reach host services via the internal network's gateway on the Linux host (M8).
