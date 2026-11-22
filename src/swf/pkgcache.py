"""Stdlib-only PyPI pull-through cache (M9 §0.6, §1.3).

Runs inside its own container as `python -I /app/pkgcache.py`, so it imports nothing from `swf`.
Index pages are rebuilt from PyPI; a file is served only if a fresh index listed its path, and
its bytes must match that index's sha256. Clients get GET and HEAD only.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

UPSTREAM_INDEX = "https://pypi.org/simple/"
UPSTREAM_FILES = "https://files.pythonhosted.org/packages/"
TTL_SECONDS = 86_400
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_CACHE_BYTES = 5 * 1024**3
MAX_INDEX_BYTES = 32 * 1024 * 1024
CHUNK = 64 * 1024

_NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,198}[a-z0-9])?")
_PATH_RE = re.compile(r"[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{60}/[A-Za-z0-9][A-Za-z0-9._+-]{0,250}")
_SHA_FRAG_RE = re.compile(r"sha256=([0-9a-f]{64})")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_TMP_PREFIX = ".tmp-"


class HashMismatch(Exception):
    pass


def normalize_name(name: str) -> str:
    out = re.sub(r"[-_.]+", "-", name).lower()
    if not _NAME_RE.fullmatch(out):
        raise ValueError("invalid project name")
    return out


def valid_file_path(path: str) -> bool:
    return _PATH_RE.fullmatch(path) is not None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.links.append(dict(attrs))


def rebuild_index(html_text: str) -> tuple[str, dict[str, str]]:
    parser = _LinkParser()
    parser.feed(html_text)
    parser.close()
    lines = ["<!DOCTYPE html><html><body>\n"]
    hashes: dict[str, str] = {}
    for attrs in parser.links:
        href = attrs.get("href") or ""
        if not href.startswith(UPSTREAM_FILES):
            continue
        path, sep, frag = href[len(UPSTREAM_FILES) :].partition("#")
        m = _SHA_FRAG_RE.fullmatch(frag)
        if not sep or m is None or not valid_file_path(path) or path in hashes:
            continue
        sha = m.group(1)
        hashes[path] = sha
        extra = ""
        rp = attrs.get("data-requires-python")
        if rp is not None:
            extra += f' data-requires-python="{html.escape(rp, quote=True)}"'
        if "data-yanked" in attrs:
            extra += f' data-yanked="{html.escape(attrs["data-yanked"] or "", quote=True)}"'
        filename = html.escape(path.rsplit("/", 1)[1], quote=True)
        lines.append(f'<a href="/files/{path}#sha256={sha}"{extra}>{filename}</a><br/>\n')
    lines.append("</body></html>\n")
    return "".join(lines), hashes


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Store:
    """Disk layout under root: index/<key>.json, sha/<key>, files/<key> (key = sha256 of name/path).

    The stored time of every entry is its mtime, set from `clock`.
    """

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], float] = time.time,
        ttl: float = TTL_SECONDS,
        max_bytes: int = MAX_CACHE_BYTES,
    ) -> None:
        self.root = Path(root)
        self.clock = clock
        self.ttl = ttl
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._dirs = {d: self.root / d for d in ("index", "sha", "files")}
        for d in self._dirs.values():
            d.mkdir(parents=True, exist_ok=True)

    def _fresh(self, p: Path) -> bool:
        try:
            return self.clock() - p.stat().st_mtime <= self.ttl
        except OSError:
            return False

    def _write(self, final: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(prefix=_TMP_PREFIX, dir=final.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            now = self.clock()
            os.utime(tmp, (now, now))
            os.replace(tmp, final)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def get_index(self, name: str) -> str | None:
        p = self._dirs["index"] / f"{_key(name)}.json"
        if not self._fresh(p):
            return None
        try:
            page = json.loads(p.read_text(encoding="utf-8"))["page"]
        except (OSError, ValueError, KeyError):
            return None
        return page if isinstance(page, str) else None

    def put_index(self, name: str, page: str, hashes: Mapping[str, str]) -> None:
        for path, sha in hashes.items():
            if not valid_file_path(path) or not _SHA_RE.fullmatch(sha):
                raise ValueError("invalid index entry")
        for path, sha in hashes.items():
            self._write(self._dirs["sha"] / _key(path), sha.encode("ascii"))
        body = json.dumps({"name": name, "page": page}).encode("utf-8")
        self._write(self._dirs["index"] / f"{_key(name)}.json", body)

    def expected_sha(self, path: str) -> str | None:
        if not valid_file_path(path):
            return None
        p = self._dirs["sha"] / _key(path)
        if not self._fresh(p):
            return None
        try:
            sha = p.read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            return None
        return sha if _SHA_RE.fullmatch(sha) else None

    def _file(self, path: str) -> Path:
        if not valid_file_path(path):
            raise ValueError("invalid file path")
        return self._dirs["files"] / _key(path)

    def get_file(self, path: str) -> Path | None:
        if not valid_file_path(path):
            return None
        p = self._file(path)
        return p if self._fresh(p) else None

    def put_file(self, path: str, chunks: Iterable[bytes], sha256: str) -> Path:
        final = self._file(path)
        if not _SHA_RE.fullmatch(sha256):
            raise ValueError("invalid sha256")
        fd, tmp = tempfile.mkstemp(prefix=_TMP_PREFIX, dir=final.parent)
        try:
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(fd, "wb") as f:
                for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise ValueError("file too large")
                    digest.update(chunk)
                    f.write(chunk)
            if digest.hexdigest() != sha256:
                raise HashMismatch(path)
            now = self.clock()
            os.utime(tmp, (now, now))
            with self._lock:
                os.replace(tmp, final)
                self._evict(keep=final)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return final

    def _stored_files(self) -> list[tuple[float, str, int, Path]]:
        out = []
        for p in self._dirs["files"].iterdir():
            if _SHA_RE.fullmatch(p.name):
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append((st.st_mtime, p.name, st.st_size, p))
        return out

    def _evict(self, keep: Path) -> None:
        files = sorted(self._stored_files())
        total = sum(f[2] for f in files)
        for _, _, size, p in files:
            if total <= self.max_bytes:
                break
            if p == keep:
                continue
            try:
                p.unlink()
            except OSError:
                continue
            total -= size

    def total_bytes(self) -> int:
        return sum(f[2] for f in self._stored_files())

    def sweep(self) -> int:
        removed = 0
        for d in self._dirs.values():
            for p in d.iterdir():
                if p.name.startswith(_TMP_PREFIX) or not self._fresh(p):
                    try:
                        p.unlink()
                    except OSError:
                        continue
                    removed += 1
        return removed


class Fetcher(Protocol):
    def get(self, url: str) -> Iterator[bytes]: ...


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise urllib.error.HTTPError(newurl, code, "off-host redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibFetcher:
    """HTTPS GET via the env proxy (HTTPS_PROXY); timeout 60 s; same-host redirects only."""

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler(), _SameHostRedirect)

    def get(self, url: str) -> Iterator[bytes]:
        if not (url.startswith(UPSTREAM_INDEX) or url.startswith(UPSTREAM_FILES)):
            raise OSError("upstream url not allowed")
        headers = {"User-Agent": "swf-pkgcache"}
        if url.startswith(UPSTREAM_INDEX):
            headers["Accept"] = "text/html"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                while chunk := resp.read(CHUNK):
                    yield chunk
        except http.client.HTTPException as e:
            raise OSError(f"upstream error: {type(e).__name__}") from e


def _read_limited(chunks: Iterable[bytes], limit: int) -> bytes:
    buf = bytearray()
    for chunk in chunks:
        buf += chunk
        if len(buf) > limit:
            raise ValueError("upstream response too large")
    return bytes(buf)


def make_handler(store: Store, fetcher: Fetcher) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "swf-pkgcache"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            pass  # one line per request comes from _log; headers are never logged

        def _log(self, status: int, cache: str) -> None:
            path = urllib.parse.urlsplit(self.path).path[:300]
            sys.stderr.write(f"{self.command} {path!r} {status} {cache}\n")
            sys.stderr.flush()

        def _plain(
            self, status: int, cache: str = "-", headers: Mapping[str, str] | None = None
        ) -> None:
            body = f"{status} {http.HTTPStatus(status).phrase}\n".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            self._log(status, cache)

        def __getattr__(self, name: str) -> Any:
            if name.startswith("do_"):
                return self._not_allowed
            raise AttributeError(name)

        def _not_allowed(self) -> None:
            self.close_connection = True
            self._plain(405, headers={"Allow": "GET, HEAD"})

        def do_GET(self) -> None:
            self._route()

        def do_HEAD(self) -> None:
            self._route()

        def _route(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if m := re.fullmatch(r"/simple/([^/]+)/", path):
                self._index(m.group(1))
            elif path.startswith("/files/"):
                self._file(path[len("/files/") :])
            else:
                self._plain(404)

        def _index(self, raw: str) -> None:
            try:
                name = normalize_name(raw)
            except ValueError:
                self._plain(404)
                return
            if name != raw:
                self._plain(301, headers={"Location": f"/simple/{name}/"})
                return
            page = store.get_index(name)
            cache = "hit"
            if page is None:
                cache = "miss"
                try:
                    text = _read_limited(fetcher.get(UPSTREAM_INDEX + name + "/"), MAX_INDEX_BYTES)
                    page, hashes = rebuild_index(text.decode("utf-8", errors="replace"))
                    store.put_index(name, page, hashes)
                except (OSError, ValueError):
                    self._plain(502, cache)
                    return
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            self._log(200, cache)

        def _file(self, path: str) -> None:
            if not valid_file_path(path):
                self._plain(404)
                return
            sha = store.expected_sha(path)
            if sha is None:
                self._plain(404)
                return
            local = store.get_file(path)
            cache = "hit"
            if local is None:
                cache = "miss"
                try:
                    local = store.put_file(path, fetcher.get(UPSTREAM_FILES + path), sha)
                except (OSError, ValueError, HashMismatch):
                    self._plain(502, cache)
                    return
            try:
                f = local.open("rb")
            except OSError:
                self._plain(502, cache)
                return
            with f:
                size = os.fstat(f.fileno()).st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                if self.command != "HEAD":
                    while chunk := f.read(CHUNK):
                        self.wfile.write(chunk)
            self._log(200, cache)

    return Handler


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="SWF PyPI pull-through cache")
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--port", type=int, default=3141)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args(argv)
    store = Store(args.root)
    store.sweep()
    server = http.server.ThreadingHTTPServer(
        (args.bind, args.port), make_handler(store, UrllibFetcher())
    )
    print(f"pkgcache listening on {args.bind}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
