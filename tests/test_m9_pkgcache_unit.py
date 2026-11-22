"""M9 T3: unit tests for swf.pkgcache (no network, no Docker)."""

from __future__ import annotations

import ast
import hashlib
import http.client
import sys
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from swf import pkgcache

FIXTURE = Path(__file__).parent / "fixtures" / "pypi_simple_pytest.html"
BASE = "https://files.pythonhosted.org/packages/"


def _path(
    a: str = "aa", b: str = "bb", c: str = "1", name: str = "pkg-1.0-py3-none-any.whl"
) -> str:
    return f"{a}/{b}/{c * 60}/{name}"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# 1


def test_normalize_name() -> None:
    assert pkgcache.normalize_name("Py_Test.Thing") == "py-test-thing"
    assert pkgcache.normalize_name("pytest") == "pytest"
    for bad in ("", "../x", "a/b", "-x"):
        with pytest.raises(ValueError):
            pkgcache.normalize_name(bad)


# 2


def test_valid_file_path() -> None:
    assert pkgcache.valid_file_path(_path())
    assert pkgcache.valid_file_path(_path(name="a_b-1.0+local.tar.gz"))
    bad = [
        "../etc/passwd",
        f"aa/bb/{'1' * 60}/",
        f"aa/bb/{'1' * 59}/x.whl",
        f"aa/bb/{'1' * 60}/..",
        f"aa/bb/{'1' * 60}/a/b.whl",
        f"aa/bb/{'1' * 60}/.hidden",
        f"AA/bb/{'1' * 60}/x.whl",
        f"/aa/bb/{'1' * 60}/x.whl",
    ]
    for p in bad:
        assert not pkgcache.valid_file_path(p), p


# 3


def test_rebuild_index_keeps_only_hashed_pythonhosted_links() -> None:
    page, hashes = pkgcache.rebuild_index(FIXTURE.read_text(encoding="utf-8"))
    assert hashes == {
        _path("aa", "bb", "1", "pytest-8.0.0-py3-none-any.whl"): "a" * 64,
        _path("cc", "dd", "2", "pytest-8.1.0-py3-none-any.whl"): "b" * 64,
        _path("ee", "ff", "3", "pytest-7.0.0-py3-none-any.whl"): "c" * 64,
    }
    assert page.count("<a ") == 3
    for path, sha in hashes.items():
        assert f'href="/files/{path}#sha256={sha}"' in page
    assert page.count("data-requires-python=") == 3
    assert "&gt;=3.9" in page
    assert 'data-yanked="broken &amp; bad"' in page
    for gone in (
        "data-dist-info-metadata",
        "data-core-metadata",
        "pypi.org",
        "pythonhosted.org",
        "evil.example.com",
        "pytest-6.0.0.tar.gz",
        "pytest-5.0.0.tar.gz",
        "pytest-9.0.0.tar.gz",
    ):
        assert gone not in page, gone
    assert page.startswith("<!DOCTYPE html><html><body>\n")
    assert page.endswith("</body></html>\n")


# 4


def test_store_ttl_and_sweep(tmp_path: Path) -> None:
    c = Clock()
    store = pkgcache.Store(tmp_path, clock=c)
    path = _path()
    store.put_index("pytest", "<html>page</html>", {path: "a" * 64})
    assert store.get_index("pytest") == "<html>page</html>"
    assert store.expected_sha(path) == "a" * 64
    c.now += 86_400 - 1
    assert store.get_index("pytest") is not None
    c.now += 2  # 86_401 s after storing
    assert store.get_index("pytest") is None
    assert store.expected_sha(path) is None
    assert store.sweep() > 0
    assert store.get_index("pytest") is None
    assert store.sweep() == 0


