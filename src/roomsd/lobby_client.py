"""roomsd's side of lobbyd: heartbeat this server into the directory and keep its
listed rooms published there.

Listing sync is driven by rooms.listing_version (bumped on any listing-relevant change)
versus rooms.listing_synced_version (what lobbyd last accepted). A change made while a
push is in flight bumps the version again, so it is pushed on the next pass and never
lost.
"""

import asyncio
import json
import logging

import httpx

from roomsd import db
from roomsd.config import Settings

log = logging.getLogger("roomsd.lobby")


async def heartbeat(client: httpx.AsyncClient, settings: Settings) -> None:
    r = await client.put(
        f"/v1/servers/roomsd/{settings.server_id}",
        json={
            "base_url": settings.base_url,
            "tags": list(settings.tags),
            "ttl_seconds": settings.lobby_heartbeat_ttl_seconds,
        },
    )
    r.raise_for_status()


async def sync_listings(client: httpx.AsyncClient, settings: Settings) -> int:
    """Push every room whose listing changed since the last successful push."""
    conn = db.connect(settings.db_path)
    try:
        rows = conn.execute(
            "select * from rooms where listing_version > listing_synced_version"
        ).fetchall()
        for row in rows:
            room_url = settings.room_url(row["id"])
            if row["listed"] and row["archived_at"] is None:
                r = await client.put(
                    "/v1/rooms",
                    json={
                        "room_url": room_url,
                        "name": row["name"],
                        "purpose": row["purpose"],
                        "tags": json.loads(row["tags_json"]),
                    },
                )
            else:
                r = await client.delete("/v1/rooms", params={"room_url": room_url})
            r.raise_for_status()
            with conn:
                conn.execute(
                    "update rooms set listing_synced_version = ? where id = ?",
                    (row["listing_version"], row["id"]),
                )
        return len(rows)
    finally:
        conn.close()


async def sync_loop(settings: Settings, wake: asyncio.Event) -> None:
    """Heartbeat every ttl/3, syncing listings each time; `wake` triggers an early pass."""
    interval = settings.lobby_heartbeat_ttl_seconds / 3
    async with httpx.AsyncClient(
        base_url=settings.lobbyd_url,
        headers={"Authorization": f"Bearer {settings.lobbyd_api_key}"},
        timeout=10,
    ) as client:
        while True:
            wake.clear()
            try:
                await heartbeat(client, settings)
                await sync_listings(client, settings)
            except httpx.HTTPError as e:
                log.warning("lobbyd sync with %s failed: %s", settings.lobbyd_url, e)
            try:
                await asyncio.wait_for(wake.wait(), interval)
            except TimeoutError:
                pass


async def deregister(settings: Settings) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            await client.delete(
                f"{settings.lobbyd_url}/v1/servers/roomsd/{settings.server_id}",
                headers={"Authorization": f"Bearer {settings.lobbyd_api_key}"},
            )
        except httpx.HTTPError as e:
            log.warning("lobbyd deregister failed: %s", e)
