"""ArchiveBox's existing authenticated HTTP and embedded OpenCode interfaces."""

import asyncio
import base64
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import NamedTuple
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .config import Settings

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024


class Artifact(NamedTuple):
    data: bytes
    mimetype: str
    url: str


class ArchiveBox:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.api_url = settings.archivebox_url.rstrip("/")
        self.admin_url = (settings.archivebox_admin_url or settings.archivebox_url).rstrip("/")
        self._api = httpx.AsyncClient(timeout=60, follow_redirects=False)
        self._browser = httpx.AsyncClient(timeout=60, follow_redirects=False)
        self._cookie = ""
        self._cookie_expires = 0.0
        self._session_lock = asyncio.Lock()

    async def close(self):
        await self._api.aclose()
        await self._browser.aclose()

    @staticmethod
    def _response(response: httpx.Response):
        if not response.is_success:
            raise RuntimeError(f"ArchiveBox returned HTTP {response.status_code} for {response.request.url.path}")
        return response.json() if response.status_code != 204 else None

    async def _request(self, method: str, path: str, **kwargs):
        headers = {"Authorization": f"Bearer {self.settings.archivebox_token}"}
        response = await self._api.request(
            method,
            self.api_url + path,
            headers=headers,
            **kwargs,
        )
        if method == "GET" and response.is_redirect:
            configured = urlsplit(self.settings.archivebox_url)
            requested = urlsplit(str(response.request.url))
            target = urlsplit(urljoin(str(response.request.url), response.headers.get("location", "")))
            host = configured.hostname or ""
            base_host = host
            for prefix in ("api.", "admin.", "web."):
                if base_host.startswith(prefix):
                    base_host = base_host[len(prefix) :]
                    break
            default_port = 443 if configured.scheme == "https" else 80
            if (
                not target.username
                and not target.password
                and not target.fragment
                and target.scheme == configured.scheme
                and (target.port or default_port) == (configured.port or default_port)
                and target.hostname in {host, base_host, "api." + base_host, "admin." + base_host, "web." + base_host}
                and target.path == requested.path
                and target.query == requested.query
            ):
                # Discover only the canonical control host on a protected read;
                # mutations and arbitrary redirects are never replayed.
                self.api_url = f"{target.scheme}://{target.netloc}{configured.path.rstrip('/')}"
                self._api.cookies.clear()
                response = await self._api.request(method, self.api_url + path, headers=headers, **kwargs)
        return self._response(response)

    async def check(self) -> dict:
        # Checking a protected endpoint verifies administrator authorization too.
        started = time.perf_counter()
        page = await self.snapshots(limit=1)
        return {
            "ok": True,
            "snapshots": page["total_items"],
            "url": self.settings.archivebox_public_url,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "checked_at": time.time(),
        }

    async def add(self, urls: list[str], tags: list[str]) -> dict:
        result = await self._request(
            "POST",
            "/api/v1/cli/add",
            json={
                "urls": urls,
                "depth": 0,
                "only_new": False,
                "tag": ",".join(tags),
                "persona": self.settings.persona,
                "plugins": self.settings.plugins,
                "parser": "url_list",
            },
        )
        if not result.get("success") or not result.get("result", {}).get("crawl_id"):
            raise RuntimeError("ArchiveBox did not queue the submitted URLs")
        return result["result"]

    async def crawl(self, crawl_id: str) -> dict:
        return await self._request("GET", f"/api/v1/crawls/crawl/{quote(crawl_id, safe='')}")

    async def crawl_snapshots(self, crawl_id: str) -> list[dict]:
        response = await self._request(
            "POST",
            "/api/v1/cli/search",
            # Read the schema's hard submission ceiling, since the user may
            # reduce their preference while an existing crawl is in flight.
            json={"crawl_id": crawl_id, "as_json": True, "limit": 500},
        )
        if not response.get("success") or not isinstance(response.get("result"), list):
            raise RuntimeError("ArchiveBox did not return the crawl's snapshots")
        # The CLI export lacks persona and result manifests. Hydrate those via
        # the canonical detail schema; every read is bounded to submitted URLs.
        snapshots = []
        for row in response["result"]:
            snapshots.append(await self._request("GET", f"/api/v1/core/snapshot/{quote(str(row['id']), safe='')}"))
        return snapshots

    async def snapshots(self, **params) -> dict:
        page = await self._request("GET", "/api/v1/core/snapshots", params=params)
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise TypeError("ArchiveBox returned an invalid snapshot page")
        return page

    async def search(self, query: str) -> list[dict]:
        query = query.strip()
        if not query:
            return []
        matches, offset = [], 0
        # Server metadata search includes inherited Crawl.config values. Chat
        # results should match the page itself, and filtering must precede the
        # ten-result limit. Bound collection reads to 500 server candidates.
        while offset < 500 and len(matches) < 10:
            page = await self.snapshots(search=query, search_mode="meta", limit=100, offset=offset)
            for snapshot in page["items"]:
                values = [snapshot.get(field) or "" for field in ("url", "title", "notes", "id", "timestamp")]
                values.extend(snapshot.get("tags") or [])
                if any(query.casefold() in str(value).casefold() for value in values):
                    matches.append(snapshot)
                    if len(matches) == 10:
                        break
            offset += len(page["items"])
            if not page["items"] or offset >= page["total_items"]:
                break
        return matches

    def detail_url(self, snapshot: dict) -> str:
        path = self._safe_path(snapshot["archive_path"])
        return self.settings.archivebox_public_url.rstrip("/") + "/" + quote(path, safe="/")

    def session_url(self, session: dict) -> str:
        proxy = self.settings.archivebox_public_url.rstrip("/") + "/admin/agent/opencode"
        server_key = base64.urlsafe_b64encode(proxy.encode()).decode().rstrip("=")
        return f"{proxy}/server/{server_key}/session/{quote(session['id'], safe='')}"

    @staticmethod
    def _safe_path(value: str) -> str:
        value = str(value)
        if not value or value.startswith("/") or "\\" in value or any(char in value for char in "?#\r\n"):
            raise ValueError("Invalid ArchiveBox artifact path")
        if any(part in (".", "..") for part in value.split("/")):
            raise ValueError("Invalid ArchiveBox artifact path")
        return str(PurePosixPath(value))

    def _discover_admin(self, discovered: str) -> None:
        if self.settings.archivebox_admin_url or not discovered:
            return
        configured, target = urlsplit(self.api_url), urlsplit(discovered)
        if target.username or target.password or target.query or target.fragment:
            return
        host = configured.hostname or ""
        base_host = host
        for prefix in ("api.", "admin.", "web."):
            if base_host.startswith(prefix):
                base_host = base_host[len(prefix) :]
                break
        default_port = 443 if configured.scheme == "https" else 80
        if (
            target.scheme != configured.scheme
            or (target.port or default_port) != (configured.port or default_port)
            or target.hostname not in {host, base_host, "admin." + base_host}
        ):
            return
        path = target.path.rstrip("/")
        if not path.endswith("/admin"):
            return
        # Discovery may select the known administrator subdomain, but cannot
        # redirect credentials to an unrelated authority or mount path.
        mount = path[: -len("/admin")]
        if mount != configured.path.rstrip("/"):
            return
        self.admin_url = f"{target.scheme}://{target.netloc}{mount}"

    async def _browser_headers(self) -> dict[str, str]:
        async with self._session_lock:
            if not self._cookie or self._cookie_expires <= datetime.now(UTC).timestamp() + 30:
                session = await self._request("POST", "/api/v1/auth/browser_session", json={})
                self._discover_admin(session.get("admin_url", ""))
                cookie = session.get("cookie", {})
                name, value = str(cookie.get("name", "")), str(cookie.get("value", ""))
                if not name or not value or any(char in name + value for char in "\r\n;= "):
                    raise RuntimeError("ArchiveBox returned an invalid browser session")
                if cookie.get("secure") and urlsplit(self.admin_url).scheme != "https":
                    raise RuntimeError(
                        "ArchiveBox requires HTTPS for its administrator session; configure an HTTPS administrator URL"
                    )
                self._cookie = f"{name}={value}"
                self._cookie_expires = float(cookie["expires"])
                # Cookies only go to the configured server or the validated
                # administrator subdomain; HTTP redirects are never followed.
                self._api.cookies.clear()
        origin = urlsplit(self.admin_url)
        return {"Cookie": self._cookie, "Origin": f"{origin.scheme}://{origin.netloc}"}

    async def artifact(self, snapshot: dict, kind: str) -> Artifact | None:
        if kind not in {"screenshot", "favicon"}:
            raise ValueError("Choose screenshot or favicon")
        results = snapshot.get("archiveresults") or []
        if not results:
            snapshot = await self._request(
                "GET",
                f"/api/v1/core/snapshot/{quote(str(snapshot['id']), safe='')}",
                params={"with_archiveresults": "true"},
            )
            results = snapshot.get("archiveresults") or []
        plugin_names = {kind}
        if kind == "screenshot":
            plugin_names |= {"chrome_extension_screenshot", "chrome_extension_viewport"}
        candidates = []
        for result in results:
            plugin = result.get("plugin", "")
            if plugin not in plugin_names or result.get("status") != "succeeded":
                continue
            for output_path, metadata in (result.get("output_files") or {}).items():
                if not isinstance(metadata, dict):
                    continue
                path = self._safe_path(output_path)
                mimetype = str(metadata.get("mimetype") or "")
                suffix = PurePosixPath(path).suffix.lower()
                if not mimetype.startswith("image/") and suffix not in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".webp",
                    ".ico",
                    ".gif",
                }:
                    continue
                if metadata.get("root_relative"):
                    path = path.removeprefix(plugin + "/")
                elif not path.startswith(plugin + "/"):
                    path = plugin + "/" + path
                candidates.append((path, mimetype))
        if not candidates:
            return None
        # Every candidate is an actual successful output manifest entry.
        path, mimetype = min(candidates)
        headers = await self._browser_headers()
        # Detail aliases redirect outputs to isolated replay hosts. The stable
        # replay endpoint serves the declared file on the configured admin host,
        # keeping the administrator cookie out of redirect targets.
        snapshot_id = quote(str(snapshot["id"]), safe="")
        url = self.admin_url + f"/snapshot/{snapshot_id}/" + quote(path, safe="/")
        async with self._browser.stream("GET", url, headers=headers) as response:
            if response.status_code == 404:
                return None
            if not response.is_success:
                raise RuntimeError(f"ArchiveBox artifact returned HTTP {response.status_code}")
            length = response.headers.get("content-length")
            if length and int(length) > MAX_ARTIFACT_BYTES:
                raise ValueError("ArchiveBox artifact exceeds the 10 MiB upload limit")
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise ValueError("ArchiveBox artifact exceeds the 10 MiB upload limit")
                chunks.append(chunk)
            actual_type = response.headers.get("content-type", mimetype).split(";", 1)[0]
            if actual_type == "text/html":
                raise RuntimeError("ArchiveBox returned an HTML page instead of the requested image")
            public_url = (
                self.settings.archivebox_public_url.rstrip("/") + f"/snapshot/{snapshot_id}/" + quote(path, safe="/")
            )
            return Artifact(b"".join(chunks), actual_type or "application/octet-stream", public_url)

    async def _agent_request(self, method: str, path: str, **kwargs):
        headers = await self._browser_headers()
        response = await self._browser.request(
            method,
            self.admin_url + "/admin/agent/opencode/" + path.lstrip("/"),
            headers=headers,
            **kwargs,
        )
        return self._response(response)

    async def create_session(self, title: str) -> dict:
        workdir = (await self._agent_request("GET", "path"))["directory"]
        session = await self._agent_request("POST", "session", params={"directory": workdir}, json={"title": title})
        if not session.get("id") or session.get("directory") != workdir:
            raise RuntimeError("OpenCode did not create a session in the configured ArchiveBox collection")
        return session

    async def submit_session(self, session: dict, prompt: str):
        await self._agent_request(
            "POST",
            f"session/{quote(session['id'], safe='')}/prompt_async",
            params={"directory": session["directory"]},
            json={"parts": [{"type": "text", "text": prompt}]},
        )

    async def session_answer(self, session: dict) -> str | None:
        messages = await self._agent_request(
            "GET",
            f"session/{quote(session['id'], safe='')}/message",
            params={"directory": session["directory"], "limit": 10},
        )
        if not messages or messages[-1].get("info", {}).get("role") != "assistant":
            return None
        response = messages[-1]
        if response.get("info", {}).get("error"):
            error = response["info"]["error"]
            raise RuntimeError(f"OpenCode could not complete the request ({error.get('name', 'agent error')})")
        info = response["info"]
        if not info.get("time", {}).get("completed") or info.get("finish") in {None, "tool-calls", "unknown"}:
            return None
        texts = [part["text"] for part in response.get("parts", []) if part.get("type") == "text" and part.get("text")]
        if not texts:
            raise RuntimeError("OpenCode completed without a text response; inspect its session in ArchiveBox")
        return "\n\n".join(texts)

    async def prompt_session(self, session: dict, prompt: str) -> str:
        await self.submit_session(session, prompt)
        while (answer := await self.session_answer(session)) is None:
            await asyncio.sleep(1)
        return answer

    async def abort_session(self, session: dict):
        return await self._agent_request(
            "POST",
            f"session/{quote(session['id'], safe='')}/abort",
            params={"directory": session["directory"]},
            json={},
        )
