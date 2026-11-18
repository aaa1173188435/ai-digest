"""Allow `python -m ai_digest ...` in CI without relying on console scripts."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
