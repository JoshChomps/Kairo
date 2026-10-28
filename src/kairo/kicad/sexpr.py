r"""Read and write KiCad S-expression files without changing a byte Kairo did not touch.

KiCad's schematics, symbol and footprint libraries, boards, worksheets,
design rules and library tables are all S-expressions. This module parses
them into a concrete syntax tree that keeps every token exactly as written,
together with the whitespace before it, so printing an untouched tree gives
back the original text. An edit replaces one node; everything else prints
as it was, which keeps diffs small and never reformats a person's file.

What the parser accepts was taken from a survey of every S-expression file
in a KiCad 10.0.6 install (16,291 files, 670 MB, 2026-10-06):

- Whitespace between tokens is spaces, tabs, carriage returns and line
  feeds. KiCad 10.0.6 on Windows writes CRLF line endings; both kinds are
  kept as they are.
- A token is ``(``, ``)``, a quoted string, or a bare atom (a run of
  characters that are none of those and not whitespace).
- Inside quoted strings the only escapes are ``\\``, ``\"`` and ``\n``.
  Any other escape is refused when the string's value is read, since none
  occurs in KiCad's own files; the raw text still round-trips.
- Files are UTF-8 without a byte-order mark.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

#: Characters that separate tokens.
WHITESPACE = " \t\r\n"

# One token with the whitespace before it. The last alternative matches an
# unterminated string's opening quote or the end of the text, so every
# position either yields a token or tells the parser to stop.
_TOKEN = re.compile(
    r'([ \t\r\n]*)(?:(\()|(\))|("[^"\\]*(?:\\.[^"\\]*)*")|([^ \t\r\n()"]+)|("|\Z))',
    re.DOTALL,
)
_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
_DECODE = {"\\": "\\", '"': '"', "n": "\n"}
_BARE_ATOM = re.compile(r'[^ \t\r\n()"]+')


class SexprError(ValueError):
    """The text is not a well-formed S-expression, or a value cannot be decoded."""


@dataclass(eq=False, slots=True)
class Atom:
    """A bare atom or a quoted string, exactly as written.

    Attributes:
        raw: The token's text in the file, with quotes and escapes for a
            string.
        before: The whitespace before it.
    """

    raw: str
    before: str = ""

    @property
    def quoted(self) -> bool:
        """True if this is a quoted string."""
        return self.raw.startswith('"')

    @property
    def value(self) -> str:
        """The atom's text, or a string's contents with escapes decoded.

        Raises:
            SexprError: If the string holds an escape KiCad does not write.
        """
        if not self.quoted:
            return self.raw
        return decode_string(self.raw[1:-1])

    @classmethod
    def symbol(cls, text: str, before: str = " ") -> Atom:
        """Make a bare atom, such as ``yes`` or ``1.27``.

        Args:
            text: The atom's text.
            before: Whitespace to print before it.

        Returns:
            The atom.

        Raises:
            SexprError: If the text would not read back as one bare atom.
        """
        if not _BARE_ATOM.fullmatch(text):
            raise SexprError(f"{text!r} cannot be a bare atom")
        return cls(text, before)

    @classmethod
    def string(cls, value: str, before: str = " ") -> Atom:
        """Make a quoted string holding ``value``.

        Args:
            value: The text the string should hold.
            before: Whitespace to print before it.

        Returns:
            The quoted atom, escaped the way KiCad escapes strings.
        """
        return cls(f'"{encode_string(value)}"', before)


@dataclass(eq=False, slots=True)
class SList:
    """A parenthesised list: ``(head item item ...)``.

    Attributes:
        items: What is inside, in order.
        before: The whitespace before the opening parenthesis.
        end: The whitespace between the last item and the closing
            parenthesis.
    """

    items: list[Node] = field(default_factory=lambda: [])
    before: str = ""
    end: str = ""

    @property
    def head(self) -> str | None:
        """The first item's value if it is a bare atom, as in ``(version ...)``."""
        if self.items and isinstance(self.items[0], Atom) and not self.items[0].quoted:
            return self.items[0].raw
        return None

    def lists(self, head: str | None = None) -> list[SList]:
        """The lists directly inside this one, optionally only those with a head.

        Args:
            head: Keep only lists whose head is this, if given.

        Returns:
            The matching lists, in order.
        """
        return [
            item
            for item in self.items
            if isinstance(item, SList) and (head is None or item.head == head)
        ]

    def first(self, head: str) -> SList | None:
        """The first list directly inside this one with the given head.

        Args:
            head: The head to look for.

        Returns:
            The list, or ``None`` if there is none.
        """
        for item in self.items:
            if isinstance(item, SList) and item.head == head:
                return item
        return None

    def values(self) -> list[str]:
        """The values of the atoms after the head, ignoring nested lists."""
        return [item.value for item in self.items[1:] if isinstance(item, Atom)]


