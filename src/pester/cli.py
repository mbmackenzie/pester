import argparse
from collections.abc import Sequence
from pathlib import Path

from pester.core.tokens import generate_token, hash_token


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from pester.config import Settings
    from pester.logs import configure_logging
    from pester.main import create_app

    overrides = {"config": args.config, "host": args.host, "port": args.port}
    settings = Settings(**{k: v for k, v in overrides.items() if v is not None})
    configure_logging(settings.log_level, settings.log_format)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)


def _hash_token(args: argparse.Namespace) -> None:
    if args.token is None:
        token = generate_token()
        print(f"token: {token}")
        print(f"hash:  {hash_token(token)}")
    else:
        print(hash_token(args.token))


def _chat(args: argparse.Namespace) -> None:
    import asyncio
    import contextlib

    from pester.devchat import run_chat

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_chat(args.url, args.address))


def _reset_password(args: argparse.Namespace) -> None:
    import asyncio

    from pester.admin.auth import AdminAuth
    from pester.config import Settings
    from pester.core.clock import SystemClock
    from pester.storage.db import Database

    async def reset() -> None:
        db = await Database.open(Settings().database_path)
        try:
            await AdminAuth(db, SystemClock()).reset()
        finally:
            await db.close()

    asyncio.run(reset())
    print("Admin password cleared. Restart Pester, then open /admin/setup with the code from the logs.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pester", description="Jobs in. Humans bothered. Events out.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the Pester server")
    serve.add_argument("--config", type=Path, help="deployment config YAML (default: $PESTER_CONFIG)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=_serve)

    tok = sub.add_parser("hash-token", help="hash a producer token for config (generates one if omitted)")
    tok.add_argument("token", nargs="?")
    tok.set_defaults(func=_hash_token)

    chat = sub.add_parser("chat", help="chat as a recipient through the fake channel (dev mode)")
    chat.add_argument("address", help="the recipient's fake channel address")
    chat.add_argument("--url", default="http://127.0.0.1:8000", help="Pester server URL")
    chat.set_defaults(func=_chat)

    admin = sub.add_parser("admin", help="admin UI maintenance")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    reset = admin_sub.add_parser("reset-password", help="clear the admin password and sign out every session")
    reset.set_defaults(func=_reset_password)

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)
