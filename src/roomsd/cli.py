import argparse
import os
import sys
import time

import httpx

from roomsd import auth, db
from roomsd.config import Settings

DEFAULT_URL = "http://127.0.0.1:8766"


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("roomsd.app:create_app", factory=True, host=args.host, port=args.port)
    return 0


def cmd_token_create(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        token = auth.create_token(conn, args.agent, args.scope)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(token)
    return 0


def cmd_token_revoke(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        n = auth.revoke_tokens(conn, args.agent)
    finally:
        conn.close()
    print(f"revoked {n} token(s) for {args.agent}")
    return 0


def format_message(m: dict) -> str:
    head = f"[{m['id']}] {m['created_at']} {m['from']} {m['type']}"
    if m.get("topic"):
        head += f" ({m['topic']})"
    if m.get("confidence") is not None:
        head += f" conf={m['confidence']}"
    return f"{head}: {m['body']}"


def cmd_tail(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("ROOMSD_TOKEN")
    if not token:
        print("error: pass --token or set ROOMSD_TOKEN", file=sys.stderr)
        return 2
    after_id = args.after_id
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="roomsd", description="Agent collaboration rooms")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    serve.set_defaults(func=cmd_serve)

    token = sub.add_parser("token", help="manage agent bearer tokens (local DB access)")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    create = token_sub.add_parser("create", help="issue a token for an agent and print it")
    create.add_argument("agent", help="agent name, or the instance_id for --scope agentd")
    create.add_argument("--scope", choices=auth.ISSUABLE_SCOPES, default="agent")
    create.set_defaults(func=cmd_token_create)
    revoke = token_sub.add_parser("revoke", help="revoke all tokens for an agent")
    revoke.add_argument("agent")
    revoke.set_defaults(func=cmd_token_revoke)

    tail = sub.add_parser("tail", help="follow a room's messages")
    tail.add_argument("room_id")
    tail.add_argument("--url", default=os.environ.get("ROOMSD_URL", DEFAULT_URL))
    tail.add_argument("--token", help="bearer token (default: $ROOMSD_TOKEN)")
    tail.add_argument("--after-id", type=int, default=0)
    tail.add_argument("--interval", type=float, default=2.0, help="poll interval seconds")
    tail.add_argument("--once", action="store_true", help="print backlog and exit")
    tail.set_defaults(func=cmd_tail)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
