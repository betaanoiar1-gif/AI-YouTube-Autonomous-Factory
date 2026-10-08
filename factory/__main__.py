"""Allow ``python -m factory`` to invoke the CLI."""

from factory.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
