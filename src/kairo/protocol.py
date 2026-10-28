"""The order of work Kairo asks a model to follow, given on one keyword.

A person starts a design by saying "Kairo" or "kairo go" and what they
want. The model then calls the ``go`` tool, or the person picks the
``go`` prompt from the client's menu, and gets the steps below for the
tier the server runs at. The steps are Kairo's, so the order of work
does not depend on what the model would have thought to do: ask before
assuming, take pins from the library and numbers from the calculations,
check before writing, and report what is not established.

The steps name only tools the tier offers; a test holds that.
"""

from __future__ import annotations

from typing import Any

#: What a person says to start. Matched by the model, not by code: the
#: server's instructions tell it what these words mean.
KEYWORDS = ("Kairo", "kairo go")

#: The line in the server's instructions that gives the keyword its meaning.
INSTRUCTION = (
    'A message starting "Kairo" or "kairo go" asks to design or check a '
    "circuit: call go first with it, then follow its steps in order."
)

_ASK = (
    "Say back in a sentence or two what is asked. List what the request "
    "leaves out that a design needs (a supply's voltage and tolerance, a "
    "load's current, an ambient temperature, a part's datasheet values) "
    "and ask the user for each. Never choose one yourself: a value you "
    "cannot get is left out, and Kairo will report the result that needs "
    "it as unverified."
)
_EXISTING = (
    "If the request is about a schematic that already exists, find it with "
    "list_projects and read it with project_summary and read_schematic."
)
_SCHEMA = (
    "Call design_schema once for the shape of a design. For each part, "
    "find its symbol with find_symbol and its footprint with "
    "find_footprint, and take pin names and numbers only from "
    "get_symbol_pins. For a resistor, capacitor, inductor or LED, choose "
    "a real part with find_part and give its number as params.mpn: its "
    "tolerance and ratings then come from the catalogue. find_part also "
    "finds regulators (kind regulator), for choosing only. For a chip, "
    "fetch its datasheet with fetch_datasheet (give its symbol), read its "
    "limits with read_datasheet and give each as a param with its page as "
    "source; "
    "save_part keeps them under the part number, so the next design "
    "need only name the part."
)
_CALC = (
    "Work out every value with calc (a divider, a current-limit resistor, "
    "a dissipation), never by your own arithmetic, and keep each result's "
    "source and worst case for the report."
)
_PLAN = (
    "Call plan_design with the design. Fix what it rejects and call it "
    "again. A requirement that fails means the design does not meet what "
    "was asked: change the design, or tell the user it cannot be met and "
    "show the arithmetic. Do not loosen a requirement to make it pass."
)
_DRAWN = (
    "For a schematic a person drew, call check_schematic with the facts "
    "the user has stated (each rail's role and voltage, each load's "
    "current, each part's datasheet values) in place of plan_design. "
    "State no fact the user or a datasheet did not give."
)
_SIMULATE = (
    "Where the request turns on behaviour in time or frequency (a rail "
    "coming up, a live supply plugged in, a filter's cutoff), call "
    "simulate. A step's rise time and source impedance come from the "
    "user, not from you."
)
_WRITE = (
    "Call apply_design to build the project, or edit_design to change one "
    "Kairo built. In a schematic a person drew, set_value changes one "
    "value and edit_schematic adds, removes or reconnects parts; "
    "save_facts keeps the facts the user stated in the project, so they "
    "are not asked for again. If the write is refused, read why, fix "
    "that, and try "
    "again; when a tool says stop, stop and tell the user."
)
_BOARD = (
    "Where the user wants the board too, give the design a board "
    "(design_schema with part 'board'): its size, layers and copper "
    "weight; each block's compass side; connectors and a radio module's "
    "antenna on edges, facing where the cable or the air is; pours for "
    "the nets that carry high current, only over the blocks it flows "
    "through; net classes with clearances from calc, and for a net that "
    "needs a set impedance (USB, Ethernet, RF) its width from calc "
    "microstrip_width or stripline_width with the laminate's er and the "
    "stack's heights from the user's maker; the maker's rules as "
    "the user's maker publishes them; spacing (B2 for a bare board, B4 "
    "for a coated one) with every net's voltage stated; and each power "
    "net's current. Blocks decide placement: put each small part "
    "(capacitor, resistor, diode) in the block of the IC whose pins it "
    "serves, and Kairo seats it at those pins; a part that must sit "
    "elsewhere (a sensor on what it measures, a gate resistor at its FET) "
    "goes in that block instead. Kairo itself seats those parts, keeps "
    "parallel parts in a row, keeps room in front of fine-pitch pins, "
    "places labels, routes, mends the last gaps, and checks spacing and "
    "current: do not try to do these by hand. Add no part the schematic "
    "lacks, and say which choices are your inference. Then call "
    "make_board once and read its summary in this order: blocking, then "
    "blocks (parts placed away from the only IC they serve: follow each "
    "fix or say why the part stays), then unconnected and what next says, "
    "then current_fails, detours and spacing_unchecked. Call it again "
    "only to act on what the summary names (the places are kept; "
    "replace_places places again). When a connection cannot be made, "
    "change the placement (blocks, sides, edges, board size), never the "
    "rules. Name nets in board.routes only for what the router cannot "
    "know (a sense pair kept on one layer, a gate drive kept short), not "
    "to close gaps: a net routed first pushes the others aside."
)
_SHOW = (
    "Call open_in_kicad so the user can look at the project in KiCad. If "
    "it was open already, tell them to revert or reopen the sheet."
)
_REPORT = (
    "Report in this order: what was built or checked and where it is; "
    "each requirement's result, naming any that passed on Kairo's "
    "default margins and which figures to state to tighten them; every "
    "unverified result with its "
    "to_resolve lines unchanged; the review's faults, then its "
    "suggestions, saying that a suggestion is Kairo's preference and not "
    "an error. An unverified result is never reported as a pass."
)
_READ_ONLY = (
    "This server only reads: it can show and search, and cannot check a "
    "design's numbers or build one. Tell the user that checking needs the "
    "server started with --tier propose, and building with --tier write."
)
_NO_WRITE = (
    "This server checks and does not write. Give the user the checked "
    "design and say that building it needs the server started with "
    "--tier write."
)

