"""The size of text as KiCad draws it on a schematic.

KiCad's schematic font is not fixed-width, so a text's length is the sum
of its characters' advances plus a constant. Both are measured from
KiCad's own drawings (``kicad-cli sch export svg`` states each text's
length), by ``evals/text_placement/calibrate.py``: every printable ASCII
character was drawn 4 and 8 times, and the difference gives its advance.
On KiCad 10.0.6 every advance is a whole number of 21sts of the text
size, the constant is 0.16 of the text size for every character, and the
two ways of working out the constant agree exactly.

Vertically, measured the same way from the ink of the drawn strokes: text
anchored at its centre reaches at most 0.883 of its size above the anchor
(brackets) and 0.640 below (brackets and descenders), and 1.018 above
when it carries an overbar (``~{...}``).
"""

from __future__ import annotations

#: KiCad's default schematic text size, in mm.
DEFAULT_SIZE = 1.27
#: Added to every text's length, in units of the text size.
LENGTH_CONSTANT = 0.16
#: Each character's advance in 21sts of the text size.
ADVANCE_21: dict[str, int] = {
    " ": 16,
    "!": 10,
    '"': 16,
    "#": 21,
    "$": 20,
    "%": 24,
    "&": 26,
    "'": 10,
    "(": 14,
    ")": 14,
    "*": 16,
    "+": 26,
    ",": 10,
    "-": 26,
    ".": 10,
    "/": 22,
    "0": 20,
    "1": 20,
    "2": 20,
    "3": 20,
    "4": 20,
    "5": 20,
    "6": 20,
    "7": 20,
    "8": 20,
    "9": 20,
    ":": 10,
    ";": 10,
    "<": 26,
    "=": 26,
    ">": 26,
    "?": 18,
    "@": 27,
    "A": 18,
    "B": 21,
    "C": 21,
    "D": 21,
    "E": 19,
    "F": 18,
    "G": 21,
    "H": 22,
    "I": 10,
    "J": 16,
    "K": 21,
    "L": 17,
    "M": 24,
    "N": 22,
    "O": 22,
    "P": 21,
    "Q": 22,
    "R": 21,
    "S": 20,
    "T": 16,
    "U": 22,
    "V": 18,
    "W": 24,
    "X": 20,
    "Y": 18,
    "Z": 20,
    "[": 14,
    "\\": 14,
    "]": 14,
    "^": 12,
    "_": 16,
    "`": 8,
    "a": 19,
    "b": 19,
    "c": 18,
    "d": 19,
    "e": 18,
    "f": 12,
    "g": 19,
    "h": 19,
    "i": 10,
    "j": 10,
    "k": 17,
    "l": 11,
    "m": 28,
    "n": 19,
    "o": 19,
    "p": 19,
    "q": 19,
    "r": 13,
    "s": 17,
    "t": 12,
    "u": 19,
    "v": 16,
    "w": 22,
    "x": 17,
    "y": 16,
    "z": 17,
    "{": 14,
    "|": 20,
    "}": 14,
    "~": 15,
}
#: The widest advance, used for a character that was not measured.
WIDEST_21 = max(ADVANCE_21.values())
#: How far ink reaches above and below a centred anchor, in units of the
#: text size, rounded up from the measurements above.
REACH = 0.9
REACH_WITH_OVERBAR = 1.05


def text_length(text: str, size: float = DEFAULT_SIZE) -> float:
    """The length KiCad gives a line of text.

    Markup is measured as if written out: the braces of an overbar,
    subscript or superscript take room here though KiCad draws nothing for
    them, and small script is counted at full size, so the result is never
    less than what KiCad draws. A character outside printable ASCII is
    counted as the widest measured character.

    The result is exact at the default size, which is the size Kairo
    writes: on three real boards every plain text of that size (5,139 on
    one of them) has exactly this length in KiCad's drawing. At other
    sizes it is within a few percent, because KiCad's stroke width does
    not scale in step with the size.

    Args:
        text: One line of text.
        size: The text size, in mm.

    Returns:
        The length in mm.
    """
    units = sum(ADVANCE_21.get(character, WIDEST_21) for character in text)
    return round(size * (LENGTH_CONSTANT + units / 21), 4)


def text_height(text: str, size: float = DEFAULT_SIZE) -> float:
    """The height to allow for a line of text centred on its anchor.

    Args:
        text: One line of text.
        size: The text size, in mm.

    Returns:
        The height in mm: twice the farthest the ink reaches from the
        anchor, so a box of this height centred on the anchor holds it.
    """
    reach = REACH_WITH_OVERBAR if "~{" in text else REACH
    return round(2 * reach * size, 4)
