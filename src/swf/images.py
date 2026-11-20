"""Container image references and the arm64 availability check (pre-flight, plan M2).

The production host is an arm64 VM, so pre-flight rejects images without a `linux/arm64`
variant before any container starts. The image name comes from the user's `AGENTS.md`, which
makes the registry client an SSRF surface:
- only allow-listed public registries are contacted, checked before any request;
- only https; manifests are never redirected; blob GETs follow ≤ 3 https redirects (Docker Hub
  sends them to a CDN) and the registry token is dropped once the host changes;
- the token realm from the `WWW-Authenticate` challenge must be https too.
Successful lookups are cached per image (`cache_ttl_seconds`, 0 disables).
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

DOCKER_HUB = "registry-1.docker.io"
_HUB_ALIASES = frozenset({"docker.io", "index.docker.io", DOCKER_HUB})

ALLOWED_REGISTRIES: frozenset[str] = frozenset(
    {
        DOCKER_HUB,
        "ghcr.io",
        "quay.io",
        "public.ecr.aws",
        "mcr.microsoft.com",
        "registry.gitlab.com",
    }
)

MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

_MAX_REF_LEN = 255
_MAX_REDIRECTS = 3
_COMPONENT_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_DIGEST_RE = re.compile(r"sha256:[a-f0-9]{64}")
_REGISTRY_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?")
_CHALLENGE_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')


class ImageError(Exception):
    pass


class InvalidImageRef(ImageError):
    pass


class RegistryNotAllowed(ImageError):
    pass


class ImageNotFound(ImageError):
    pass


class RegistryError(ImageError):
    pass


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    reference: str  # tag or sha256 digest


def parse_image_ref(ref: str) -> ImageRef:
    """Docker reference grammar, restricted to what pre-flight needs (no defaults beyond Hub)."""
    if not ref or len(ref) > _MAX_REF_LEN:
        raise InvalidImageRef(f"invalid image reference: {ref[:80]!r}")
    name, digest = ref, None
    if "@" in ref:
        name, digest = ref.split("@", 1)
        if not _DIGEST_RE.fullmatch(digest):
            raise InvalidImageRef(f"invalid image digest in {ref!r}")
    tag = None
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        name, tag = name[:colon], name[colon + 1 :]
        if not _TAG_RE.fullmatch(tag):
            raise InvalidImageRef(f"invalid image tag in {ref!r}")
    parts = name.split("/")
    first = parts[0]
    if len(parts) > 1 and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, parts[1:]
        if not _REGISTRY_RE.fullmatch(registry):
            raise InvalidImageRef(f"invalid registry in {ref!r}")
    else:
        registry, path = DOCKER_HUB, parts
    if registry in _HUB_ALIASES:
        registry = DOCKER_HUB
        if len(path) == 1:
            path = ["library", *path]
    if not path or not all(_COMPONENT_RE.fullmatch(p) for p in path):
        raise InvalidImageRef(f"invalid image repository in {ref!r}")
    return ImageRef(registry, "/".join(path), digest or tag or "latest")


def _challenge(resp: httpx.Response) -> dict[str, str]:
    header = resp.headers.get("WWW-Authenticate", "")
    scheme, _, params = header.partition(" ")
    if scheme.lower() != "bearer":
        raise RegistryError(f"registry {resp.request.url.host} wants unsupported auth")
    return dict(_CHALLENGE_PARAM_RE.findall(params))


class RegistryClient:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        allowed_registries: frozenset[str] = ALLOWED_REGISTRIES,
        timeout_seconds: float = 15.0,
        cache_ttl_seconds: float = 3600,
    ) -> None:
        self._transport = transport
        self._allowed = allowed_registries
        self._timeout = timeout_seconds
        # Successful lookups only, per image string: protects the anonymous Docker Hub quota.
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, frozenset[str]]] = {}
        self._lock = threading.Lock()

    def supports(self, image: str, platform: str = "linux/arm64") -> bool:
        return platform in self.platforms(image)

    def platforms(self, image: str) -> frozenset[str]:
        """`os/arch` pairs the image is published for (variant dropped, `unknown` skipped)."""
        ref = parse_image_ref(image)
        if ref.registry not in self._allowed:
            raise RegistryNotAllowed(f"registry {ref.registry} is not allowed")
        if self._cache_ttl > 0:
            with self._lock:
                hit = self._cache.get(image)
            if hit is not None and time.monotonic() - hit[0] < self._cache_ttl:
                return hit[1]
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
                headers={"User-Agent": "swf-agent"},
            ) as client:
                found = self._platforms(client, ref)
        except httpx.HTTPError as exc:
            raise RegistryError(f"registry {ref.registry} unreachable: {exc}") from exc
        if self._cache_ttl > 0:
            with self._lock:
                self._cache[image] = (time.monotonic(), found)
        return found

    def _platforms(self, client: httpx.Client, ref: ImageRef) -> frozenset[str]:
        base = f"https://{ref.registry}/v2/{ref.repository}"
        token: str | None = None
        url = f"{base}/manifests/{ref.reference}"
        resp = self._get(client, url, token, accept=MANIFEST_ACCEPT)
        if resp.status_code == 401:
            token = self._token(client, resp)
            resp = self._get(client, url, token, accept=MANIFEST_ACCEPT)
        manifest = self._json(resp, ref)
        if isinstance(manifest.get("manifests"), list):
            found: set[str] = set()
            for entry in manifest["manifests"]:
                plat = entry.get("platform") if isinstance(entry, dict) else None
                if isinstance(plat, dict):
                    os_, arch = plat.get("os"), plat.get("architecture")
                    if isinstance(os_, str) and isinstance(arch, str) and os_ != "unknown":
                        found.add(f"{os_}/{arch}")
            return frozenset(found)
        config = manifest.get("config")
        digest = config.get("digest") if isinstance(config, dict) else None
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise RegistryError(f"unrecognised manifest for {ref.repository}:{ref.reference}")
        blob_resp = self._get(client, f"{base}/blobs/{digest}", token, follow_redirects=True)
        blob = self._json(blob_resp, ref)
        os_, arch = blob.get("os"), blob.get("architecture")
        if not isinstance(os_, str) or not isinstance(arch, str):
            raise RegistryError(f"image config for {ref.repository} lacks os/architecture")
        return frozenset() if os_ == "unknown" else frozenset({f"{os_}/{arch}"})

    def _get(
        self,
        client: httpx.Client,
        url: str,
        token: str | None,
        *,
        accept: str | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        """GET; with `follow_redirects` (blobs only) follow ≤ 3 https redirects.

        Docker Hub answers blob GETs with a 307 to a CDN. The token is sent only while the
        request is still on the registry host; once redirected elsewhere it is never sent again.
        Manifests are never redirected: a 3xx there is a registry error.
        """
        target = httpx.URL(url)
        origin = target.host
        send_token = token is not None
        for _ in range(_MAX_REDIRECTS + 1):
            headers: dict[str, str] = {}
            if accept:
                headers["Accept"] = accept
            if send_token and target.host != origin:
                send_token = False
            if send_token:
                headers["Authorization"] = f"Bearer {token}"
            resp = client.get(target, headers=headers)
            if not resp.is_redirect:
                return resp
            location = resp.headers.get("Location")
            if not follow_redirects or not location:
                raise RegistryError(f"unexpected redirect from registry for {target.path}")
            target = target.join(location)
            if target.scheme != "https" or not target.host:
                raise RegistryError("registry redirected to a non-https URL")
        raise RegistryError("too many redirects from registry")

    def _token(self, client: httpx.Client, challenge_resp: httpx.Response) -> str:
        params = _challenge(challenge_resp)
        realm = httpx.URL(params.get("realm", ""))
        if realm.scheme != "https" or not realm.host:
            raise RegistryError("registry token realm must be an https URL")
        query: dict[str, str] = {k: params[k] for k in ("service", "scope") if k in params}
        resp = client.get(realm.copy_merge_params(query))
        if resp.status_code >= 400:
            raise RegistryError(f"registry token request failed ({resp.status_code})")
        try:
            data = resp.json()
        except ValueError as exc:
            raise RegistryError("registry token response is not JSON") from exc
        token = (data.get("token") or data.get("access_token")) if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise RegistryError("registry token response has no token")
        return token

    @staticmethod
    def _json(resp: httpx.Response, ref: ImageRef) -> dict[str, Any]:
        name = f"{ref.registry}/{ref.repository}:{ref.reference}"
        if resp.status_code == 404:
            raise ImageNotFound(f"image {name} not found")
        if resp.status_code == 401:
            raise RegistryError(
                f"registry refused anonymous access to {name} (private or does not exist)"
            )
        if resp.status_code >= 400:
            raise RegistryError(f"registry error {resp.status_code} for {name}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise RegistryError(f"registry returned invalid JSON for {name}") from exc
        if not isinstance(data, dict):
            raise RegistryError(f"registry returned unexpected JSON for {name}")
        return data
