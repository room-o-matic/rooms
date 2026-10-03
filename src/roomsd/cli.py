import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx

DEFAULT_URL = "http://127.0.0.1:8766"


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("roomsd.app:create_app", factory=True, host=args.host, port=args.port)
    return 0


def access_token(roomsd_url: str, explicit: str | None) -> str:
    """An explicit token (lobbyd access token or invite), or exchange LOBBYD_API_KEY."""
    if token := explicit or os.environ.get("ROOMSD_TOKEN"):
        return token
    key, lobby = os.environ.get("LOBBYD_API_KEY"), os.environ.get("LOBBYD_URL")
    if not (key and lobby):
        raise SystemExit(
            "error: pass --token, set ROOMSD_TOKEN, or set LOBBYD_URL and LOBBYD_API_KEY"
        )
    r = httpx.post(
        f"{lobby.rstrip('/')}/v1/token",
        json={"audience": roomsd_url.rstrip("/")},
        headers={"Authorization": f"Bearer {key}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def format_message(m: dict) -> str:
    head = f"[{m['id']}] {m['created_at']} {m['from']} {m['type']}"
    if m.get("topic"):
        head += f" ({m['topic']})"
    if m.get("confidence") is not None:
        head += f" conf={m['confidence']}"
    return f"{head}: {m['body']}"


def cmd_tail(args: argparse.Namespace) -> int:
    token = access_token(args.url, args.token)
    after_id = args.after_id
    refreshed = False
    with httpx.Client(
        base_url=args.url, headers={"Authorization": f"Bearer {token}"}, timeout=10
    ) as client:
        while True:
            try:
                r = client.get(f"/v1/rooms/{args.room_id}/messages", params={"after_id": after_id})
            except httpx.TransportError as e:
                print(f"warning: {e}; retrying", file=sys.stderr)
                time.sleep(args.interval)
                continue
            exchanged = not (args.token or os.environ.get("ROOMSD_TOKEN"))
            if r.status_code == 401 and exchanged and not refreshed:
                # Exchanged access tokens are short-lived; get a fresh one (once).
                client.headers["Authorization"] = f"Bearer {access_token(args.url, None)}"
                refreshed = True
                continue
            refreshed = False
            if r.status_code >= 400:
                print(f"error: {r.status_code} {r.text}", file=sys.stderr)
                return 1
            page = r.json()
            for m in page["messages"]:
                print(format_message(m), flush=True)
            after_id = page["latest_message_id"]
            # Keep fetching without sleeping until the backlog is drained.
            if page["messages"]:
                continue
            if args.once:
                return 0
            time.sleep(args.interval)


def cmd_backup(args: argparse.Namespace) -> int:
    from roomsd import recovery
    from roomsd.config import Settings

    manifest = recovery.backup(Settings.from_env(), Path(args.out))
    print(json.dumps({k: v for k, v in manifest.items() if k != "files"}, indent=2))
    print(
        f"backed up {len(manifest['files'])} files to {args.out}; encrypt it before it"
        " leaves this host",
        file=sys.stderr,
    )
    return 0


def cmd_verify_backup(args: argparse.Namespace) -> int:
    from roomsd import ops

    try:
        manifest = ops.verify_backup(Path(args.path))
    except ops.BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(
        f"ok: {manifest['service']} schema v{manifest['schema_version']},"
        f" {len(manifest['files'])} files, taken {manifest['created_at']}"
    )
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from roomsd import ops, recovery
    from roomsd.config import Settings

    try:
        report = recovery.restore(
            Settings.from_env(), Path(args.source), force=args.force, id_gap=args.id_gap
        )
    except (ops.BackupError, ops.SchemaError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="roomsd", description="Agent collaboration rooms")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    serve.set_defaults(func=cmd_serve)

    tail = sub.add_parser("tail", help="follow a room's messages")
    tail.add_argument("room_id")
    tail.add_argument("--url", default=os.environ.get("ROOMSD_URL", DEFAULT_URL))
    tail.add_argument("--token", help="access or invite token (default: $ROOMSD_TOKEN)")
    tail.add_argument("--after-id", type=int, default=0)
    tail.add_argument("--interval", type=float, default=2.0, help="poll interval seconds")
    tail.add_argument("--once", action="store_true", help="print backlog and exit")
    tail.set_defaults(func=cmd_tail)

    # docs#24: operate on $ROOMSD_DATA_DIR directly (stop the server before restoring).
    b = sub.add_parser("backup", help="consistent snapshot of the database (safe while serving)")
    b.add_argument("--out", required=True, help="new directory to write the backup into")
    b.set_defaults(func=cmd_backup)
    v = sub.add_parser("verify-backup", help="check a backup's checksums and integrity")
    v.add_argument("path")
    v.set_defaults(func=cmd_verify_backup)
    r = sub.add_parser("restore", help="restore a backup into $ROOMSD_DATA_DIR (server stopped)")
    r.add_argument("source", help="backup directory")
    r.add_argument("--force", action="store_true", help="move an existing database aside")
    r.add_argument(
        "--id-gap",
        type=int,
        default=1_000_000,
        help="advance message/audit IDs by this much past the snapshot",
    )
    r.set_defaults(func=cmd_restore)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
