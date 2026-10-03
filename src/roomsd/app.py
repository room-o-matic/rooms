import asyncio
import contextlib
import shutil
from collections.abc import AsyncIterator

from fastapi import FastAPI, Response

from roomsd import db, lobby_client, ops
from roomsd.config import Settings
from roomsd.limits import RequestSizeLimit
from roomsd.models import WellKnown
from roomsd.routes import auth, me, rooms, tasks
from roomsd.verify import TokenVerifier

VERSION = "0.2.0"


def create_app(settings: Settings | None = None, verifier: TokenVerifier | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    # docs#24: upgrades the schema (after a pre-upgrade backup) or refuses to start.
    db.init_db(settings.db_path, backup_dir=settings.backup_dir)
    lobby_health = ops.LoopHealth()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not settings.lobby_sync_enabled:
            yield
            return
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        # Routes run in a threadpool; asyncio.Event must be set from the loop thread.
        app.state.lobby_wake = lambda: loop.call_soon_threadsafe(wake.set)
        task = asyncio.create_task(lobby_client.sync_loop(settings, wake, lobby_health))
        try:
            yield
        finally:
            task.cancel()
            await lobby_client.deregister(settings)

    app = FastAPI(title="roomsd", version=VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(RequestSizeLimit, max_bytes=settings.max_request_bytes)
    app.state.verifier = verifier or TokenVerifier(
        issuer=settings.lobbyd_url,
        domain=settings.lobbyd_domain,
        audience=settings.base_url,
        jwks_url=settings.lobbyd_jwks_url,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    def checks() -> dict:
        """Readiness (docs#24): can this server actually serve the collaboration path?"""
        conn = db.connect(settings.db_path)
        try:
            version = ops.read_schema_version(conn)
            pending_listings = conn.execute(
                "select count(*) from rooms where listing_version > listing_synced_version"
            ).fetchone()[0]
        finally:
            conn.close()
        db_error = ops.db_writable(settings.db_path)
        free = shutil.disk_usage(settings.data_dir).free
        jwks = app.state.verifier.health()
        sync_age = lobby_health.age()
        sync_stale = settings.lobby_sync_enabled and (
            sync_age is None or sync_age > 3 * settings.lobby_heartbeat_ttl_seconds
        )
        return {
            "database": {"ok": db_error is None, "error": db_error},
            "schema": {"ok": version == db.SCHEMA_VERSION, "version": version},
            "storage": {"ok": free >= settings.min_free_bytes, "free_bytes": free},
            "jwks": {"ok": not jwks["failing_closed"], **jwks},
            # Directory sync is degraded-not-fatal: rooms keep working without lobbyd.
            "lobby_sync": {
                "ok": not sync_stale,
                "required": False,
                "enabled": settings.lobby_sync_enabled,
                "age_seconds": sync_age,
                "consecutive_failures": lobby_health.failures,
                "last_error": lobby_health.last_error,
                "pending_listings": pending_listings,
            },
        }

    @app.get("/readyz")
    def readyz(response: Response) -> dict:
        c = checks()
        ready = all(v["ok"] for v in c.values() if v.get("required", True))
        response.status_code = 200 if ready else 503
        return {"ready": ready, "checks": c}

    @app.get("/metrics")
    def metrics() -> Response:
        c = checks()
        conn = db.connect(settings.db_path)
        try:
            counts = conn.execute(
                "select (select count(*) from rooms where archived_at is null),"
                " (select coalesce(max(id), 0) from messages),"
                " (select count(*) from invites where revoked_at is null and expires_at > ?),"
                " (select count(*) from tasks where lease_expires_at > ?)",
                (now := ops.now(), now),
            ).fetchone()
        finally:
            conn.close()
        gauges = {
            "ready": all(v["ok"] for v in c.values() if v.get("required", True)),
            "schema_version": c["schema"]["version"],
            "db_bytes": settings.db_path.stat().st_size,
            "disk_free_bytes": c["storage"]["free_bytes"],
            "jwks_age_seconds": c["jwks"]["age_seconds"],
            "jwks_fetch_failures": c["jwks"]["consecutive_failures"],
            "jwks_failing_closed": c["jwks"]["failing_closed"],
            "lobby_sync_age_seconds": c["lobby_sync"]["age_seconds"],
            "lobby_sync_failures": c["lobby_sync"]["consecutive_failures"],
            "pending_listings": c["lobby_sync"]["pending_listings"],
            "active_rooms": counts[0],
            "last_message_id": counts[1],
            "live_invites": counts[2],
            "held_task_leases": counts[3],
        }
        return Response(ops.prometheus("roomsd", gauges), media_type="text/plain; version=0.0.4")

    @app.get("/.well-known/roomsd")
    def well_known() -> WellKnown:
        return WellKnown(
            server_id=settings.server_id,
            base_url=settings.base_url,
            issuer=settings.lobbyd_url,
            domain=settings.lobbyd_domain,
            version=VERSION,
            features=["invites", "notes", "me.updates", "listing", "admission", "tasks"],
        )

    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(rooms.router)
    app.include_router(tasks.router)
    return app
