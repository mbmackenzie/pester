import argparse
from collections.abc import Sequence
from pathlib import Path

from pester.core.tokens import generate_token, hash_token


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from pester.config import Settings
    from pester.main import create_app

    overrides = {"config": args.config, "host": args.host, "port": args.port}
    settings = Settings(**{k: v for k, v in overrides.items() if v is not None})
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


def _hash_token(args: argparse.Namespace) -> None:
    if args.token is None:
        token = generate_token()
        print(f"token: {token}")
        print(f"hash:  {hash_token(token)}")
    else:
        print(hash_token(args.token))


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

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)
