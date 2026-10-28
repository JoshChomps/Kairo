from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kairo.kicad.sexpr import (
    Atom,
    Document,
    SexprError,
    SList,
    decode_string,
    encode_string,
    parse,
    to_text,
)

# A small schematic-shaped text with CRLF endings and tab indents, the way
# KiCad 10.0.6 writes on Windows.
SAMPLE = (
    "(kicad_sch\r\n"
    "\t(version 20260306)\r\n"
    '\t(generator "eeschema")\r\n'
    '\t(property "Datasheet" "\\\\\\\\server\\\\share\\\\x.pdf")\r\n'
    '\t(text "line one\\nline \\"two\\"")\r\n'
    "\t(lib_symbols\r\n"
    '\t\t(symbol "Device:R" (pin_names (offset 0)) (in_bom yes))\r\n'
    "\t)\r\n"
    ")\r\n"
)


def test_round_trip_is_exact() -> None:
    assert parse(SAMPLE).to_text() == SAMPLE


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "(a)",
        "(a)\n",
        "(a) (b)",
        "(  a   b\t)\n\n",
        '(a\n\t(b c)\n\t(d "e f")\n)',
        '(a "")',
        "(a(b)c)",
        '(a"b")',
        "()",
        "(a\r\n)",
    ],
)
def test_round_trip_cases(text: str) -> None:
    assert parse(text).to_text() == text


def test_structure() -> None:
    root = parse(SAMPLE).root
    assert root.head == "kicad_sch"
    version = root.first("version")
    assert version is not None
    assert version.values() == ["20260306"]
    symbols = root.first("lib_symbols")
    assert symbols is not None
    [symbol] = symbols.lists("symbol")
    assert symbol.values() == ["Device:R"]
    assert symbol.first("in_bom") is not None
    assert root.first("missing") is None


def test_string_values_are_decoded() -> None:
    root = parse(SAMPLE).root
    prop = root.first("property")
    text = root.first("text")
    assert prop is not None and text is not None
    assert prop.values() == ["Datasheet", "\\\\server\\share\\x.pdf"]
    assert text.values() == ['line one\nline "two"']


def test_a_string_head_is_not_a_head() -> None:
    assert parse('("a" b)').root.head is None


def test_unknown_escape_is_refused_on_reading_but_round_trips() -> None:
    doc = parse('(a "tab\\there")')
    atom = doc.root.items[1]
    assert isinstance(atom, Atom)
    with pytest.raises(SexprError, match="unknown escape"):
        _ = atom.value
    assert doc.to_text() == '(a "tab\\there")'


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("(a", "unclosed"),
        ("(a))", "unmatched"),
        (")", "unmatched"),
        ('(a "b)', "unterminated"),
        ("(a\n  (b\n)\n)\n)", "line 5, column 1"),
    ],
)
def test_malformed_text_is_refused(text: str, message: str) -> None:
    with pytest.raises(SexprError, match=message):
        parse(text)


def test_root_needs_exactly_one_list() -> None:
    with pytest.raises(SexprError):
        _ = parse("(a) (b)").root
    with pytest.raises(SexprError):
        _ = parse("a").root


def test_editing_one_atom_changes_only_that_token() -> None:
    doc = parse(SAMPLE)
    version = doc.root.first("version")
    assert version is not None
    old = version.items[1]
    assert isinstance(old, Atom)
    version.items[1] = Atom.symbol("20250114", before=old.before)
    assert doc.to_text() == SAMPLE.replace("20260306", "20250114")


def test_adding_a_list_keeps_the_rest() -> None:
    doc = parse("(a\n\t(b)\n)\n")
    doc.root.items.append(SList([Atom.symbol("c", before="")], before="\n\t"))
    assert doc.to_text() == "(a\n\t(b)\n\t(c)\n)\n"


def test_atom_constructors() -> None:
    assert Atom.symbol("1.27").raw == "1.27"
    with pytest.raises(SexprError):
        Atom.symbol("two words")
    with pytest.raises(SexprError):
        Atom.symbol("")
    made = Atom.string('say "hi"\nthen \\')
    assert made.raw == '"say \\"hi\\"\\nthen \\\\"'
    assert made.value == 'say "hi"\nthen \\'


def test_to_text_prints_one_node_with_its_whitespace() -> None:
    root = parse("(a\n\t(b c)\n)").root
    assert to_text(root.items[1]) == "\n\t(b c)"


# Properties


_ALPHABET = st.sampled_from(list('()" \t\r\n\\abc019.-_:'))


@given(st.text(_ALPHABET, max_size=200))
def test_any_text_either_round_trips_or_is_refused(text: str) -> None:
    try:
        doc = parse(text)
    except SexprError:
        return
    assert doc.to_text() == text


_WS = st.lists(st.sampled_from([" ", "\t", "\r\n", "\n"]), max_size=3).map("".join)
_SPACED = st.lists(
    st.sampled_from([" ", "\t", "\r\n", "\n"]), min_size=1, max_size=3
).map("".join)
_BARE = st.text(st.sampled_from(list("abcxyz0129.-_:+*#~$")), min_size=1, max_size=8)


@st.composite
def _nodes(draw: st.DrawFn, depth: int = 0) -> list[Atom | SList]:
    """Items for one list, each separated from the last so they stay apart."""
    count = draw(st.integers(0, 4))
    items: list[Atom | SList] = []
    for index in range(count):
        before = draw(_WS) if index == 0 else draw(_SPACED)
        if depth < 3 and draw(st.booleans()):
            inner = draw(_nodes(depth + 1))
            items.append(SList(inner, before=before, end=draw(_WS)))
        elif draw(st.booleans()):
            items.append(Atom.symbol(draw(_BARE), before=before))
        else:
            items.append(Atom.string(draw(st.text(max_size=12)), before=before))
    return items


# No deadline: a large generated tree on a busy machine can pass the default
# 200 ms and fail for time alone; 20,000 examples with none found no fault.
@settings(deadline=None)
@given(_nodes(), _WS)
def test_printed_trees_parse_back_to_the_same_text(
    items: list[Atom | SList], end: str
) -> None:
    text = Document([SList(items)], end=end).to_text()
    assert parse(text).to_text() == text


@given(st.text(max_size=50))
def test_string_encoding_is_reversible(value: str) -> None:
    assert decode_string(encode_string(value)) == value
    assert Atom.string(value).value == value
    assert parse(f"(a {Atom.string(value).raw})").root.values() == [value]


def test_deep_nesting_prints_back() -> None:
    # Deeper than Python's recursion limit: the parser takes it, so the
    # printer must as well.
    text = "(" * 5000 + ")" * 5000
    assert parse(text).to_text() == text
