"""Command line entry point.

Four verbs, intentionally: ``run`` does the work, ``render`` rebuilds the site
from the database without spending money, ``status`` tells you what happened
last time, ``serve`` lets you look at the output before pushing it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import db
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


def _rebuild_outputs(settings: Settings) -> tuple[list[Path], int]:
    """Regenerate the whole site (and per-day markdown) from the database."""
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    with db.open_db(settings.database_path) as conn:
        digests = db.list_digests(conn)
        paths = rebuild_site(conn, settings.output_dir)

        for digest in digests:
            md_path = (
                settings.output_dir / f"digest-{digest.digest_date.isoformat()}.md"
            )
            md_path.write_text(render_markdown(digest), encoding="utf-8")
            paths.append(md_path)

    return paths, len(digests)


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
        paths, _ = _rebuild_outputs(settings)
        for path in paths:
            log.info("wrote %s", path)

    print(
        f"\n{digest.digest_date}: {len(digest.items)} 条 · "
        f"候选 {digest.stats.get('candidates', 0)} · "
        f"去重后 {digest.stats.get('after_dedupe', 0)} · "
        f"${digest.stats.get('estimated_cost_usd', 0):.4f}"
    )
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Rebuild the static site from the existing database.

    No network, no LLM: this is the edit-preview loop for templates and CSS.
    Running the full pipeline to look at a colour change would cost an API call
    and 17 seconds every time.
    """
    settings = load_settings()
    if args.out_dir:
        settings.output_dir = Path(args.out_dir)

    if not settings.database_path.exists():
        print(f"no database at {settings.database_path} - run `ai-digest run` first")
        return 1

    with db.open_db(settings.database_path) as conn:
        digests = db.list_digests(conn)

    if not digests:
        print("database has no digests yet - run `ai-digest run` first")
        return 1

    paths, count = _rebuild_outputs(settings)
    for path in paths:
        log.info("wrote %s", path)
    print(f"rebuilt {len(paths)} files from {count} digest(s) -> {settings.output_dir}")
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


def cmd_eval_snapshot(args: argparse.Namespace) -> int:
    """Freeze the current candidate pool so later runs can replay it."""
    from .eval import dataset

    settings = load_settings()
    case_id = args.case_id or dataset.default_case_id()

    with db.open_db(settings.database_path) as conn:
        case = dataset.case_from_db(
            conn,
            case_id=case_id,
            max_age_days=settings.max_age_days,
            near_dup_threshold=settings.near_dup_threshold,
        )

    if not case.items:
        print("no candidates in the window - run `ai-digest run` first")
        return 1

    path = dataset.save_json(case, settings.eval_dir / "cases" / f"{case_id}.json")
    print(f"case {case_id}: {len(case.items)} items -> {path}")
    print(f"next:  ai-digest eval label {path}")
    return 0


def cmd_eval_annotate(args: argparse.Namespace) -> int:
    """Generate Chinese gists for every item so labelling is a content judgement."""
    from .eval import dataset
    from .eval.annotate import annotate_case

    settings = load_settings()
    if args.provider:
        settings.llm_provider = args.provider

    case_path = Path(args.case)
    case = dataset.load_case(case_path)

    print(f"annotating {len(case.items)} items with {settings.llm_provider} ...")
    annotations = asyncio.run(annotate_case(case, settings))

    out = dataset.save_json(annotations, dataset.annotations_path(case_path))
    missed = [
        item.id for item in case.items if item.id not in annotations.items
    ]
    print(
        f"wrote {out}  coverage {annotations.coverage(case):.0%} "
        f"({len(annotations.items)}/{len(case.items)})"
    )
    if missed:
        print(f"  ! {len(missed)} items got no gist and will show a warning in the page")
    print(f"next:  ai-digest eval label {args.case}")
    return 0 if not missed else 0


def cmd_eval_label(args: argparse.Namespace) -> int:
    """Generate a self-contained labelling page for a frozen case."""
    from .eval import dataset
    from .eval.labeler import build_labeler_html

    settings = load_settings()
    case_path = Path(args.case)
    case = dataset.load_case(case_path)

    annotations = None
    zh_path = args.zh and Path(args.zh) or dataset.annotations_path(case_path)
    if zh_path.exists():
        try:
            annotations = dataset.load_annotations(zh_path)
        except Exception as exc:  # noqa: BLE001 - a bad sidecar must not block labelling
            print(f"warning: could not read {zh_path}: {exc}")
    else:
        print(
            f"note: no Chinese gists yet ({zh_path.name}). "
            "Run `ai-digest eval annotate <case>` first for a much easier labelling pass."
        )

    out = settings.eval_dir / f"label-{case.case_id}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_labeler_html(case, annotations), encoding="utf-8")

    covered = annotations.coverage(case) if annotations else 0.0
    print(f"wrote {out}  ({len(case.items)} items, gists {covered:.0%})")
    if not args.no_open:
        import webbrowser

        webbrowser.open(out.resolve().as_uri())
    print(
        "标注 → 点『导出 labels.json』→ 然后:"
        f"\n  ai-digest eval score {args.case} <导出的 labels.json>"
    )
    return 0


