"""What a model is told when a tool refuses, and what clears it.

A tool that cannot answer raises an error whose message starts with a
stable code (``E_PATH_NOT_ALLOWED: ...``). The server sends that to the
client as a refusal, a small object a model can act on without reading
prose:

- ``ok``: always false.
- ``code``: the stable code.
- ``error``: what happened, the message after the code.
- ``fix``: what clears it, written for the model: the tool or argument
  to use next, or that it must stop and ask the person.
- ``retryable``: whether the same call, unchanged, may succeed if it is
  made again. Only a fault outside the call (a file changed underneath
  it, the network) is; a wrong argument stays wrong.

The shape is the one HeyPCB's MCP proxy uses (MIT, read 2026-10-16),
so a model that has met one reads the other. The fixes are Kairo's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CODE = re.compile(r"(E_[A-Z0-9_]+):\s*")


@dataclass(frozen=True)
class Remedy:
    """What clears one kind of refusal.

    Attributes:
        fix: What to do next, for the model.
        retryable: Whether the same call may succeed if made again.
    """

    fix: str
    retryable: bool = False


STOP = (
    "Stop calling tools and tell the user what the error says; do not try "
    "again without their answer."
)
ASK_USER = "Tell the user what the error says; only they can change this."
READ_SCHEMA = (
    "Call design_schema for the exact shape, correct the argument it names, "
    "and send the call again."
)

REMEDIES: dict[str, Remedy] = {
    # Where Kairo may look and what it can run.
    "E_PATH_NOT_ALLOWED": Remedy(
        "Use a path under one of the allowed roots that list_projects shows. "
        "To add a root, ask the user to restart Kairo with --allowed-root."
    ),
    "E_PROJECT_NOT_FOUND": Remedy(
        "Call list_projects and pass one of the .kicad_pro files it lists."
    ),
    "E_NO_PROJECT_FILE": Remedy(ASK_USER),
    "E_KICAD_UNAVAILABLE": Remedy(
        "Tell the user that Kairo needs KiCad 9 or newer, or KAIRO_KICAD_CLI "
        "set to kicad-cli; no tool that runs KiCad will work until then."
    ),
    "E_KICAD_GUI_NOT_FOUND": Remedy(ASK_USER),
    "E_KICAD_GUI_FAILED": Remedy(ASK_USER),
    "E_SIMULATOR_UNAVAILABLE": Remedy(
        "Tell the user which simulator is missing; Kairo needs ngspice, or the "
        "server started with --ngspice PATH."
    ),
    "E_CATALOGUE_UNAVAILABLE": Remedy(
        "Choose parts without find_part, from the KiCad libraries with "
        "find_symbol, and tell the user how to build the catalogue as the "
        "error says."
    ),
    "E_RENDER_UNAVAILABLE": Remedy(
        "Read the schematic with read_schematic instead, and tell the user "
        "the drawing could not be made."
    ),
    "E_NETLIST_UNAVAILABLE": Remedy(
        "Run run_erc on the project to see what KiCad reports, and tell the user."
    ),
    "E_NETLIST_MALFORMED": Remedy(ASK_USER),
    # Arguments the call got wrong.
    "E_JSON_INVALID": Remedy(
        "Send the argument as a JSON object or list itself, not as a string, "
        "and fix the place the error points to."
    ),
    "E_DESIGN_INVALID": Remedy(READ_SCHEMA),
    "E_PATCH_INVALID": Remedy(
        "Call design_schema with part 'patch' for the shape of each operation, "
        "correct the one named, and send the call again."
    ),
    "E_SCHEMA_PART": Remedy(
        "Call design_schema with part design, patch, board or all."
    ),
    "E_NO_DESIGN": Remedy(
        "Pass the design to make_board, or build the project with apply_design "
        "in this run first."
    ),
    "E_NO_BOARD": Remedy(
        "Add board to the design (design_schema with part 'board' gives its "
        "shape) and call make_board again."
    ),
    "E_OPERATION": Remedy(
        "Correct the operation the error names: op is add_part, remove_part or "
        "reconnect, pins maps each pin name or number to a net, and "
        "no_connect lists pins."
    ),
    "E_PAGE_OUT_OF_RANGE": Remedy(
        "Ask again for a page within the range the error gives, starting at 1."
    ),
    "E_LIMIT_OUT_OF_RANGE": Remedy("Ask again with limit from 1 to 50."),
    "E_PAGES_INVALID": Remedy(
        "Give pages as a list such as [5, 6], one number, or text such as '5-7'."
    ),
    "E_CALC_UNKNOWN": Remedy(
        "Use one of the calculation names the error lists, exactly as written."
    ),
    "E_CALC_INPUT": Remedy(
        "Give each input the calculation takes, as text with its unit and "
        "tolerance such as '10k 1%', and no others."
    ),
    "E_PART_QUERY": Remedy("Correct the search argument the error names."),
    "E_PART_INVALID": Remedy(
        "Correct the figure the error names, giving each as an object with "
        "value and the datasheet page."
    ),
    "E_SIM_REQUEST_INVALID": Remedy(
        "Correct the simulation request as the error says; design_schema "
        "gives the design's shape."
    ),
    "E_VIOLATION_TYPE_UNKNOWN": Remedy(
        "Ask for one of the violation types the error lists, or leave the type "
        "out to see them all."
    ),
    # Names the libraries or the schematic do not have.
    "E_SYMBOL_UNKNOWN": Remedy(
        "Find the symbol with find_symbol and use the lib_id it returns."
    ),
    "E_FOOTPRINT_UNKNOWN": Remedy(
        "Find the footprint with find_footprint and use the lib_id it returns."
    ),
    "E_PIN_UNKNOWN": Remedy(
        "Read the part's pins with get_symbol_pins and use a name or number it lists."
    ),
    "E_PIN_AMBIGUOUS": Remedy(
        "Read the part's pins with get_symbol_pins and give the pin by number, "
        "or as NAME* where every unit's copy is meant."
    ),
    "E_PIN": Remedy(
        "Read the part's pins with trace_net or get_symbol_pins and use a pin "
        "number it lists."
    ),
    "E_PIN_ON_TWO_NETS": Remedy(
        "Put each pin on one net only; join the two nets if they are one."
    ),
    "E_PIN_PAD_MISMATCH": Remedy(
        "Choose a footprint whose pads match the symbol's pin numbers; "
        "find_footprint with the package name lists candidates."
    ),
    "E_UNIT_UNKNOWN": Remedy(
        "Read the part's units with get_symbol_pins and use a unit it has."
    ),
    "E_PIN_TWICE": Remedy(
        "Give each pin one net, or list it under no_connect, not both."
    ),
    "E_PIN_UNCONNECTED": Remedy(
        "Give every pin the error lists a net, or list it under no_connect."
    ),
    "E_NET_UNKNOWN": Remedy(
        "Use a net name read_schematic lists; the error gives the closest ones."
    ),
    "E_NET_UNNAMED": Remedy(
        "Use a named net, or ask the user to put a label on this one in KiCad."
    ),
    "E_NET_NOT_ON_SHEET": Remedy(
        "Write the local net's name in the form the error gives, or use a global net."
    ),
    "E_REF_NOT_FOUND": Remedy(
        "Use a reference read_schematic or project_summary lists."
    ),
    "E_REF_EXISTS": Remedy("Choose a reference the project does not use yet."),
    "E_SHEET_UNKNOWN": Remedy("Use one of the sheet files the error lists."),
    "E_SHEET_NEEDED": Remedy(
        "Name the sheet file with sheet, or a part to draw beside with beside."
    ),
    "E_PAPER_UNKNOWN": Remedy(ASK_USER),
    # What Kairo will not change.
    "E_PART_REVIEWED": Remedy(
        "Keep the reviewed figure, or state a different one in the design "
        "itself, where it applies to this design only."
    ),
    "E_SHARED_SHEET": Remedy(
        "Leave the part as it is and tell the user: the sheet is used more "
        "than once, so a change would reach every copy."
    ),
    "E_SYMBOL_DIFFERS": Remedy(ASK_USER),
    "E_SYMBOL_MISSING": Remedy(ASK_USER),
    "E_NOT_A_SCHEMATIC": Remedy(
        "Tell the user which file Kairo could not read as a schematic; "
        "opening it in KiCad shows what is wrong with it."
    ),
    "E_FORMAT_UNSUPPORTED": Remedy(
        "Tell the user that Kairo edits only sheets in KiCad 10's format; "
        "opening and saving the project in KiCad 10 converts it."
    ),
    "E_DRAWN_UNSUPPORTED": Remedy(ASK_USER),
    "E_RECONNECT_UNSUPPORTED": Remedy(
        "Leave that pin as it is and tell the user it has to be redrawn by "
        "hand in KiCad."
    ),
    "E_NO_ROOM": Remedy(
        "Name another sheet or a part elsewhere to place beside, or tell the "
        "user the sheet is full."
    ),
    "E_FACTS_INVALID": Remedy(
        "Name only nets and parts the schematic has (read_schematic lists "
        "them) and only the keys the error allows."
    ),
    "E_FACTS_FILE": Remedy(ASK_USER),
    # Writing and undoing.
    "E_NO_SESSION": Remedy(
        "Build the design with apply_design first; Kairo edits only designs "
        "it applied in this run."
    ),
    "E_FILE_CHANGED": Remedy(
        "The file changed while the call worked. Read it again (read_schematic "
        "or project_summary) and send the call again.",
        retryable=True,
    ),
    "E_WRITE_FAILED": Remedy(ASK_USER),
    "E_BAD_NAME": Remedy("Name a file inside the project folder."),
    "E_NOTHING_TO_UNDO": Remedy("Nothing to undo; carry on."),
    # Parts, datasheets and models.
    "E_DATASHEET_NO_LINK": Remedy(
        "Give the maker's link to the datasheet as url, or ask the user for it."
    ),
    "E_DATASHEET_LINK": Remedy(
        "Give an http or https link to the maker's own datasheet."
    ),
    "E_DATASHEET_FETCH": Remedy(
        "The download failed. Try once more; if it fails again, ask the user "
        "for another link.",
        retryable=True,
    ),
    "E_DATASHEET_NOT_PDF": Remedy(
        "The link gave a web page, not a PDF. Find the direct link to the PDF, "
        "or ask the user for it."
    ),
    "E_DATASHEET_TOO_LARGE": Remedy(ASK_USER),
    "E_DATASHEET_UNREADABLE": Remedy(ASK_USER),
    "E_DATASHEET_READER_MISSING": Remedy(
        "Tell the user that reading datasheets needs pdfplumber, installed "
        "with pip install kairo[datasheets]."
    ),
    "E_MODEL_MISSING": Remedy(ASK_USER),
    "E_MODEL_CHANGED": Remedy(ASK_USER),
    "E_MODEL_REGISTRY": Remedy(ASK_USER),
    # Limits that end a run.
    "E_NO_PROGRESS": Remedy(STOP),
    "E_ROUND_LIMIT": Remedy(STOP),
    "E_REPEATED_CALL": Remedy(STOP),
    "E_REPEATED_FAILURE": Remedy(
        "Stop calling this tool. Read what the error points to (design_schema, "
        "get_symbol_pins), or tell the user what the error says."
    ),
    "E_TOO_MANY_CALLS": Remedy(STOP),
}

UNLISTED = Remedy(
    "Read the error: it says what is wrong. Correct the call, or tell the "
    "user if it is not something a call can change."
)


def refusal(exc: BaseException) -> dict[str, Any]:
    """The refusal a client is sent for a tool's error.

    Args:
        exc: The error, whose message starts with its code.

    Returns:
        ``ok``, ``code``, ``error``, ``fix`` and ``retryable``. An error
        without a code is ``E_TOOL_FAILED`` with its whole message.
    """
    message = str(exc)
    found = CODE.match(message)
    code = found.group(1) if found else "E_TOOL_FAILED"
    error = message[found.end() :] if found else message
    remedy = REMEDIES.get(code, UNLISTED)
    return {
        "ok": False,
        "code": code,
        "error": error,
        "fix": remedy.fix,
        "retryable": remedy.retryable,
    }
