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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from slack_sdk.signature import SignatureVerifier

from .archivebox import ArchiveBox
from .config import Settings, merge_settings
from .engine import safe_error
from .manifest import manifest
from .runtime import Runtime
from .store import Store

STATIC = Path(__file__).parent / "static"


def create_app(directory=None):
    store = Store(directory or os.environ.get("DATA_DIR", "data"))
    engine = Runtime(store)
    password = os.environ.get("ADMIN_PASSWORD", "")

    def save_password(value):
        if not 12 <= len(value) <= 1024:
            raise ValueError("Choose a password of 12 to 1024 characters")
        salt = secrets.token_hex(16)
        digest = hashlib.scrypt(value.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        with store.db:
            store.db.executemany(
                "INSERT OR REPLACE INTO meta VALUES (?,?)", [("password_salt", salt), ("password_hash", digest)]
            )

    if password and not store.meta("password_hash"):
        save_password(password)
    sessions = {}
    failures = {}

    @asynccontextmanager
    async def lifespan(app):
        startup = asyncio.create_task(engine.initialize())
        yield
        startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        await engine.close()
        store.close()

    app = FastAPI(title="Chatbot Admin Console", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
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

    def verify_origin(request):
        origin = request.headers.get("origin", "")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origin does not match this Chatbot Admin Console")

    def signed_in(request):
        key, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        sessions[key] = {"csrf": csrf, "expires": time.time() + 86400}
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(
            "abx_chat_session", key, httponly=True, samesite="lax", secure=request.url.scheme == "https", max_age=86400
        )
        return response

    async def authenticated(request: Request):
        key = request.cookies.get("abx_chat_session", "")
        session = sessions.get(key)
        if not session or session["expires"] < time.time():
            sessions.pop(key, None)
            raise HTTPException(401, "Sign in to the Chatbot Admin Console")
        if request.method not in ("GET", "HEAD"):
            verify_origin(request)
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                raise HTTPException(403, "Refresh this page before saving")
        return session

    admin_session = Depends(authenticated)

    @app.get("/auth/setup")
    async def setup_status():
        return {"required": not bool(store.meta("password_hash"))}

    @app.post("/auth/setup")
    async def setup_password(request: Request):
        verify_origin(request)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise HTTPException(415, "Use the password setup form")
        data = await request.json()
        # No await between checking and saving: only the first setup request can win.
        if store.meta("password_hash"):
            raise HTTPException(409, "A password is already set. Sign in instead.")
        try:
            save_password(str(data.get("password", "")))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return signed_in(request)

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
        return signed_in(request)

    @app.post("/auth/logout")
    async def logout(request: Request, session=admin_session):
        sessions.pop(request.cookies.get("abx_chat_session", ""), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("abx_chat_session")
        return response

    @app.post("/api/password")
    async def change_password(request: Request, session=admin_session):
        data = await request.json()
        old = str(data.get("current_password", ""))[:1024]
        digest = hashlib.scrypt(old.encode(), salt=store.meta("password_salt").encode(), n=16384, r=8, p=1).hex()
        if not hmac.compare_digest(digest, store.meta("password_hash")):
            raise HTTPException(403, "Current password is incorrect")
        new = str(data.get("new_password", ""))
        try:
            save_password(new)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        current_key = request.cookies["abx_chat_session"]
        sessions.clear()
        sessions[current_key] = session
        return {"ok": True}

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/api/state")
    async def state(session=admin_session):
        jobs = []
        for job in store.jobs(limit=40):
            payload, result = job["payload"], job["result"]
            jobs.append(
                {
                    "id": job["id"],
                    "kind": job["kind"],
                    "state": job["state"]
                    if job["scope"] in engine.scopes or job["state"] in ("done", "ignored", "cancelled")
                    else "held",
                    "error": job["error"],
                    "created_at": job["created_at"],
                    "title": payload.get("title") or payload.get("text", "")[:160] or payload.get("url", ""),
                    "crawl_id": result.get("crawl_id", ""),
                    "session_id": result.get("session", {}).get("id", ""),
                    "session_url": result.get("session_url", ""),
                    "answer_delivery_started": result.get("phase") == "agent_delivery",
                }
            )
        return {
            "settings": store.public_settings(),
            "csrf": session["csrf"],
            "stats": store.stats(),
            "connections": engine.status(),
            "groups": {c.id: store.groups(c.id) for c in store.settings().connections},
            "error": engine.error,
            "jobs": jobs,
            "beeper_default_url": "http://host.docker.internal:23373"
            if Path("/.dockerenv").exists()
            else "http://127.0.0.1:23373",
        }

    @app.put("/api/settings")
    async def save_settings(request: Request, session=admin_session):
        data = await request.json()
        try:
            settings = Settings.model_validate(merge_settings(store.settings().model_dump(), data))
        except ValidationError as exc:
            raise HTTPException(
                422, [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]
            ) from exc
        store.save_settings(settings)
        await engine.restart()
        return {"ok": True, "settings": store.public_settings()}

    def connection(connection_id):
        value = next((c for c in store.settings().connections if c.id == connection_id), None)
        if value is None:
            raise HTTPException(404, "Connection not found")
        return value

    def bot_for(connection_id, role="capture"):
        active = engine.engines.get(connection_id)
        bot = active.bots.get(role) if active else None
        if bot is None:
            raise HTTPException(400, "Save and connect this bot first")
        return bot

    @app.post("/api/check/{service}")
    async def check(service: str, connection_id: str = "default", role: str = "capture", session=admin_session):
        client = None
        try:
            if service == "archivebox":
                client = ArchiveBox(store.settings())
                result = await asyncio.wait_for(client.check(), timeout=8)
                engine.connections["archivebox"] = result
            elif service == "chat":
                result = await asyncio.wait_for(bot_for(connection_id, role).check(), timeout=15)
            else:
                raise ValueError("Unknown service")
            return {"ok": result.get("state", "connected") == "connected", **result}
        except Exception as exc:
            raise HTTPException(400, safe_error(exc)) from exc
        finally:
            if client:
                await client.close()

    def update_connection(value):
        settings = store.settings()
        settings.connections = [value if c.id == value.id else c for c in settings.connections]
        store.save_settings(settings)

    @app.post("/api/discord/discover")
    async def discover_discord(request: Request, session=admin_session):
        from .transports.discord import DiscordTransport

        data = await request.json()
        role = data.get("role", "capture")
        if role not in {"capture", "ai"}:
            raise HTTPException(422, "Choose a bot role")
        previous = next(
            (c for c in store.settings().connections if c.id == data.get("connection_id") and c.platform == "discord"),
            None,
        )
        options = merge_settings(previous.account_options(role) if previous else {}, data.get("options", {}))
        client = DiscordTransport(options, f"discovery:{role}", store.directory, None)
        try:
            return await asyncio.wait_for(client.discover(), timeout=15)
        except Exception as exc:
            raise HTTPException(400, safe_error(exc)) from exc
        finally:
            await client.close()

    @app.post("/api/beeper/discover")
    async def discover_beeper(request: Request, session=admin_session):
        data = await request.json()
        role = data.get("role", "capture")
        if role not in {"capture", "ai"}:
            raise HTTPException(422, "Choose a bot role")
        previous = next(
            (c for c in store.settings().connections if c.id == data.get("connection_id") and c.platform == "beeper"),
            None,
        )
        options = merge_settings(previous.account_options(role) if previous else {}, data.get("options", {}))
        try:
            base_url = Settings.http_url(options.get("base_url") or "http://127.0.0.1:23373")
            headers = {"Authorization": "Bearer " + options["access_token"]} if options.get("access_token") else {}
            async with httpx.AsyncClient(
                base_url=base_url, headers=headers, timeout=15, follow_redirects=False
            ) as client:
                setup_response = await client.get("/v1/app/setup")
                setup_response.raise_for_status()
                setup = setup_response.json()
                if setup["state"] != "ready":
                    return {"state": setup["state"], "accounts": [], "channels": []}
                response = await client.get("/v1/accounts")
                response.raise_for_status()
                accounts = [
                    {
                        "id": a["accountID"],
                        "name": " · ".join(
                            filter(
                                None,
                                [
                                    a.get("network"),
                                    a["user"].get("fullName") or a["user"].get("username") or a["user"]["id"],
                                ],
                            )
                        ),
                        "status": a["status"],
                    }
                    for a in response.json()
                ]
            channels = []
            if options.get("account_id"):
                from .transports.beeper import BeeperTransport

                discovery = BeeperTransport(options, "discovery", store.directory / "beeper-discovery", None)
                try:
                    channels = await discovery.channels()
                finally:
                    await discovery.close()
            return {"state": "ready", "accounts": accounts, "channels": channels}
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, "Check the Beeper server URL and account selection") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise HTTPException(
                400,
                "Approve a Beeper API access token first" if status in {401, 403} else f"Beeper returned HTTP {status}",
            ) from None
        except httpx.TransportError:
            raise HTTPException(
                400, "Beeper is unreachable; start Beeper Desktop or Server and check its address"
            ) from None
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/connections/{connection_id}/channels")
    async def channels(connection_id: str, session=admin_session):
        bot = bot_for(connection_id)
        try:
            if hasattr(bot, "setup_channels"):
                updates = await bot.setup_channels()
                update_connection(connection(connection_id).model_copy(update=updates))
                await engine.restart()
                return {"ok": True, **updates}
            return {"ok": True, "channels": await bot.channels()}
        except Exception as exc:
            raise HTTPException(400, safe_error(exc)) from exc

    @app.get("/api/connections/{connection_id}/conversations")
    async def conversations(connection_id: str, role: str = "capture", session=admin_session):
        bot = bot_for(connection_id, role)
        try:
            for channel in await bot.channels():
                store.set_group(
                    connection_id,
                    str(channel["id"]),
                    name=channel.get("name") or str(channel["id"]),
                    is_dm=channel.get("is_dm", False),
                )
        except Exception as exc:
            raise HTTPException(400, safe_error(exc)) from exc
        return {"channels": store.groups(connection_id)}

    @app.get("/api/connections/{connection_id}/people")
    async def people(connection_id: str, session=admin_session):
        connection(connection_id)
        rows = store.db.execute(
            "SELECT user_id AS id, MAX(user_name) AS name FROM history WHERE connection=? AND is_bot=0 GROUP BY user_id ORDER BY name",
            (connection_id,),
        ).fetchall()
        return {"people": [dict(row) for row in rows]}

    @app.put("/api/connections/{connection_id}/groups")
    async def group_settings(connection_id: str, request: Request, session=admin_session):
        connection(connection_id)
        data = await request.json()
        channel = str(data.get("channel", ""))
        if not channel or not isinstance(data.get("auto_archive"), bool):
            raise HTTPException(422, "Choose a group and its archiving preference")
        store.set_group(connection_id, channel, auto_archive=data["auto_archive"])
        return {"ok": True}

    @app.get("/api/connections/{connection_id}/{role}/pairing.png")
    async def pairing(connection_id: str, role: str, session=admin_session):
        import io

        import qrcode

        connection(connection_id)
        qr = engine.adapters.worker.status.get(f"{connection_id}:{role}", {}).get("qr")
        if not qr:
            raise HTTPException(404, "No pairing code available")
        output = io.BytesIO()
        qrcode.make(qr).save(output, format="PNG")
        return Response(output.getvalue(), media_type="image/png")

    @app.api_route("/connections/{connection_id}/{role}/webhook", methods=["GET", "POST"])
    async def webhook(connection_id: str, role: str, request: Request):
        value = connection(connection_id)
        if value.platform not in {"telegram", "messenger"} or role not in {"capture", "ai"}:
            raise HTTPException(404)
        body = await request.body()
        if len(body) > 1024 * 1024:
            raise HTTPException(413)
        result = await engine.adapters.worker.call(
            "webhook",
            f"{connection_id}:{role}",
            {
                "url": str(request.url),
                "method": request.method,
                "headers": dict(request.headers),
                "body": body.decode(),
            },
        )
        return Response(result["body"], status_code=result["status"], headers=result["headers"])

    @app.post("/api/jobs/retry")
    async def retry(request: Request, session=admin_session):
        data = await request.json()
        job = store.get(data.get("id", ""))
        if not job or job["state"] not in ("failed", "uncertain"):
            raise HTTPException(400, "Only failed or uncertain jobs can be retried")
        if data.get("confirmed_remote_absent") is not True:
            raise HTTPException(400, "First confirm the remote crawl, session, or message was not created")
        if job["scope"] not in engine.scopes:
            raise HTTPException(409, "Restore this job's original server, workspace and channel before retrying")
        if job["result"].get("session"):
            raise HTTPException(
                409, "An agent session already exists; recover its answer instead of repeating the task"
            )
        store.update(job["id"], "queued")
        return {"ok": True}

    @app.post("/api/jobs/resume")
    async def resume_agent(request: Request, session=admin_session):
        data = await request.json()
        job = store.get(data.get("id", ""))
        if not job or job["state"] != "uncertain" or not job["result"].get("session"):
            raise HTTPException(400, "Choose an interrupted agent session")
        if job["scope"] not in engine.scopes:
            raise HTTPException(409, "Reconnect the original server and bot first")
        if job["result"].get("phase") != "agent_result" and data.get("confirmed_answer_absent") is not True:
            raise HTTPException(400, "Check chat and confirm the answer was not already delivered")
        store.update(job["id"], "agent_waiting", {**job["result"], "phase": "agent_result"})
        return {"ok": True}

    @app.post("/api/jobs/stop")
    async def stop_agent(request: Request, session=admin_session):
        job = store.get((await request.json()).get("id", ""))
        if not job or job["state"] not in {"running", "agent_waiting", "uncertain"} or not job["result"].get("session"):
            raise HTTPException(400, "Choose an active agent task")
        if job["scope"] not in engine.scopes:
            raise HTTPException(409, "Reconnect the original server and bot first")
        await engine.archive.abort_session(job["result"]["session"])
        store.update(job["id"], "cancelled")
        return {"ok": True}

    @app.get("/api/manifest/{role}")
    async def app_manifest(
        role: str,
        transport: str = "socket",
        create: bool = False,
        connection_id: str = "default",
        session=admin_session,
    ):
        if role not in ("capture", "ai") or transport not in ("socket", "http"):
            raise HTTPException(400, "Invalid app or transport")
        settings = store.settings()
        definition = manifest(role, transport, settings.public_url, connection_id)
        if create:
            return RedirectResponse(
                "https://api.slack.com/apps?" + urlencode({"new_app": 1, "manifest_json": json.dumps(definition)}),
                status_code=303,
            )
        return JSONResponse(
            definition,
            headers={"Content-Disposition": f'attachment; filename="archivebox-{role}.json"'},
        )

    def slack_setting(connection_id, role, field):
        if role not in ("capture", "ai"):
            raise HTTPException(404)
        value = connection(connection_id)
        if value.platform != "slack":
            raise HTTPException(404)
        return value.account_options(role).get(field, "")

    async def verify_slack(request, connection_id, role):
        secret = slack_setting(connection_id, role, "signing_secret")
        if not secret:
            raise HTTPException(503, "Configure the app signing secret first")
        body = await request.body()
        if len(body) > 1024 * 1024:
            raise HTTPException(413)
        if not SignatureVerifier(secret).is_valid_request(body, dict(request.headers)):
            raise HTTPException(401, "Invalid Slack signature")
        return body

    @app.post("/slack/{role}/events")
    @app.post("/connections/{connection_id}/slack/{role}/events")
    async def slack_events(role: str, request: Request, connection_id: str = "default"):
        body = await verify_slack(request, connection_id, role)
        payload = json.loads(body)
        if payload.get("type") == "url_verification":
            return {"challenge": payload["challenge"]}
        bot = bot_for(connection_id, role)
        if not bot or not bot.identity:
            raise HTTPException(503, "Bot is not connected")
        await bot.receive(payload)
        return {"ok": True}

    @app.post("/slack/{role}/commands")
    @app.post("/connections/{connection_id}/slack/{role}/commands")
    async def slack_commands(role: str, request: Request, connection_id: str = "default"):
        from urllib.parse import parse_qs

        body = await verify_slack(request, connection_id, role)
        payload = {key: values[0] for key, values in parse_qs(body.decode()).items()}
        bot = bot_for(connection_id, role)
        if not bot:
            raise HTTPException(503)
        command = payload.get("text", "").split(maxsplit=1)[0] if payload.get("text", "").strip() else "help"
        if command not in connection(connection_id).commands:
            return {"response_type": "ephemeral", "text": "That command is disabled."}
        await bot.receive_command(payload)
        return {"response_type": "ephemeral", "text": "Working on it — results will appear here."}

    @app.get("/slack/{role}/install")
    @app.get("/connections/{connection_id}/slack/{role}/install")
    async def install(role: str, connection_id: str = "default", session=admin_session):
        settings = store.settings()
        client_id = slack_setting(connection_id, role, "client_id")
        if not client_id or not settings.public_url.startswith("https://"):
            raise HTTPException(400, "Configure an HTTPS public URL and Slack OAuth client ID")
        state = secrets.token_urlsafe(32)
        store.set_meta(
            f"oauth:{state}",
            json.dumps(
                {"role": role, "connection_id": connection_id, "expires": time.time() + 600, "session": session["csrf"]}
            ),
        )
        params = {
            "client_id": client_id,
            "scope": ",".join(manifest(role)["oauth_config"]["scopes"]["bot"]),
            "state": state,
            "redirect_uri": f"{settings.public_url}/connections/{connection_id}/slack/{role}/oauth/callback",
        }
        return RedirectResponse("https://slack.com/oauth/v2/authorize?" + urlencode(params))

    @app.get("/slack/{role}/oauth/callback")
    @app.get("/connections/{connection_id}/slack/{role}/oauth/callback")
    async def callback(
        role: str,
        connection_id: str = "default",
        state: str = "",
        code: str = "",
        error: str = "",
        session=admin_session,
    ):
        saved = store.meta(f"oauth:{state}")
        store.set_meta(f"oauth:{state}", "")
        data = json.loads(saved) if saved else {}
        if (
            data.get("connection_id") != connection_id
            or data.get("role") != role
            or data.get("expires", 0) < time.time()
            or data.get("session") != session["csrf"]
        ):
            raise HTTPException(400, "Installation expired; start Connect Slack again")
        if error or not code:
            return RedirectResponse("/?install=cancelled")
        settings = store.settings()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://slack.com/api/oauth.v2.access",
                data={
                    "client_id": slack_setting(connection_id, role, "client_id"),
                    "client_secret": slack_setting(connection_id, role, "client_secret"),
                    "code": code,
                    "redirect_uri": f"{settings.public_url}/connections/{connection_id}/slack/{role}/oauth/callback",
                },
            )
        token = response.json()
        if not token.get("ok"):
            raise HTTPException(400, "Slack installation failed: " + str(token.get("error", "unknown")))
        value = connection(connection_id)
        getattr(value, role).options["bot_token"] = token["access_token"]
        update_connection(value)
        await engine.restart()
        return RedirectResponse("/?install=connected")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    return app
