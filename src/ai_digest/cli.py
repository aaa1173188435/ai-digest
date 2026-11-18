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

    out_dir = Path(args.out) if args.out else settings.eval_dir
    out = out_dir / f"label-{case.case_id}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        build_labeler_html(case, annotations, slots=settings.top_n), encoding="utf-8"
    )

    covered = annotations.coverage(case) if annotations else 0.0
    print(
        f"wrote {out}  ({len(case.items)} items, gists {covered:.0%}, "
        f"rubric: 只有 {settings.top_n} 个位置)"
    )
    if not args.no_open:
        import webbrowser

        webbrowser.open(out.resolve().as_uri())
    print(
        "标注 → 点『导出 labels.json』→ 然后:"
        f"\n  ai-digest eval score {args.case} <导出的 labels.json>"
    )
    return 0


def _newest_export() -> Path | None:
    """Newest ``labels-*.json`` in the Downloads folder, if there is one.

    The labelling page downloads to wherever the browser puts downloads, and
    asking a human to move a file is exactly the step where the feedback loop
    dies. So the tool looks in the obvious place.
    """
    downloads = Path.home() / "Downloads"
    if not downloads.exists():
        return None
    candidates = sorted(
        downloads.glob("labels-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def cmd_eval_import(args: argparse.Namespace) -> int:
    """Validate an exported label file and file it under eval/labels/."""
    from .eval import dataset

    settings = load_settings()

    source = Path(args.source) if args.source else _newest_export()
    if source is None:
        print("no labels-*.json found in Downloads - pass a path explicitly")
        return 1
    if not source.exists():
        print(f"not found: {source}")
        return 1

    labels = dataset.load_labels(source)
    case_path = settings.eval_dir / "cases" / f"{labels.case_id}.json"
    if not case_path.exists():
        print(f"no case for case_id={labels.case_id} at {case_path}")
        print("labels must match a frozen case; run `ai-digest eval snapshot` first")
        return 1

    case = dataset.load_case(case_path)
    known = set(case.ids())
    unknown = [item_id for item_id in labels.labels if item_id not in known]
    if unknown:
        print(f"FAIL: {len(unknown)} labelled ids are not in the case (stale export?)")
        return 1

    dest = settings.eval_dir / "labels" / source.name
    if dest.exists():
        print(f"replacing existing {dest}")
    dataset.save_json(labels, dest)

    must = sum(1 for value in labels.labels.values() if value == 1)
    print(f"imported {source.name}")
    print(f"  case      : {case.case_id}")
    print(f"  labelled  : {len(labels.labels)}/{len(case.items)} ({labels.coverage(case):.0%})")
    print(f"  必须留    : {must}  (位置 {settings.top_n})")
    if must > settings.top_n:
        print(f"  ! {must} 条超过 {settings.top_n} 个位置，rubric 可能又松了")
    print(f"  saved to  : {dest}")
    print(f"\nnext:  ai-digest eval score {case_path} {dest}")
    return 0


def cmd_eval_report(args: argparse.Namespace) -> int:
    """Score every labelled case and print one compact table.

    This exists to keep the feedback loop cheap: one run per batch of labels
    instead of one per day, and a summary that is a page rather than ten.
    """
    from .eval import dataset, runner

    settings = load_settings()
    if args.provider:
        settings.llm_provider = args.provider
    k = args.k or settings.top_n

    cases_dir = settings.eval_dir / "cases"
    labels_dir = settings.eval_dir / "labels"
    if not cases_dir.exists() or not labels_dir.exists():
        print(f"no cases/labels under {settings.eval_dir}")
        return 1

    cases = {}
    for path in sorted(cases_dir.glob("*.json")):
        if path.name.endswith(".zh.json"):
            continue
        case = dataset.load_case(path)
        cases[case.case_id] = case

    # Newest label file per case wins: re-labelling under a new rubric should
    # replace the old answer, not compete with it.
    newest: dict[str, Path] = {}
    for path in labels_dir.glob("*.json"):
        try:
            labels = dataset.load_labels(path)
        except Exception:  # noqa: BLE001 - skip unreadable files, report the rest
            print(f"skip unreadable label file: {path.name}")
            continue
        if labels.case_id not in cases:
            continue
        current = newest.get(labels.case_id)
        if current is None or path.stat().st_mtime > current.stat().st_mtime:
            newest[labels.case_id] = path

    if not newest:
        print("no labels match any case yet")
        return 1

    if args.max_cases and len(newest) > args.max_cases:
        ordered = sorted(newest.items())[-args.max_cases :]
        newest = dict(ordered)

    print(f"scoring {len(newest)} case(s) at k={k} with {settings.llm_provider} ...\n")

    selected = [(case_id, cases[case_id], dataset.load_labels(newest[case_id]))
                for case_id in sorted(newest)]

    async def score_all() -> list[dict]:
        rows: list[dict] = []
        for case_id, case, labels in selected:
            report = await runner.evaluate_case(case, labels, settings, k=k)
            rows.append(report)
            print(
                f"  {case_id}  must={report['metrics']['relevant_total']:>2}  "
                f"P@{k}={report['metrics'][f'precision@{k}']:<6} "
                f"lift={report['metrics']['lift_over_random']}"
            )
        return rows

    rows = asyncio.run(score_all())

    totals = runner.aggregate(rows, k)

    print("\n" + "-" * 58)
    print(f"cases           : {totals['cases']}")
    print(f"mean precision@{k}: {totals[f'mean_precision@{k}']}")
    print(f"mean nDCG@{k}     : {totals[f'mean_ndcg@{k}']}")
    print(
        f"mean lift       : {totals['mean_lift']}"
        f"  (min {totals['min_lift']} / max {totals['max_lift']})"
    )

    summary_json = dataset.save_report(
        {
            "case_id": "summary",
            "metrics": totals,
            "cases": [r["case_id"] for r in rows],
        },
        settings.eval_dir / "reports",
    )
    markdown = runner.render_summary_markdown(rows, totals, k)
    summary_md = settings.eval_dir / "reports" / "summary.md"
    summary_md.parent.mkdir(parents=True, exist_ok=True)
    summary_md.write_text(markdown, encoding="utf-8")

    print(f"\nsummary: {summary_md}")
    print(f"raw    : {summary_json}")
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
    lab.add_argument("--out", default=None, help="output directory")
    lab.add_argument("--no-open", action="store_true")
    lab.set_defaults(func=cmd_eval_label)

    imp = ev_sub.add_parser(
        "import", help="file an exported labels json under eval/labels/"
    )
    imp.add_argument("source", nargs="?", default=None, help="path (default: newest in Downloads)")
    imp.set_defaults(func=cmd_eval_import)

    rep = ev_sub.add_parser(
        "report", help="score every labelled case; one compact table"
    )
    rep.add_argument("--k", type=int, default=None)
    rep.add_argument("--provider", choices=["deepseek", "fake"])
    rep.add_argument(
        "--max-cases", type=int, default=None, help="only the newest N cases"
    )
    rep.set_defaults(func=cmd_eval_report)

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
