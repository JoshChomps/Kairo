"""Allow ``python -m kairo`` as well as the ``kairo`` command."""

from kairo.cli import main

# Guarded because the library index starts worker processes, and on Windows
# each worker imports this module again under another name.
if __name__ == "__main__":
    raise SystemExit(main())
