"""Content hashes for folders of fixture files, so a fixture cannot change unnoticed.

The scheme follows CopperBench's idea (files sorted by path, each
contributing its path, length and bytes); the exact byte layout is
Kairo's own and is stated in `tree_sha256`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def tree_sha256(root: Path) -> str:
    """Hash every file under a folder, independent of the platform.

    Files are taken in order of their path relative to ``root``, written
    with ``/`` separators. Each contributes its path as UTF-8, a zero byte,
    its length in bytes as decimal digits, a zero byte, then its bytes. The
    hash is SHA-256 over all of that. Folders contribute nothing of their
    own, so an empty folder does not change the hash.

    Args:
        root: The folder to hash.

    Returns:
        The hex digest.

    Raises:
        NotADirectoryError: If ``root`` is not a folder.
    """
    if not root.is_dir():
        raise NotADirectoryError(root)
    digest = hashlib.sha256()
    paths = sorted(
        (p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file()
    )
    for relative, path in paths:
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()