def test_store_file_ttl(tmp_path: Path) -> None:
    c = Clock()
    store = pkgcache.Store(tmp_path, clock=c)
    data = b"wheel-bytes"
    path = _path()
    store.put_file(path, [data], hashlib.sha256(data).hexdigest())
    assert store.get_file(path) is not None
    c.now += 86_401
    assert store.get_file(path) is None
    assert store.sweep() > 0
    assert store.total_bytes() == 0


# 4b


def _put(store: pkgcache.Store, tag: str, data: bytes) -> str:
    path = _path(c=tag, name=f"f{tag}-1.0-py3-none-any.whl")
    store.put_file(path, [data], hashlib.sha256(data).hexdigest())
    return path


def test_store_size_cap_evicts_oldest_first(tmp_path: Path) -> None:
    c = Clock(1.0)
    store = pkgcache.Store(tmp_path, max_bytes=25, clock=c)
    p1 = _put(store, "1", b"a" * 10)
    c.now = 2.0
    p2 = _put(store, "2", b"b" * 10)
    c.now = 3.0
    p3 = _put(store, "3", b"c" * 10)
    assert store.get_file(p1) is None
    assert store.get_file(p2) is not None
    assert store.get_file(p3) is not None
    assert store.total_bytes() <= 25


def test_store_oversize_single_file_evicts_everything_older(tmp_path: Path) -> None:
    c = Clock(1.0)
    store = pkgcache.Store(tmp_path, max_bytes=25, clock=c)
    p1 = _put(store, "1", b"a" * 10)
    c.now = 2.0
    p2 = _put(store, "2", b"b" * 10)
    c.now = 3.0
    big = _put(store, "3", b"c" * 30)
    assert store.get_file(big) is not None
    assert store.get_file(p1) is None
    assert store.get_file(p2) is None


# 5


def test_put_file_ok_and_hash_mismatch(tmp_path: Path) -> None:
    store = pkgcache.Store(tmp_path)
    data = b"x" * 100
    path = _path()
    out = store.put_file(path, [data[:40], data[40:]], hashlib.sha256(data).hexdigest())
    assert out.is_file()
    assert out.read_bytes() == data
    assert store.get_file(path) == out

    other = _path(c="2")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    with pytest.raises(pkgcache.HashMismatch):
        store.put_file(other, [data], "0" * 64)
    assert store.get_file(other) is None
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert after == before  # no file, no temp file


def test_put_file_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pkgcache, "MAX_FILE_BYTES", 10)
    store = pkgcache.Store(tmp_path)
    data = b"y" * 11
    path = _path()
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    with pytest.raises(ValueError):
        store.put_file(path, [data], hashlib.sha256(data).hexdigest())
    assert store.get_file(path) is None
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert after == before


# 6


