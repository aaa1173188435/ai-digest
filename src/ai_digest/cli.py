"""Command line entry point.

Three verbs, intentionally: ``run`` does the work, ``status`` tells you what
happened last time, ``serve`` lets you look at the output before pushing it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import db
from .models import Digest
from .pipeline import run_digest
from .render.html import rebuild_site
from .render.markdown import render_markdown
from .settings import Settings, load_settings

log = logging.getLogger("ai_digest")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _write_outputs(settings: Settings, digest: Digest) -> list[Path]:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    with db.open_db(settings.database_path) as conn:
        paths = rebuild_site(conn, settings.output_dir)

    md_path = settings.output_dir / f"digest-{digest.digest_date.isoformat()}.md"
    md_path.write_text(render_markdown(digest), encoding="utf-8")
    paths.append(md_path)
    return paths


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.provider:
        settings.llm_provider = args.provider
    if args.out_dir:
        settings.output_dir = Path(args.out_dir)
    if args.top_n:
        settings.top_n = args.top_n

    digest = asyncio.run(run_digest(settings))

    if args.no_render:
        log.info("rendered output skipped (--no-render)")
    else:
        paths = _write_outputs(settings, digest)
        for path in paths:
            log.info("wrote %s", path)

    print(
        f"\n{digest.digest_date}: {len(digest.items)} 条 · "
        f"候选 {digest.stats.get('candidates', 0)} · "
        f"去重后 {digest.stats.get('after_dedupe', 0)} · "
        f"${digest.stats.get('estimated_cost_usd', 0):.4f}"
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings()
    if not settings.database_path.exists():
        print(f"no database at {settings.database_path} - run `ai-digest run` first")
        return 1

    conn = db.connect(settings.database_path)
    try:
        dates = db.list_digest_dates(conn, limit=10)
        total = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
        print(f"database : {settings.database_path}")
        print(f"articles : {total}")
        print(f"digests  : {len(dates)} (latest: {dates[0] if dates else '-'})")

        runs = conn.execute(
            "SELECT id, started_at, finished_at, status, stats FROM runs "
            "ORDER BY id DESC LIMIT 5"
        ).fetchall()
        if runs:
            print("\nrecent runs:")
            for row in runs:
                print(
                    f"  #{row['id']:<4} {row['started_at'][:19]}  {row['status']:<7} "
                    f"{(row['finished_at'] or '')[:19]}"
                )
    finally:
        conn.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    settings = load_settings()
    if not settings.output_dir.exists():
        print(f"nothing to serve: {settings.output_dir} does not exist")
        return 1

    app = FastAPI(title="ai-digest preview")
    app.mount(
        "/",
        StaticFiles(directory=str(settings.output_dir), html=True),
        name="site",
    )
    print(f"serving {settings.output_dir} at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-digest", description="Daily AI-news digest pipeline."
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="fetch, dedupe, rank and render the digest")
    run.add_argument("--provider", choices=["deepseek", "fake"])
    run.add_argument("--out-dir")
    run.add_argument("--top-n", type=int)
    run.add_argument("--no-render", action="store_true")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show database and run history")
    status.set_defaults(func=cmd_status)

    serve = sub.add_parser("serve", help="preview the generated site locally")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
