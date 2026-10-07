import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from slack_sdk.signature import SignatureVerifier

from .archivebox import ArchiveBox
from .config import SECRET_FIELDS, Settings
from .engine import Engine, safe_error
from .manifest import manifest
from .slack import Slack
from .store import Store
from .zulip import Zulip

STATIC = Path(__file__).parent / "static"


def create_app(directory=None):
    store = Store(directory or os.environ.get("DATA_DIR", "data"))
    engine = Engine(store)
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not store.meta("password_hash"):
        if not password:
            password = secrets.token_urlsafe(18)
            credential_path = store.directory / "admin-password"
            credential_path.write_text(password + "\n")
            credential_path.chmod(0o600)
        if len(password) < 12:
            raise ValueError("ADMIN_PASSWORD must be at least 12 characters")
        salt = secrets.token_hex(16)
        store.set_meta("password_salt", salt)
        store.set_meta("password_hash", hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex())
    sessions = {}
    failures = {}

    @asynccontextmanager
    async def lifespan(app):
        await engine.start()
        yield
        await engine.close()
        store.close()

    app = FastAPI(title="ArchiveBox Slack", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.engine = store, engine

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    async def authenticated(request: Request):
        key = request.cookies.get("abx_slack_session", "")
        session = sessions.get(key)
        if not session or session["expires"] < time.time():
            sessions.pop(key, None)
            raise HTTPException(401, "Sign in to the setup console")
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin", "")
            if origin and origin != str(request.base_url).rstrip("/"):
                raise HTTPException(403, "Origin does not match this setup console")
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                raise HTTPException(403, "Refresh this page before saving")
        return session

    admin_session = Depends(authenticated)

    @app.post("/auth/login")
    async def login(request: Request):
        host = request.client.host
        attempts = [t for t in failures.get(host, []) if t > time.time() - 300]
        if len(attempts) >= 10:
            raise HTTPException(429, "Too many attempts. Try again in five minutes.")
        data = await request.json()
        value = str(data.get("password", ""))[:1024]
        digest = hashlib.scrypt(value.encode(), salt=store.meta("password_salt").encode(), n=16384, r=8, p=1).hex()
        if not hmac.compare_digest(digest, store.meta("password_hash")):
            failures[host] = [*attempts, time.time()]
            raise HTTPException(401, "Incorrect password")
        failures.pop(host, None)
        key, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        sessions[key] = {"csrf": csrf, "expires": time.time() + 86400}
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(
            "abx_slack_session", key, httponly=True, samesite="lax", secure=request.url.scheme == "https", max_age=86400
        )
        return response

    @app.post("/auth/logout")
    async def logout(request: Request, session=admin_session):
        sessions.pop(request.cookies.get("abx_slack_session", ""), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("abx_slack_session")
        return response

    @app.post("/api/password")
    async def change_password(request: Request, session=admin_session):
        data = await request.json()
        old = str(data.get("current_password", ""))[:1024]
        digest = hashlib.scrypt(old.encode(), salt=store.meta("password_salt").encode(), n=16384, r=8, p=1).hex()
        if not hmac.compare_digest(digest, store.meta("password_hash")):
            raise HTTPException(403, "Current password is incorrect")
        new = str(data.get("new_password", ""))
        if not 12 <= len(new) <= 1024:
            raise HTTPException(422, "Choose a password of 12 to 1024 characters")
        salt = secrets.token_hex(16)
        store.set_meta("password_salt", salt)
        store.set_meta("password_hash", hashlib.scrypt(new.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex())
        current_key = request.cookies["abx_slack_session"]
        sessions.clear()
        sessions[current_key] = session
        return {"ok": True}

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/api/state")
    async def state(session=admin_session):
        for role, bot in engine.bots.items():
            task = getattr(bot, "task", None)
            if task and task.done() and not task.cancelled():
                error = task.exception()
                if error:
                    engine.connections[role] = {"ok": False, "error": safe_error(error)}
        jobs = []
        for job in store.jobs(limit=40):
            payload, result = job["payload"], job["result"]
            jobs.append(
                {
                    "id": job["id"],
                    "kind": job["kind"],
                    "state": job["state"]
                    if job["scope"] in (engine.scope(), engine.scope("ai"))
                    or job["state"] in ("done", "ignored", "cancelled")
                    else "held",
                    "error": job["error"],
                    "created_at": job["created_at"],
                    "title": payload.get("title") or payload.get("text", "")[:160] or payload.get("url", ""),
                    "crawl_id": result.get("crawl_id", ""),
                    "session_id": result.get("session", {}).get("id", ""),
                    "session_url": result.get("session_url", ""),
                }
            )
        return {
            "settings": store.public_settings(),
            "csrf": session["csrf"],
            "stats": store.stats(),
            "connections": engine.connections,
            "error": engine.error,
            "jobs": jobs,
        }

    @app.put("/api/settings")
    async def save_settings(request: Request, session=admin_session):
        data = await request.json()
        clear = data.pop("clear_secrets", [])
        current = store.settings().model_dump()
        for key in SECRET_FIELDS:
            if key in clear:
                current[key] = ""
            elif not data.get(key):
                data.pop(key, None)
        try:
            settings = Settings.model_validate({**current, **data})
        except ValidationError as exc:
            raise HTTPException(
                422, [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]
            ) from exc
        if settings.enable_ai and not settings.ai_allowed_users:
            raise HTTPException(422, "Add at least one allowed AI user ID before enabling the AI bot")
        store.save_settings(settings)
        await engine.restart()
        return {"ok": True, "settings": store.public_settings()}

    @app.post("/api/check/{service}")
    async def check(service: str, session=admin_session):
        settings = store.settings()
        client = None
        try:
            if service not in ("archivebox", "chat"):
                raise ValueError("Unknown service")
            client = (
                ArchiveBox(settings)
                if service == "archivebox"
                else (Slack(settings) if settings.platform == "slack" else Zulip(settings))
            )
            return {"ok": True, **await asyncio.wait_for(client.check(), timeout=8)}
        except Exception as exc:
            raise HTTPException(400, safe_error(exc)) from exc
        finally:
            if client:
                await client.close()

    @app.post("/api/channels")
    async def channels(session=admin_session):
        bot = engine.bots.get("capture")
        if not bot:
            raise HTTPException(400, "Connect your chat bot first")
        try:
            channels = await bot.setup_channels()
            settings = store.settings().model_copy(update=channels)
            store.save_settings(settings)
            await engine.restart()
            return {"ok": True, **channels}
        except Exception as exc:
            raise HTTPException(400, safe_error(exc)) from exc

    @app.post("/api/jobs/retry")
    async def retry(request: Request, session=admin_session):
        data = await request.json()
        job = store.get(data.get("id", ""))
        if not job or job["state"] not in ("failed", "uncertain"):
            raise HTTPException(400, "Only failed or uncertain jobs can be retried")
        if data.get("confirmed_remote_absent") is not True:
            raise HTTPException(400, "First confirm the remote crawl, session, or message was not created")
        if job["scope"] not in (engine.scope(), engine.scope("ai")):
            raise HTTPException(409, "Restore this job's original server, workspace and channel before retrying")
        store.update(job["id"], "queued")
        return {"ok": True}

    @app.get("/api/manifest/{role}")
    async def app_manifest(role: str, transport: str = "socket", create: bool = False, session=admin_session):
        if role not in ("capture", "ai") or transport not in ("socket", "http"):
            raise HTTPException(400, "Invalid app or transport")
        settings = store.settings()
        definition = manifest(role, transport, settings.public_url)
        if create:
            return RedirectResponse(
                "https://api.slack.com/apps?" + urlencode({"new_app": 1, "manifest_json": json.dumps(definition)}),
                status_code=303,
            )
        return JSONResponse(
            definition,
            headers={"Content-Disposition": f'attachment; filename="archivebox-{role}.json"'},
        )

    def slack_setting(role, field):
        if role not in ("capture", "ai"):
            raise HTTPException(404)
        return getattr(store.settings(), f"slack_{'ai_' if role == 'ai' else ''}{field}")

    async def verify_slack(request, role):
        secret = slack_setting(role, "signing_secret")
        if not secret:
            raise HTTPException(503, "Configure the app signing secret first")
        body = await request.body()
        if len(body) > 1024 * 1024:
            raise HTTPException(413)
        if not SignatureVerifier(secret).is_valid_request(body, dict(request.headers)):
            raise HTTPException(401, "Invalid Slack signature")
        return body

    @app.post("/slack/{role}/events")
    async def slack_events(role: str, request: Request):
        body = await verify_slack(request, role)
        payload = json.loads(body)
        if payload.get("type") == "url_verification":
            return {"challenge": payload["challenge"]}
        bot = engine.bots.get(role)
        if not bot or not bot.identity:
            raise HTTPException(503, "Bot is not connected")
        await bot.receive(payload)
        return {"ok": True}

    @app.post("/slack/{role}/commands")
    async def slack_commands(role: str, request: Request):
        from urllib.parse import parse_qs

        body = await verify_slack(request, role)
        payload = {key: values[0] for key, values in parse_qs(body.decode()).items()}
        bot = engine.bots.get(role)
        if not bot:
            raise HTTPException(503)
        command = payload.get("text", "").split(maxsplit=1)[0] if payload.get("text", "").strip() else "help"
        if command not in store.settings().commands:
            return {"response_type": "ephemeral", "text": "That command is disabled."}
        await bot.receive_command(payload)
        return {"response_type": "ephemeral", "text": "Working on it — results will appear here."}

    @app.get("/slack/{role}/install")
    async def install(role: str, session=admin_session):
        settings = store.settings()
        client_id = slack_setting(role, "client_id")
        if not client_id or not settings.public_url.startswith("https://"):
            raise HTTPException(400, "Configure an HTTPS public URL and Slack OAuth client ID")
        state = secrets.token_urlsafe(32)
        store.set_meta(
            f"oauth:{state}", json.dumps({"role": role, "expires": time.time() + 600, "session": session["csrf"]})
        )
        params = {
            "client_id": client_id,
            "scope": ",".join(manifest(role)["oauth_config"]["scopes"]["bot"]),
            "state": state,
            "redirect_uri": f"{settings.public_url}/slack/{role}/oauth/callback",
        }
        return RedirectResponse("https://slack.com/oauth/v2/authorize?" + urlencode(params))

    @app.get("/slack/{role}/oauth/callback")
    async def callback(role: str, state: str = "", code: str = "", error: str = "", session=admin_session):
        saved = store.meta(f"oauth:{state}")
        store.set_meta(f"oauth:{state}", "")
        data = json.loads(saved) if saved else {}
        if data.get("role") != role or data.get("expires", 0) < time.time() or data.get("session") != session["csrf"]:
            raise HTTPException(400, "Installation expired; start Connect Slack again")
        if error or not code:
            return RedirectResponse("/?install=cancelled")
        settings = store.settings()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://slack.com/api/oauth.v2.access",
                data={
                    "client_id": slack_setting(role, "client_id"),
                    "client_secret": slack_setting(role, "client_secret"),
                    "code": code,
                    "redirect_uri": f"{settings.public_url}/slack/{role}/oauth/callback",
                },
            )
        token = response.json()
        if not token.get("ok"):
            raise HTTPException(400, "Slack installation failed: " + str(token.get("error", "unknown")))
        key = f"slack_{'ai_' if role == 'ai' else ''}bot_token"
        store.save_settings(settings.model_copy(update={key: token["access_token"]}))
        await engine.restart()
        return RedirectResponse("/?install=connected")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    return app
