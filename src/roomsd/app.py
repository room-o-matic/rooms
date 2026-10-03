from fastapi import FastAPI

from roomsd import db
from roomsd.config import Settings
from roomsd.routes import auth, registry, rooms


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db.init_db(settings.db_path)

    app = FastAPI(title="roomsd", version="0.1.0")
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(rooms.router)
    app.include_router(registry.router)
    return app
