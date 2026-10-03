import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from roomsd import db, lobby_client
from roomsd.config import Settings
from roomsd.models import WellKnown
from roomsd.routes import auth, me, rooms
from roomsd.verify import TokenVerifier

VERSION = "0.2.0"


def create_app(settings: Settings | None = None, verifier: TokenVerifier | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db.init_db(settings.db_path)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not settings.lobby_sync_enabled:
            yield
            return
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        # Routes run in a threadpool; asyncio.Event must be set from the loop thread.
        app.state.lobby_wake = lambda: loop.call_soon_threadsafe(wake.set)
        task = asyncio.create_task(lobby_client.sync_loop(settings, wake))
        try:
            yield
        finally:
            task.cancel()
            await lobby_client.deregister(settings)

    app = FastAPI(title="roomsd", version=VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.verifier = verifier or TokenVerifier(
        issuer=settings.lobbyd_url,
        domain=settings.lobbyd_domain,
        audience=settings.base_url,
        jwks_url=settings.lobbyd_jwks_url,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/.well-known/roomsd")
    def well_known() -> WellKnown:
        return WellKnown(
            server_id=settings.server_id,
            base_url=settings.base_url,
            issuer=settings.lobbyd_url,
            domain=settings.lobbyd_domain,
            version=VERSION,
            features=["invites", "notes", "me.updates", "listing"],
        )

    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(rooms.router)
    return app
