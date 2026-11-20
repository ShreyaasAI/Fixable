"""Image reference parsing and the arm64 availability check (plan §7: pre-flight rejects images
without an arm64 variant; the production host is an Oracle Ampere arm64 VM).

The image name comes from the user's AGENTS.md, so the registry client is an SSRF surface: only
allow-listed public registries may be contacted, and only over https.
"""

from __future__ import annotations

import httpx
import pytest
from helpers import LIST_MT, FakeRegistry, index_manifest, single_manifest

from swf.images import (
    ImageNotFound,
    ImageRef,
    InvalidImageRef,
    RegistryClient,
    RegistryError,
    RegistryNotAllowed,
    parse_image_ref,
)

HUB = "registry-1.docker.io"
DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("python:3.12-slim", ImageRef(HUB, "library/python", "3.12-slim")),
        ("python", ImageRef(HUB, "library/python", "latest")),
        ("docker.io/library/python:3.12", ImageRef(HUB, "library/python", "3.12")),
        ("index.docker.io/library/python:3.12", ImageRef(HUB, "library/python", "3.12")),
        ("ubuntu/squid:6.6-24.04_edge", ImageRef(HUB, "ubuntu/squid", "6.6-24.04_edge")),
        ("ghcr.io/astral-sh/uv:0.5", ImageRef("ghcr.io", "astral-sh/uv", "0.5")),
        ("quay.io/org/sub/img:v1", ImageRef("quay.io", "org/sub/img", "v1")),
        (f"python@{DIGEST}", ImageRef(HUB, "library/python", DIGEST)),
        (f"python:3.12@{DIGEST}", ImageRef(HUB, "library/python", DIGEST)),
        ("localhost:5000/x:1", ImageRef("localhost:5000", "x", "1")),
    ],
)
def test_parse_image_ref(ref: str, expected: ImageRef) -> None:
    assert parse_image_ref(ref) == expected


@pytest.mark.parametrize(
    "ref",
    ["", " ", "Python:3", "a b", "python:", ":tag", "http://x/y", "python@sha256:short", "x" * 300],
)
def test_parse_rejects_invalid(ref: str) -> None:
    with pytest.raises(InvalidImageRef):
        parse_image_ref(ref)


def _registry() -> FakeRegistry:
    reg = FakeRegistry()
    reg.manifests[(HUB, "library/python", "3.12-slim")] = index_manifest(
        "linux/amd64", "linux/arm64/v8", "unknown/unknown"
    )
    reg.manifests[(HUB, "library/amd-only", "1")] = index_manifest(
        "linux/amd64", "unknown/unknown", media_type=LIST_MT
    )
    reg.manifests[(HUB, "library/single", "arm")] = single_manifest("sha256:" + "1" * 64)
    reg.blobs[(HUB, "library/single", "sha256:" + "1" * 64)] = {
        "architecture": "arm64",
        "os": "linux",
    }
    reg.manifests[(HUB, "library/single", "amd")] = single_manifest("sha256:" + "2" * 64)
    reg.blobs[(HUB, "library/single", "sha256:" + "2" * 64)] = {
        "architecture": "amd64",
        "os": "linux",
    }
    return reg


def test_multi_arch_index_with_anonymous_token() -> None:
    reg = _registry()
    client = RegistryClient(transport=reg.transport())
    assert client.platforms("python:3.12-slim") == frozenset({"linux/amd64", "linux/arm64"})
    assert client.supports("python:3.12-slim", "linux/arm64")
    # 401 challenge -> anonymous token -> retried with Bearer
    first, token_req, retry = reg.requests[:3]
    assert "authorization" not in first.headers
    assert token_req.url.host == "auth.registry.test"
    assert token_req.url.params["scope"] == "repository:library/python:pull"
    assert retry.headers["authorization"] == "Bearer anon-token"


def test_manifest_list_without_arm64() -> None:
    client = RegistryClient(transport=_registry().transport())
    assert not client.supports("amd-only:1", "linux/arm64")


def test_single_manifest_uses_config_architecture() -> None:
    client = RegistryClient(transport=_registry().transport())
    assert client.supports("single:arm", "linux/arm64")
    assert not client.supports("single:amd", "linux/arm64")


def test_missing_image() -> None:
    with pytest.raises(ImageNotFound):
        RegistryClient(transport=_registry().transport()).platforms("python:9.9-nope")


@pytest.mark.parametrize(
    "ref", ["169.254.169.254/latest:x", "localhost:5000/x:1", "10.0.0.5/a/b:c", "evil.example/x"]
)
def test_registry_allowlist_blocks_ssrf(ref: str) -> None:
    reg = _registry()
    with pytest.raises(RegistryNotAllowed):
        RegistryClient(transport=reg.transport()).platforms(ref)
    assert reg.requests == []