Node = Atom | SList


@dataclass(eq=False, slots=True)
class Document:
    """A whole file: its top-level nodes and the whitespace after them.

    Attributes:
        items: Top-level nodes; KiCad files have exactly one list.
        end: Whitespace after the last node, usually the final line ending.
    """

    items: list[Node]
    end: str = ""

    @property
    def root(self) -> SList:
        """The single top-level list.

        Raises:
            SexprError: If the file does not hold exactly one top-level list.
        """
        if len(self.items) != 1 or not isinstance(self.items[0], SList):
            raise SexprError("expected exactly one top-level list")
        return self.items[0]

    def to_text(self) -> str:
        """Print the document; untouched parts come out exactly as parsed."""
        out: list[str] = []
        for item in self.items:
            _print(item, out)
        out.append(self.end)
        return "".join(out)


def parse(text: str) -> Document:
    """Parse S-expression text into a lossless tree.

    Args:
        text: The file's contents, decoded from UTF-8 with line endings
            untouched (read with ``newline=""`` or from bytes).

    Returns:
        The document. ``parse(t).to_text() == t`` for any text this accepts.

    Raises:
        SexprError: On an unbalanced parenthesis, an unterminated string, or
            anything that is not whitespace, a parenthesis, a string or an
            atom. The message gives the line and column.
    """
    top: list[Node] = []
    stack: list[SList] = []
    items = top
    for match in _TOKEN.finditer(text):
        before = match.group(1)
        kind = match.lastindex
        if kind == 2:
            node = SList(before=before)
            items.append(node)
            stack.append(node)
            items = node.items
        elif kind == 3:
            if not stack:
                raise SexprError(f"unmatched ')' at {_where(text, match.start(3))}")
            stack.pop().end = before
            items = stack[-1].items if stack else top
        elif kind == 4 or kind == 5:
            items.append(Atom(match.group(kind), before))
        elif match.group(6) == '"':
            raise SexprError(f"unterminated string at {_where(text, match.start(6))}")
        else:
            if stack:
                raise SexprError(f"{len(stack)} unclosed '(' at end of text")
            return Document(top, before)
    raise AssertionError("the token pattern always matches the end of the text")


def parse_file(path: str | os.PathLike[str]) -> Document:
    """Parse a file, reading it as UTF-8 with line endings kept.

    Args:
        path: The file to read.

    Returns:
        The document.

    Raises:
        SexprError: If the file is not a well-formed S-expression.
        UnicodeDecodeError: If it is not UTF-8.
    """
    with open(path, encoding="utf-8", newline="") as handle:
        return parse(handle.read())


def to_text(node: Node) -> str:
    """Print one node with the whitespace it carries.

    Args:
        node: An atom or list.

    Returns:
        Its text.
    """
    out: list[str] = []
    _print(node, out)
    return "".join(out)


def decode_string(body: str) -> str:
    r"""Decode the inside of a quoted string.

    Args:
        body: The text between the quotes, as written in the file.

    Returns:
        The string's value.

    Raises:
        SexprError: If it holds an escape other than ``\\``, ``\"`` or
            ``\n``.
    """

    def replace(match: re.Match[str]) -> str:
        char = match.group(1)
        if char not in _DECODE:
            raise SexprError(f"unknown escape \\{char} in string {body[:40]!r}")
        return _DECODE[char]

    return _ESCAPE.sub(replace, body)


def encode_string(value: str) -> str:
    """Escape a value for writing inside quotes, the inverse of `decode_string`.

    Args:
        value: The text to store.

    Returns:
        The text to put between the quotes.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _print(node: Node, out: list[str]) -> None:
    # An explicit stack, not recursion: the parser accepts any nesting depth,
    # so the printer must too. A string on the stack is a list's closing text.
    stack: list[Node | str] = [node]
    append = out.append
    while stack:
        item = stack.pop()
        # Atoms are by far the most common, so they are tested first.
        if isinstance(item, Atom):
            append(item.before)
            append(item.raw)
        elif isinstance(item, str):
            append(item)
        else:
            append(item.before)
            append("(")
            stack.append(item.end + ")")
            stack.extend(item.items[::-1])


def _where(text: str, index: int) -> str:
    line = text.count("\n", 0, index) + 1
    column = index - (text.rfind("\n", 0, index) + 1) + 1
    return f"line {line}, column {column}"
