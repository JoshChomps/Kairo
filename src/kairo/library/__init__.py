"""KiCad's symbol and footprint libraries, read once and indexed.

Every pin and pad in a design is checked against this index, so a pin
that is not in the installed library is an error rather than a guess.
"""

from kairo.library.index import (
    FileProblem,
    FootprintSummary,
    LibraryIndex,
    SymbolSummary,
    UpdateReport,
)
from kairo.library.reader import (
    ELECTRICAL_TYPES,
    Footprint,
    LibraryFormatError,
    Pad,
    Pin,
    PinAlternate,
    Symbol,
    read_footprint,
    read_symbol_library,
)
from kairo.library.tables import (
    Library,
    TableProblem,
    install_variables,
    read_library_table,
)

__all__ = [
    "ELECTRICAL_TYPES",
    "FileProblem",
    "Footprint",
    "FootprintSummary",
    "Library",
    "LibraryFormatError",
    "LibraryIndex",
    "Pad",
    "Pin",
    "PinAlternate",
    "Symbol",
    "SymbolSummary",
    "TableProblem",
    "UpdateReport",
    "install_variables",
    "read_footprint",
    "read_library_table",
    "read_symbol_library",
]