_STEPS: dict[str, tuple[str, ...]] = {
    "read": (_ASK, _EXISTING, _READ_ONLY, _SHOW),
    "propose": (
        _ASK,
        _EXISTING,
        _SCHEMA,
        _CALC,
        _PLAN,
        _DRAWN,
        _SIMULATE,
        _NO_WRITE,
        _REPORT,
    ),
    "write": (
        _ASK,
        _EXISTING,
        _SCHEMA,
        _CALC,
        _PLAN,
        _DRAWN,
        _SIMULATE,
        _WRITE,
        _BOARD,
        _SHOW,
        _REPORT,
    ),
}


def steps(tier: str) -> tuple[str, ...]:
    """The steps for a server at one tier, in order.

    Args:
        tier: ``read``, ``propose`` or ``write``.

    Returns:
        One instruction per step, naming only tools that tier offers.

    Raises:
        KeyError: If the tier is not one of the three.
    """
    return _STEPS[tier]


def go(tier: str, request: str = "") -> dict[str, Any]:
    """What the ``go`` tool answers.

    Args:
        tier: The tier the server runs at.
        request: What the user asked for, in their words. Returned
            unchanged so the steps and the request sit together.

    Returns:
        The request, the tier, and the numbered steps.
    """
    return {
        "request": request.strip(),
        "tier": tier,
        "steps": [f"{number}. {text}" for number, text in enumerate(steps(tier), 1)],
    }


def prompt(tier: str, request: str = "") -> str:
    """The same steps as one message, for a client's prompt menu.

    Args:
        tier: The tier the server runs at.
        request: What the user wants designed or checked.

    Returns:
        The message a client sends on the user's behalf.
    """
    asked = request.strip() or "(the user will say what they want next)"
    numbered = "\n".join(go(tier)["steps"])
    return (
        f"Design or check this with Kairo's tools: {asked}\n\n"
        f"Follow these steps in order.\n{numbered}"
    )