def cmd_eval_score(args: argparse.Namespace) -> int:
    """Replay a case through the real ranker and score it against labels."""
    import json

    from .eval import dataset, runner

    settings = load_settings()
    if args.provider:
        settings.llm_provider = args.provider
    if args.k:
        settings.top_n = args.k

    case = dataset.load_case(Path(args.case))
    labels = dataset.load_labels(Path(args.labels))

    if labels.case_id != case.case_id:
        print(f"case mismatch: case={case.case_id} labels={labels.case_id}")
        return 1

    coverage = labels.coverage(case)
    report = asyncio.run(runner.evaluate_case(case, labels, settings, k=args.k))

    path = dataset.save_report(report, settings.eval_dir / "reports")
    print(f"\ncase {case.case_id} · {len(case.items)} items · provider {report['provider']}")
    print(f"labels: {int(coverage * len(case.items))}/{len(case.items)} ({coverage:.0%})"
          f" by {labels.labeler or 'unknown'}")
    if coverage < 0.9:
        print("  ! coverage below 90% - metrics use only labelled items as positives")

    print("\nmetrics")
    for key, value in report["metrics"].items():
        print(f"  {key:<22} {value}")

    # The numbers alone say whether it is good; the list below says *why*.
    k = report["k"]
    print(f"\ntop {k}")
    for row in report["ranking"][:k]:
        mark = "OK " if row["label"] == 1 else "·  "
        print(f"  {row['rank']:>2} {mark} {row['score']:>5.1f}  {row['title'][:64]}")

    missed = [
        row
        for row in report["ranking"][k:]
        if row["label"] == 1
    ]
    if missed:
        print(f"\nmissed relevant (rank > {k})")
        for row in missed:
            print(f"  {row['rank']:>2}     {row['score']:>5.1f}  {row['title'][:64]}")

    unpicked = [row for row in report["ranking"][:k] if row["label"] == 0]
    if unpicked:
        print(f"\nshould not be published")
        for row in unpicked:
            print(f"  {row['rank']:>2}     {row['score']:>5.1f}  {row['title'][:64]}")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        print(f"\nvs {Path(args.baseline).name}")
        for key, row in runner.compare(report, baseline).items():
            arrow = "+" if row["delta"] > 0 else ("=" if row["delta"] == 0 else "")
            print(
                f"  {key:<22} {row['baseline']:>8} -> {row['current']:<8} {arrow}{row['delta']}"
            )

    print(f"\nreport: {path}")

    if args.min_precision is not None:
        precision = report["metrics"].get(f"precision@{report['k']}", 0.0)
        if precision < args.min_precision:
            print(
                f"\nFAIL: precision@{report['k']} {precision} < {args.min_precision}"
            )
            return 1
        print(f"\nOK: precision@{report['k']} {precision} >= {args.min_precision}")

    if args.min_lift is not None:
        lift = report["metrics"].get("lift_over_random", 0.0)
        if lift < args.min_lift:
            print(f"\nFAIL: lift over random {lift} < {args.min_lift}")
            return 1
        print(f"\nOK: lift over random {lift} >= {args.min_lift}")
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

    render = sub.add_parser(
        "render", help="rebuild the site from the database (no fetch, no LLM)"
    )
    render.add_argument("--out-dir")
    render.set_defaults(func=cmd_render)

    serve = sub.add_parser("serve", help="preview the generated site locally")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    ev = sub.add_parser(
        "eval", help="offline evaluation: freeze a case, label it, score the ranker"
    )
    ev_sub = ev.add_subparsers(dest="eval_command", required=True)

    snap = ev_sub.add_parser("snapshot", help="freeze the current candidate pool")
    snap.add_argument("--case-id", default=None)
    snap.set_defaults(func=cmd_eval_snapshot)

    ann = ev_sub.add_parser(
        "annotate", help="generate Chinese gists so labelling is about content"
    )
    ann.add_argument("case")
    ann.add_argument("--provider", choices=["deepseek", "fake"])
    ann.set_defaults(func=cmd_eval_annotate)

    lab = ev_sub.add_parser("label", help="generate the labelling page for a case")
    lab.add_argument("case")
    lab.add_argument("--zh", default=None, help="annotation sidecar (auto-detected)")
    lab.add_argument("--no-open", action="store_true")
    lab.set_defaults(func=cmd_eval_label)

    sc = ev_sub.add_parser("score", help="replay a case and compute metrics")
    sc.add_argument("case")
    sc.add_argument("labels")
    sc.add_argument("--provider", choices=["deepseek", "fake"])
    sc.add_argument("--k", type=int, default=None)
    sc.add_argument("--baseline", default=None, help="previous report json")
    sc.add_argument("--min-precision", type=float, default=None)
    sc.add_argument(
        "--min-lift",
        type=float,
        default=None,
        help="fail unless precision@k beats random by this factor (more robust than raw precision)",
    )
    sc.set_defaults(func=cmd_eval_score)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