def test_token_realm_must_be_https() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, headers={"WWW-Authenticate": 'Bearer realm="http://169.254.169.254/token"'}
        )

    client = RegistryClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RegistryError):
        client.platforms("python:3.12-slim")


def test_registry_server_error() -> None:
    client = RegistryClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    with pytest.raises(RegistryError):
        client.platforms("python:3.12-slim")


# --- blob redirects (Docker Hub serves config blobs from a CDN) ----------------------------

SINGLE_DIGEST = "sha256:" + "1" * 64


def _redirecting_registry() -> FakeRegistry:
    reg = _registry()
    reg.redirect_blobs.add((HUB, "library/single", SINGLE_DIGEST))
    return reg


def test_blob_redirect_is_followed_without_the_registry_token() -> None:
    reg = _redirecting_registry()
    assert RegistryClient(transport=reg.transport()).supports("single:arm", "linux/arm64")
    blob_hops = [r for r in reg.requests if "/blobs/" in r.url.path]
    registry_hop = next(r for r in blob_hops if r.url.host == HUB)
    cdn_hop = next(r for r in blob_hops if r.url.host == reg.cdn_host)
    assert registry_hop.headers["authorization"] == "Bearer anon-token"
    assert "authorization" not in cdn_hop.headers  # never leak the token to another host


def test_blob_redirect_to_plain_http_is_refused() -> None:
    reg = _redirecting_registry()
    reg.cdn_location_override = f"http://{reg.cdn_host}/blobs/{SINGLE_DIGEST}"
    with pytest.raises(RegistryError):
        RegistryClient(transport=reg.transport()).platforms("single:arm")
    assert all(r.url.scheme == "https" for r in reg.requests)


def test_blob_redirect_chain_is_capped() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "auth.registry.test":
            return httpx.Response(200, json={"token": "t"})
        if request.url.host == "cdn.registry.test":
            hops = len([r for r in seen if r.url.host == "cdn.registry.test"])
            return httpx.Response(307, headers={"Location": f"https://cdn.registry.test/{hops}"})
        if request.headers.get("authorization") != "Bearer t":
            challenge = 'Bearer realm="https://auth.registry.test/token",service="x"'
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        if "/manifests/" in request.url.path:
            return httpx.Response(
                200,
                json=single_manifest(SINGLE_DIGEST),
                headers={"Content-Type": "application/vnd.docker.distribution.manifest.v2+json"},
            )
        return httpx.Response(307, headers={"Location": "https://cdn.registry.test/0"})

    client = RegistryClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RegistryError):
        client.platforms("single:arm")
    assert len([r for r in seen if r.url.host == "cdn.registry.test"]) <= 4


def test_manifest_redirects_are_never_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.example":
            raise AssertionError("followed a manifest redirect to another host")
        if request.url.host == "auth.registry.test":
            return httpx.Response(200, json={"token": "t"})
        if request.headers.get("authorization") != "Bearer t":
            challenge = 'Bearer realm="https://auth.registry.test/token",service="x"'
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        return httpx.Response(307, headers={"Location": "https://evil.example/manifest"})

    client = RegistryClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RegistryError):
        client.platforms("python:3.12-slim")


# --- cache (protects the Docker Hub anonymous pull quota) ----------------------------------


def test_platforms_are_cached_per_image() -> None:
    reg = _registry()
    client = RegistryClient(transport=reg.transport())
    first = client.platforms("python:3.12-slim")
    made = len(reg.requests)
    assert made > 0
    assert client.platforms("python:3.12-slim") == first
    assert client.supports("python:3.12-slim", "linux/arm64")
    assert len(reg.requests) == made  # no further requests
    client.platforms("single:arm")  # a different image is not served from the cache
    assert len(reg.requests) > made


def test_cache_ttl_zero_disables_the_cache() -> None:
    reg = _registry()
    client = RegistryClient(transport=reg.transport(), cache_ttl_seconds=0)
    client.platforms("python:3.12-slim")
    made = len(reg.requests)
    client.platforms("python:3.12-slim")
    assert len(reg.requests) > made


def test_failed_lookups_are_not_cached() -> None:
    reg = _registry()
    client = RegistryClient(transport=reg.transport())
    with pytest.raises(ImageNotFound):
        client.platforms("later:1")
    reg.manifests[(HUB, "library/later", "1")] = index_manifest("linux/arm64")
    assert client.platforms("later:1") == frozenset({"linux/arm64"})


@pytest.mark.network
@pytest.mark.parametrize("image", ["python:3.12-slim", "ubuntu/squid:6.6-24.04_edge"])
def test_live_docker_hub_images_have_arm64(image: str) -> None:
    assert RegistryClient().supports(image, "linux/arm64")
