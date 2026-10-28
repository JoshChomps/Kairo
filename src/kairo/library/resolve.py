"""Turn ``REF.PIN`` in a design into real pins of a library symbol.

A pin name a model wrote is checked against the installed library before
anything is written, so an invented pin is an error naming the symbol's
real pins, never a wrong connection.

Resolution order:

1. the exact pin name;
2. the same name in another letter case, then the pin's alternate names;
3. an alias the project's owner approved (for example ``VIN`` for ``VI``);
4. the pin number, only for a pin that has no name of its own (or whose
   name is a number), such as a resistor's pins or a logic gate's
   unnamed input. A pin that has a name must be called by it, so a
   number recalled from memory for a named pin is refused.

A name that fits more than one pin is an error unless the design writes
``NAME*`` to mean all of them.
"""

from __future__ import annotations

from collections.abc import Mapping

from kairo.library.reader import Pin, Symbol


class PinError(ValueError):
    """A pin name in a design does not resolve.

    Attributes:
        code: ``E_PIN_UNKNOWN`` or ``E_PIN_AMBIGUOUS``.
        ref: The part's reference.
        pin: The pin name as the design wrote it.
        pins: The symbol's real pins, as ``name (number)``.
    """

    def __init__(
        self, code: str, ref: str, pin: str, message: str, pins: list[str]
    ) -> None:
        """Record what failed.

        Args:
            code: The error code.
            ref: The part's reference.
            pin: The pin name as written.
            message: What went wrong.
            pins: The symbol's real pins.
        """
        super().__init__(f"{code}: {message}")
        self.code = code
        self.ref = ref
        self.pin = pin
        self.pins = pins


def resolve_pin(
    symbol: Symbol,
    ref: str,
    name: str,
    *,
    unit: int = 1,
    aliases: Mapping[str, str] | None = None,
) -> list[Pin]:
    """Find the pin or pins a design means.

    Args:
        symbol: The part's symbol from the library index.
        ref: The part's reference, for error messages.
        name: The pin name the design wrote. A trailing ``*`` means every
            pin of that name.
        unit: The unit of the symbol the part is.
        aliases: Approved other names for pins, mapping alias to real name.

    Returns:
        One pin, or every pin of the name when the design wrote ``NAME*``.

    Raises:
        PinError: With ``E_PIN_UNKNOWN`` if nothing fits, or
            ``E_PIN_AMBIGUOUS`` if more than one pin fits and the design
            did not write ``NAME*``.
    """
    every = name.endswith("*") and len(name) > 1
    wanted = name[:-1] if every else name
    # Body style 2 is the alternate (De Morgan) drawing of the same pins.
    pins = [
        pin for pin in symbol.pins if pin.unit in (0, unit) and pin.body_style in (0, 1)
    ]
    real = [f"{pin.name or '(no name)'} ({pin.number})" for pin in pins]

    found = [pin for pin in pins if pin.name == wanted]
    if not found:
        found = [pin for pin in pins if pin.name.lower() == wanted.lower()]
    if not found:
        found = [
            pin
            for pin in pins
            if any(alt.name.lower() == wanted.lower() for alt in pin.alternates)
        ]
    if not found and aliases and wanted in aliases:
        found = [pin for pin in pins if pin.name == aliases[wanted]]
    if not found:
        found = [
            pin for pin in pins if pin.number == wanted and _numeric_or_empty(pin.name)
        ]

    if not found:
        raise PinError(
            "E_PIN_UNKNOWN",
            ref,
            name,
            f"{ref} ({symbol.name}) has no pin {wanted!r}. Its pins: "
            f"{', '.join(real) or 'none'}.",
            real,
        )
    if len(found) > 1 and not every:
        numbers = ", ".join(pin.number for pin in found)
        raise PinError(
            "E_PIN_AMBIGUOUS",
            ref,
            name,
            f"{ref} ({symbol.name}) has {len(found)} pins called {wanted!r} "
            f"(numbers {numbers}). Write {ref}.{wanted}* to mean all of them.",
            real,
        )
    return found


def pin_label(pin: Pin) -> str:
    """What to call a pin in a message: its name, or its number if it has none.

    Args:
        pin: The pin.

    Returns:
        The name, or the number for a pin whose name is empty or ``~``,
        which is how KiCad writes "no name".
    """
    return pin.number if pin.name in ("", "~") else pin.name


def _numeric_or_empty(name: str) -> bool:
    return name in ("", "~") or name.isdigit()
