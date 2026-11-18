"""Static site rendering.

The output is a plain folder of HTML files -- no build step, no JS framework --
so GitHub Pages can serve it directly and the whole thing stays readable in ten
years.

The site is rebuilt from the database on every run rather than incrementally
patching files. One consequence matters for CI: the database is the only state
that has to survive between runs, and the published history can always be
regenerated from it.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import db
from ..models import Digest

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_digest_html(digest: Digest, *, prefix: str, archive_dates: list[str]) -> str:
    return _environment().get_template("digest.html.j2").render(
        digest=digest, prefix=prefix, archive_dates=archive_dates
    )


def rebuild_site(conn, output_dir: Path) -> list[Path]:
    """Regenerate index, every dated page, and the archive index from the DB."""
    env = _environment()
    output_dir = Path(output_dir)
    (output_dir / "digests").mkdir(parents=True, exist_ok=True)

    digests = db.list_digests(conn)
    archive_dates = [d.digest_date.isoformat() for d in digests]
    template = env.get_template("digest.html.j2")
    written: list[Path] = []

    for digest in digests:
        path = output_dir / "digests" / f"{digest.digest_date.isoformat()}.html"
        path.write_text(
            template.render(digest=digest, prefix="../", archive_dates=archive_dates),
            encoding="utf-8",
        )
        written.append(path)

    if digests:
        index_path = output_dir / "index.html"
        index_path.write_text(
            template.render(
                digest=digests[0], prefix="", archive_dates=archive_dates
            ),
            encoding="utf-8",
        )
        written.append(index_path)

    archive_path = output_dir / "archive.html"
    archive_path.write_text(
        env.get_template("archive.html.j2").render(dates=archive_dates),
        encoding="utf-8",
    )
    written.append(archive_path)

    # Tell GitHub Pages to skip Jekyll processing.
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return written
