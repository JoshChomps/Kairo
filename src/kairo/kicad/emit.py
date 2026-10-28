"""Build new S-expression trees and lay them out the way KiCad's writer does.

`kairo.kicad.sexpr` keeps the whitespace of a file it read. A tree Kairo
builds has none, so `lay_out` gives it KiCad's: one tab per level, every
nested list on its own line, the closing parenthesis of a list that holds
lists on a line of its own, and consecutive ``(xy ...)`` points kept on
one line until the line reaches 99 characters.

Those rules are measured, not taken from KiCad's source: applied to files
KiCad wrote, they give the same bytes back for all 223 symbol libraries in
a KiCad 10.0.6 install and for 114 of the 149 real-board sheets saved by
the KiCad 9 and 10 programs. The other 35 sheets hold what the rules do
not cover, embedded image data and long ``(members ...)`` lists of bus
aliases, where KiCad wraps runs of quoted strings; Kairo writes neither.
"""

from __future__ import annotations

from kairo.kicad.sexpr import Atom, Node, SList

#: A run of ``(xy ...)`` points continues on one line while the line is
#: shorter than this, counting each tab as one character.
XY_COLUMN_LIMIT = 99

Item = Node | str | int | float


def sym(text: str) -> Atom:
    """A bare word such as ``yes`` or ``power_in``.

    Args:
        text: The word.

    Returns:
        The atom.
    """
    return Atom.symbol(text)


def number(value: float) -> Atom:
    """A number as KiCad writes lengths: at most four decimals, no padding.

    KiCad's schematic unit is 100 nm, so four decimals of a millimetre
    hold any position exactly.

    Args:
        value: The number.

    Returns:
        The atom, for example ``1.27`` or ``-3.81`` or ``0``.
    """
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return Atom.symbol("0" if text in ("-0", "") else text)


def lst(head: str, *items: Item) -> SList:
    """A list ``(head item ...)``.

    Args:
        head: The list's first word.
        *items: What follows. A ``str`` becomes a quoted string, an ``int``
            or ``float`` a number, and atoms and lists are used as given.

    Returns:
        The list, with no whitespace set; see `lay_out`.
    """
    nodes: list[Node] = [Atom.symbol(head)]
    for item in items:
        if isinstance(item, str):
            nodes.append(Atom.string(item))
        elif isinstance(item, int | float):
            nodes.append(number(item))
        else:
            nodes.append(item)
    return SList(nodes)


def copy(node: Node) -> Node:
    """A deep copy of a node, so a tree read from one file can go in another.

    Args:
        node: The node to copy.

    Returns:
        A new node with the same tokens.
    """
    if isinstance(node, Atom):
        return Atom(node.raw, node.before)
    return SList([copy(item) for item in node.items], node.before, node.end)


def lay_out(node: SList, depth: int = 0, newline: str = "\n") -> SList:
    """Set a tree's whitespace to KiCad's layout, in place.

    Args:
        node: The list to lay out. Whitespace it already has is replaced.
        depth: How many lists enclose it, which sets its indentation.
        newline: The line ending to use.

    Returns:
        The same list. Its own leading whitespace is left for the caller,
        who knows what comes before it.
    """
    _lay_out(node, depth, newline, depth)
    return node


def _lay_out(node: SList, depth: int, newline: str, column: int) -> int:
    """Lay one list out and return the column after its closing parenthesis."""
    column += 1
    nested = False
    previous_xy = False
    for index, item in enumerate(node.items):
        if isinstance(item, Atom):
            item.before = " " if index else ""
            column += len(item.before) + len(item.raw)
            previous_xy = False
            continue
        is_xy = item.head == "xy"
        if is_xy and previous_xy and column < XY_COLUMN_LIMIT:
            item.before = " "
            column += 1
        else:
            item.before = newline + "\t" * (depth + 1)
            column = depth + 1
        nested = True
        column = _lay_out(item, depth + 1, newline, column)
        previous_xy = is_xy
    if nested:
        node.end = newline + "\t" * depth
        column = depth
    else:
        node.end = ""
    return column + 1
