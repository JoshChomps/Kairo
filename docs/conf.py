"""Sphinx configuration for Kairo's documentation.

Build with ``uv run sphinx-build -W docs docs/_build/html``.
"""

import kairo.server  # noqa: F401
from kairo import __version__

# The server is imported here, before the pages are read, on purpose. When
# autodoc imports a module it first tries with typing.TYPE_CHECKING turned
# on, and the MCP library's models cannot be built in that state; with the
# server (and so the MCP library) already loaded, they are not built again.

project = "Kairo"
author = "Josh"
copyright = "2026, Josh. All rights reserved"
release = __version__
version = __version__

extensions = [
    "myst_parser",  # pages are written in Markdown
    "sphinx.ext.autodoc",  # API pages come from the docstrings
    "sphinx.ext.napoleon",  # docstrings use Google style
]

# Saved third-party pages and old plan revisions live under docs/ but are
# records, not pages of the site.
exclude_patterns = ["_build", "history", "sources"]

napoleon_google_docstring = True
napoleon_numpy_docstring = False
autodoc_typehints = "description"
autodoc_member_order = "bysource"

myst_enable_extensions = ["colon_fence"]
myst_heading_anchors = 3

html_theme = "furo"
html_title = f"Kairo {version}"