class FakeFetcher:
    def __init__(self, responses: dict[str, bytes | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> Iterator[bytes]:
        self.calls.append(url)
        r = self.responses.get(url)
        if r is None:
            raise OSError("404 upstream")
        if isinstance(r, Exception):
            raise r
        return iter([r[: len(r) // 2], r[len(r) // 2 :]])


WHEEL = b"PK-fake-wheel-bytes" * 50
WHEEL_PATH = _path("aa", "bb", "1", "pytest-8.0.0-py3-none-any.whl")
WHEEL_SHA = hashlib.sha256(WHEEL).hexdigest()
BAD_PATH = _path("cc", "dd", "2", "pytest-8.1.0-py3-none-any.whl")
UPSTREAM_PAGE = (
    "<!DOCTYPE html><html><body>\n"
    f'<a href="{BASE}{WHEEL_PATH}#sha256={WHEEL_SHA}"'
    ' data-requires-python="&gt;=3.9">x.whl</a><br/>\n'
    f'<a href="{BASE}{BAD_PATH}#sha256={"9" * 64}">y.whl</a><br/>\n'
    "</body></html>\n"
)


def _serve(store: pkgcache.Store, fetcher: FakeFetcher):
    server = ThreadingHTTPServer(("127.0.0.1", 0), pkgcache.make_handler(store, fetcher))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]

    def req(method: str, path: str) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path)
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)

    return req, stop


@pytest.fixture
def served(tmp_path: Path):
    fetcher = FakeFetcher(
        {
            pkgcache.UPSTREAM_INDEX + "pytest/": UPSTREAM_PAGE.encode(),
            BASE + WHEEL_PATH: WHEEL,
            BASE + BAD_PATH: b"not the bytes the index promised",
        }
    )
    req, stop = _serve(pkgcache.Store(tmp_path / "cache"), fetcher)
    try:
        yield req, fetcher
    finally:
        stop()


def test_handler_index_cached(served) -> None:
    req, fetcher = served
    for _ in range(2):
        status, headers, body = req("GET", "/simple/pytest/")
        assert status == 200
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert f"/files/{WHEEL_PATH}#sha256={WHEEL_SHA}".encode() in body
        assert b"pythonhosted.org" not in body
    assert fetcher.calls.count(pkgcache.UPSTREAM_INDEX + "pytest/") == 1


def test_handler_non_normalized_name_redirects(served) -> None:
    req, _ = served
    status, headers, _ = req("GET", "/simple/PyTest/")
    assert status == 301
    assert headers["location"] == "/simple/pytest/"


def test_handler_file_served_and_cached(served) -> None:
    req, fetcher = served
    req("GET", "/simple/pytest/")
    for _ in range(2):
        status, headers, body = req("GET", f"/files/{WHEEL_PATH}")
        assert status == 200
        assert headers["content-type"] == "application/octet-stream"
        assert body == WHEEL
    assert fetcher.calls.count(BASE + WHEEL_PATH) == 1


def test_handler_unknown_valid_path_is_404_without_upstream(served) -> None:
    req, fetcher = served
    req("GET", "/simple/pytest/")
    before = len(fetcher.calls)
    status, _, _ = req("GET", f"/files/{_path('ab', 'cd', '7', 'other-1.0.whl')}")
    assert status == 404
    assert len(fetcher.calls) == before


def test_handler_hash_mismatch_502_and_not_stored(served) -> None:
    req, fetcher = served
    req("GET", "/simple/pytest/")
    assert req("GET", f"/files/{BAD_PATH}")[0] == 502
    assert req("GET", f"/files/{BAD_PATH}")[0] == 502
    assert fetcher.calls.count(BASE + BAD_PATH) == 2  # refetched: nothing was stored


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
@pytest.mark.parametrize("path", ["/simple/pytest/", f"/files/{WHEEL_PATH}"])
def test_handler_other_methods_405(served, method: str, path: str) -> None:
    req, fetcher = served
    status, headers, _ = req(method, path)
    assert status == 405
    assert headers["allow"] == "GET, HEAD"
    assert fetcher.calls == []


@pytest.mark.parametrize("path", ["/", "/simple/", "/files/../../etc/passwd", "/admin"])
def test_handler_unknown_paths_404(served, path: str) -> None:
    req, fetcher = served
    assert req("GET", path)[0] == 404
    assert fetcher.calls == []


def test_handler_upstream_oserror_502(tmp_path: Path) -> None:
    fetcher = FakeFetcher({pkgcache.UPSTREAM_INDEX + "pytest/": OSError("boom")})
    req, stop = _serve(pkgcache.Store(tmp_path), fetcher)
    try:
        assert req("GET", "/simple/pytest/")[0] == 502
    finally:
        stop()


def test_handler_head_index_has_empty_body(served) -> None:
    req, _ = served
    status, headers, body = req("HEAD", "/simple/pytest/")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert body == b""


# 7


def test_pkgcache_imports_stdlib_only() -> None:
    src = Path(pkgcache.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            mods = [node.module or ""]
        else:
            continue
        for m in mods:
            top = m.split(".")[0]
            assert top != "swf", m
            assert top in sys.stdlib_module_names, m
