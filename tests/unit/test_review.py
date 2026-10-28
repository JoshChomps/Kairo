from __future__ import annotations

from kairo.kicad.drawn import read_drawn
from kairo.kicad.netlist import Component, Net, Netlist, Node
from kairo.verify.review import Review, review_design

ROLES = {
    "GND": {"role": "ground"},
    "+3V3": {"role": "power", "voltage": "3.3V"},
}
# A small sound design: a controller and a sensor on an I2C bus with
# pull-ups, each chip with its own capacitor, an interrupt line with its
# pull-up, an indicator with its resistor, and a protected USB port.
PARTS = {
    "U1": "MCU:Controller",
    "U2": "Sensor:Thing",
    "C1": "Device:C",
    "C2": "Device:C",
    "R1": "Device:R",
    "R2": "Device:R",
    "R3": "Device:R",
    "R4": "Device:R",
    "D1": "Device:LED",
    "J1": "Connector:USB_B_Micro",
    "U3": "Power_Protection:USBLC6-2SC6",
}
NETS: dict[str, list[tuple[str, str, str, str]]] = {
    "+3V3": [
        ("U1", "1", "VDD", "power_in"),
        ("U2", "1", "VDD", "power_in"),
        ("C1", "1", "", "passive"),
        ("C2", "1", "", "passive"),
        ("R1", "1", "", "passive"),
        ("R2", "1", "", "passive"),
        ("R3", "1", "", "passive"),
        ("R4", "1", "", "passive"),
    ],
    "GND": [
        ("U1", "2", "VSS", "power_in"),
        ("U2", "2", "GND", "power_in"),
        ("C1", "2", "", "passive"),
        ("C2", "2", "", "passive"),
        ("D1", "1", "K", "passive"),
        ("J1", "5", "GND", "passive"),
        ("U3", "2", "GND", "passive"),
    ],
    "SDA": [
        ("U1", "3", "SDA", "bidirectional"),
        ("U2", "3", "SDA", "bidirectional"),
        ("R1", "2", "", "passive"),
    ],
    "SCL": [
        ("U1", "4", "SCL", "bidirectional"),
        ("U2", "4", "SCL", "input"),
        ("R2", "2", "", "passive"),
    ],
    "INT": [
        ("U2", "5", "INT", "open_collector"),
        ("U1", "5", "PA0", "input"),
        ("R3", "2", "", "passive"),
    ],
    "LED_A": [("R4", "2", "", "passive"), ("D1", "2", "A", "passive")],
    "USB_DP": [
        ("J1", "3", "D+", "bidirectional"),
        ("U1", "6", "PA12", "bidirectional"),
        ("U3", "1", "IO1", "passive"),
    ],
}


def review(
    nets: dict[str, list[tuple[str, str, str, str]]] | None = None,
    parts: dict[str, str] | None = None,
    roles: dict[str, dict[str, str]] | None = ROLES,
    values: dict[str, str] | None = None,
    policy: dict[str, object] | None = None,
) -> Review:
    nets = NETS if nets is None else nets
    parts = PARTS if parts is None else parts
    used = {ref for pins in nets.values() for ref, *_ in pins}
    netlist = Netlist(
        "test",
        ("/",),
        tuple(
            Component(ref, (values or {}).get(ref, ""), "", lib_id, "", "/")
            for ref, lib_id in parts.items()
            if ref in used
        ),
        tuple(
            Net(name, tuple(Node(*pin) for pin in pins)) for name, pins in nets.items()
        ),
    )
    facts: dict[str, object] = {
        "nets": {k: v for k, v in (roles or {}).items() if k in nets}
    }
    if policy is not None:
        facts["policy"] = policy
    return review_design(read_drawn(netlist, "t", facts).grounded)


def without(net: str, ref: str) -> dict[str, list[tuple[str, str, str, str]]]:
    """The sound design with one part taken off one net, and off the rail."""
    return {
        name: [pin for pin in pins if not (pin[0] == ref and name in (net, "+3V3"))]
        for name, pins in NETS.items()
    }


def codes(found: Review) -> list[str]:
    return [remark.code for remark in found.remarks]


def test_a_sound_design_gets_no_remark() -> None:
    found = review()
    assert found.remarks == () and found.skipped == 0


def test_each_seeded_fault_is_caught_and_named() -> None:
    # The sensor's clock input left on a net of its own.
    floating = {
        **NETS,
        "SCL": NETS["SCL"][:1] + NETS["SCL"][2:],
        "X": [NETS["SCL"][1]],
    }
    found = review(floating)
    assert [r.code for r in found.faults] == ["R_INPUT_FLOATING"]
    assert found.faults[0].where == "U2.SCL" and "SCBA004" in found.faults[0].source
    # A rail with chips and no capacitor on it at all.
    no_caps = {
        name: [pin for pin in pins if pin[0] not in ("C1", "C2")]
        for name, pins in NETS.items()
    }
    assert codes(review(no_caps)) == ["R_RAIL_NO_DECOUPLING"]


def test_what_may_be_sound_is_suggested_and_never_faulted() -> None:
    one_cap = {
        name: [pin for pin in pins if pin[0] != "C2"] for name, pins in NETS.items()
    }
    bare_port = {
        name: [pin for pin in pins if pin[0] != "U3"] for name, pins in NETS.items()
    }
    # An indicator with its resistor taken out of the path.
    bare_led = without("LED_A", "R4")
    bare_led["+3V3"] = [*bare_led["+3V3"], ("D1", "2", "A", "passive")]
    del bare_led["LED_A"]
    for nets, code in (
        (one_cap, "S_DECOUPLING_SHARED"),
        (bare_port, "S_USB_DATA_UNPROTECTED"),
        (without("SDA", "R1"), "S_I2C_NO_PULLUP_DRAWN"),
        (without("INT", "R3"), "S_OPEN_OUTPUT_NO_PULL_DRAWN"),
        (bare_led, "S_LED_SYMBOL_ACROSS_RAIL"),
    ):
        found = review(nets)
        assert found.faults == (), code
        assert codes(found) == [code]
        assert "Kairo would" in found.remarks[0].message


def test_a_part_kairo_does_not_know_may_be_the_cure() -> None:
    # Seen on real boards: capacitors and resistors drawn with a board's
    # own symbols, and an open output that sinks an LED.
    own = {**PARTS, "C1": "MyLib:C_100n_0402", "C2": "MyLib:C_100n_0402"}
    assert codes(review(parts=own)) == []
    own_resistor = {**PARTS, "R3": "MyLib:R_10k_0402"}
    assert codes(review(parts=own_resistor)) == []
    sinking = without("INT", "R3")
    sinking["INT"] = [*sinking["INT"], ("D2", "1", "K", "passive")]
    sinking["N1"] = [("D2", "2", "A", "passive"), ("R5", "1", "", "passive")]
    sinking["+3V3"] = [*sinking["+3V3"], ("R5", "2", "", "passive")]
    parts = {**PARTS, "D2": "Device:LED", "R5": "Device:R"}
    assert codes(review(sinking, parts)) == []


def test_a_cure_that_may_be_off_the_board_is_not_called_missing() -> None:
    # The bus leaves through a connector, where its pull-ups may be.
    nets = without("SDA", "R1")
    nets["SDA"] = [*nets["SDA"], ("J2", "1", "Pin_1", "passive")]
    parts = {**PARTS, "J2": "Connector:Conn_01x02_Pin"}
    assert codes(review(nets, parts)) == []


def test_rules_that_need_supplies_wait_for_the_roles_to_be_stated() -> None:
    # With no roles nothing can be called a rail, so only the rules that
    # need none run, and the review says how many did not.
    found = review(without("SDA", "R1"), roles=None)
    assert found.remarks == () and found.skipped == 8


def _buck(v_ref: str | None) -> Review:
    # A step-down converter whose feedback pin is on its output rail with
    # no divider, as on a drawn board found 2026-10-15 (an LM5164's FB
    # tied straight to a 12 V output).
    nets: dict[str, list[tuple[str, str, str, str]]] = {
        "VIN": [("U1", "1", "VIN", "power_in"), ("C1", "1", "", "passive")],
        "GND": [
            ("U1", "2", "GND", "power_in"),
            ("C1", "2", "", "passive"),
            ("C2", "2", "", "passive"),
        ],
        "SW": [("U1", "3", "SW", "power_out"), ("L1", "1", "", "passive")],
        "+12V": [
            ("L1", "2", "", "passive"),
            ("U1", "4", "FB", "input"),
            ("C2", "1", "", "passive"),
        ],
    }
    parts = {"U1": "Reg:Buck", "C1": "Device:C", "C2": "Device:C", "L1": "Device:L"}
    netlist = Netlist(
        "test",
        ("/",),
        tuple(Component(ref, "", "", lib_id, "", "/") for ref, lib_id in parts.items()),
        tuple(
            Net(name, tuple(Node(*pin) for pin in pins)) for name, pins in nets.items()
        ),
    )
    facts: dict[str, object] = {
        "nets": {
            "VIN": {"role": "power", "voltage": "48V"},
            "+12V": {"role": "power", "voltage": "12V"},
            "GND": {"role": "ground"},
        }
    }
    if v_ref is not None:
        facts["components"] = {"U1": {"params": {"v_ref": v_ref}}}
    return review_design(read_drawn(netlist, "t", facts).grounded)


def test_a_feedback_pin_on_its_rail_with_no_divider_is_a_fault() -> None:
    found = _buck("1.2V")
    assert codes(found) == ["R_FEEDBACK_NO_DIVIDER"]
    assert "1.2 V reference, not the 12 V" in found.remarks[0].message


def test_a_feedback_pin_on_its_rail_with_no_reference_stated_is_a_suggestion() -> None:
    assert codes(_buck(None)) == ["S_FEEDBACK_ON_RAIL"]


def test_a_feedback_pin_on_a_rail_at_its_reference_is_sound() -> None:
    assert codes(_buck("12V")) == []


def _netlist_review(
    nets: dict[str, list[tuple[str, str, str, str]]],
    parts: dict[str, tuple[str, str, str]],
    roles: dict[str, dict[str, str]],
) -> Review:
    """Review a netlist whose parts are given as lib_id, value and description."""
    netlist = Netlist(
        "test",
        ("/",),
        tuple(
            Component(ref, value, "", lib_id, description, "/")
            for ref, (lib_id, value, description) in parts.items()
        ),
        tuple(
            Net(name, tuple(Node(*pin) for pin in pins)) for name, pins in nets.items()
        ),
    )
    return review_design(read_drawn(netlist, "t", {"nets": roles}).grounded)


def _switch(pre_source: str, pre_resistor: str) -> Review:
    # A battery's high-side switch: charge and discharge FETs back to back
    # with a common drain, and a predischarge P-FET through a resistor to
    # the pack terminal, as a BMS designed through Kairo drew it
    # (2026-10-16, revision C.2 had the P-FET's source on the battery).
    # A drawn schematic keeps no library symbols, so the channel is read
    # from the symbol's name here.
    nmos = ("Transistor_FET:Q_NMOS_GDS", "IPT015N10N5", "")
    nets: dict[str, list[tuple[str, str, str, str]]] = {
        "BAT": [("Q1", "2", "S", "passive"), ("J1", "1", "Pin_1", "passive")],
        "PACK": [
            ("Q2", "2", "S", "passive"),
            ("R1", "2", "", "passive"),
            ("J2", "1", "Pin_1", "passive"),
        ],
        "MID": [("Q1", "3", "D", "passive"), ("Q2", "3", "D", "passive")],
        "PRE": [("Q3", "2", "D", "passive"), ("R1", "1", "", "passive")],
        "CHG_G": [("Q1", "1", "G", "passive")],
        "DSG_G": [("Q2", "1", "G", "passive")],
        "PRE_G": [("Q3", "1", "G", "passive")],
        "GND": [("J3", "1", "Pin_1", "passive")],
        "Q4_G": [("Q4", "1", "G", "passive")],
        "Q4_S": [("Q4", "2", "S", "passive")],
        "Q4_D": [("Q4", "3", "D", "passive")],
    }
    nets[pre_source] = [*nets[pre_source], ("Q3", "3", "S", "passive")]
    parts = {
        "Q1": nmos,
        "Q2": nmos,
        "Q3": ("Transistor_FET:Q_PMOS_GDS", "IRFR9120", ""),
        # A FET whose channel nothing tells is left out, not guessed.
        "Q4": ("Transistor_FET:IPT015N10N5", "IPT015N10N5", ""),
        "R1": ("Device:R", pre_resistor, ""),
        "J1": ("Connector_Generic:Conn_01x01", "B+", ""),
        "J2": ("Connector_Generic:Conn_01x01", "P+", ""),
        "J3": ("Connector_Generic:Conn_01x01", "B-", ""),
    }
    roles = {
        "BAT": {"role": "power", "voltage": "48V"},
        "PACK": {"role": "power", "voltage": "48V"},
        "GND": {"role": "ground"},
    }
    return _netlist_review(nets, parts, roles)


def test_a_body_diode_around_a_back_to_back_switch_is_a_fault() -> None:
    found = _switch("BAT", "47")
    assert codes(found) == ["R_SWITCH_BYPASSED"]
    remark = found.faults[0]
    # The charge FET is the one the path defeats: current from the pack
    # terminal reaches the battery while it is off.
    assert remark.where == "Q1"
    assert "from PACK to BAT" in remark.message
    assert "R1 -> PRE -> Q3 (its body diode) -> BAT" in remark.message
    assert "body diode" in remark.source


def test_a_predischarge_fet_across_the_discharge_fets_alone_is_sound() -> None:
    # Revision C.3 moved the P-FET's source to the common drain, inside
    # the charge FETs, as TI's Figure 16-1 has it.
    assert codes(_switch("MID", "47")) == []


def test_a_path_through_a_high_resistance_is_a_leak_not_a_fault() -> None:
    found = _switch("BAT", "1M")
    assert codes(found) == ["S_SWITCH_LEAKS"]
    assert "1e+06 ohm" in found.suggestions[0].message


def _shunt(grounds: int) -> Review:
    # A low-side shunt between the battery's negative and the load's,
    # with a load connector and some number of ground connectors.
    nets: dict[str, list[tuple[str, str, str, str]]] = {
        "GND": [("R1", "1", "", "passive")]
        + [(f"J{n}", "1", "Pin_1", "passive") for n in range(1, grounds + 1)],
        "LOAD_N": [("R1", "2", "", "passive"), ("J9", "1", "Pin_1", "passive")],
        "+48V": [("J9", "2", "Pin_2", "passive")],
    }
    parts = {
        "R1": ("Device:R", "0.3m", ""),
        "J9": ("Connector_Generic:Conn_01x02", "LOAD", ""),
        **{
            f"J{n}": ("Connector_Generic:Conn_01x01", "", "")
            for n in range(1, grounds + 1)
        },
    }
    roles = {"GND": {"role": "ground"}, "+48V": {"role": "power", "voltage": "48V"}}
    return _netlist_review(nets, parts, roles)


def test_a_sense_resistor_with_grounds_leaving_on_both_sides_is_questioned() -> None:
    # Seen on the same BMS (revision C.2): the CAN connector's ground on
    # the battery side of the shunt, the vehicle's ground on the other.
    found = _shunt(2)
    assert codes(found) == ["S_SENSE_RESISTOR_SHARED"]
    assert "GND through J1, J2" in found.suggestions[0].message
    assert "LOAD_N through J9" in found.suggestions[0].message


def test_one_connector_on_each_side_of_a_sense_resistor_is_its_own_path() -> None:
    assert codes(_shunt(1)) == []


def test_a_supply_sensed_on_its_way_out_is_not_asked_about() -> None:
    # A real booster board (git4dcc rtb-c13) senses its supply through
    # 0.1 ohm before an output, and the supply goes on to other boards
    # through several connectors on purpose.
    nets: dict[str, list[tuple[str, str, str, str]]] = {
        "VBST": [("R1", "1", "", "passive")]
        + [(f"J{n}", "1", "Pin_1", "passive") for n in (1, 2, 3)],
        "OUT": [("R1", "2", "", "passive"), ("J9", "1", "Pin_1", "passive")],
        "GND": [(f"J{n}", "2", "Pin_2", "passive") for n in (1, 2, 3, 9)],
    }
    parts = {
        "R1": ("Device:R", "0.1", ""),
        **{f"J{n}": ("Connector_Generic:Conn_01x02", "", "") for n in (1, 2, 3, 9)},
    }
    roles = {"GND": {"role": "ground"}, "VBST": {"role": "power", "voltage": "15V"}}
    assert codes(_netlist_review(nets, parts, roles)) == []


# A supply from off the board, and an input nothing drives


Nets = dict[str, list[tuple[str, str, str, str]]]


def _fed_from_a_connector(*values: str) -> tuple[Nets, dict[str, str], dict[str, str]]:
    """The sound design with +3V3 brought in on J2, and its capacitors' values."""
    nets = {name: list(pins) for name, pins in NETS.items()}
    nets["+3V3"] = [*nets["+3V3"], ("J2", "1", "Pin_1", "passive")]
    nets["GND"] = [*nets["GND"], ("J2", "2", "Pin_2", "passive")]
    parts = {**PARTS, "J2": "Connector_Generic:Conn_01x02"}
    return nets, parts, dict(zip(("C1", "C2"), values, strict=True))


def test_a_supply_from_a_connector_with_only_small_capacitors_is_questioned() -> None:
    nets, parts, values = _fed_from_a_connector("100n", "1u")
    found = review(nets, parts, values=values)
    assert codes(found) == ["S_SUPPLY_IN_NO_BULK"]
    (remark,) = found.remarks
    assert remark.where == "+3V3" and "J2" in remark.message
    assert "largest capacitor is 1 uF" in remark.message
    assert "10 uF or more" in remark.message and "MT-101" in remark.source


def test_a_supply_with_bulk_or_made_on_the_board_or_unknown_is_left_alone() -> None:
    nets, parts, values = _fed_from_a_connector("100n", "22u")
    assert codes(review(nets, parts, values=values)) == []
    # A value Kairo cannot read may be bulk.
    nets, parts, values = _fed_from_a_connector("100n", "big")
    assert codes(review(nets, parts, values=values)) == []
    # A rail a regulator makes, which a connector only passes on.
    nets, parts, values = _fed_from_a_connector("100n", "100n")
    nets["+3V3"] = [*nets["+3V3"], ("U4", "2", "VOUT", "power_out")]
    nets["GND"] = [*nets["GND"], ("U4", "1", "GND", "power_in")]
    assert codes(review(nets, {**parts, "U4": "Regulator:LDO"}, values=values)) == []
    # A test point is a place to probe, not a way in, in either library.
    for lib_id in ("TestPoint:TestPoint", "Connector:TestPoint"):
        nets, parts, values = _fed_from_a_connector("100n", "100n")
        parts["J2"] = lib_id
        assert codes(review(nets, parts, values=values)) == [], lib_id


def test_an_input_with_only_a_capacitor_beside_it_is_questioned() -> None:
    nets = {name: list(pins) for name, pins in NETS.items()}
    nets["NRST"] = [("U1", "7", "NRST", "input"), ("C3", "1", "", "passive")]
    nets["GND"] = [*nets["GND"], ("C3", "2", "", "passive")]
    parts = {**PARTS, "C3": "Device:C"}
    found = review(nets, parts)
    assert codes(found) == ["S_CONTROL_INPUT_UNSET"]
    (remark,) = found.remarks
    assert remark.where == "U1.NRST" and "only C3 beside it" in remark.message
    assert "datasheet" in remark.message
    # Two inputs joined with nothing else are not driven either, and the
    # rule needs no roles.
    tied = {**NETS, "EN": [("U1", "8", "~{EN}", "input"), ("U2", "8", "EN", "input")]}
    assert "S_CONTROL_INPUT_UNSET" in codes(review(tied, roles=None))
    # Seen on real boards and sound: a soft-start pin with its capacitor,
    # an input coupled through a capacitor, a crystal input.
    for name in ("SS", "RFIN", "XA", "REF_IN"):
        other = {**NETS, "X": [("U1", "7", name, "input"), ("C3", "1", "", "passive")]}
        other["GND"] = [*other["GND"], ("C3", "2", "", "passive")]
        assert codes(review(other, {**PARTS, "C3": "Device:C"})) == [], name


def test_reset_and_enable_names_are_read_through_their_active_low_marks() -> None:
    from kairo.verify.review import (
        _control,  # pyright: ignore[reportPrivateUsage]
    )

    named = ("RESET", "~{RESET}", "nRST", "NRST", "RST_N", "/MCLR", "EN", "CE#", "SHDN")
    for name in named:
        assert _control(name), name
    for name in ("SS", "SDA", "ENC", "PDN_X", "RXEN1", "XA"):
        assert not _control(name), name


def test_an_input_with_anything_that_may_set_it_is_left_alone() -> None:
    for other, lib_id in (
        (("R9", "1", "", "passive"), "Device:R"),
        (("U2", "9", "OUT", "output"), "Sensor:Thing"),
        (("J9", "1", "Pin_1", "passive"), "Connector:Conn_01x01"),
        (("X1", "1", "", "passive"), "MyLib:Mystery"),
        (("U2", "9", "IO", "unspecified"), "Sensor:Thing"),
    ):
        nets = {name: list(pins) for name, pins in NETS.items()}
        nets["NRST"] = [("U1", "7", "NRST", "input"), ("C3", "1", "", "passive"), other]
        nets["GND"] = [*nets["GND"], ("C3", "2", "", "passive")]
        parts = {**PARTS, "C3": "Device:C", other[0]: lib_id}
        assert codes(review(nets, parts)) == [], other


def uart(
    a: tuple[str, str], b: tuple[str, str], parts: dict[str, str] | None = None
) -> Review:
    """U1 and U2 on one serial line, each pin as a name and a type."""
    return review(
        {"LINE": [("U1", "7", *a), ("U2", "7", *b)]},
        parts or {"U1": "MCU:Controller", "U2": "Interface:Bridge"},
        roles=None,
    )


def test_two_uart_transmit_outputs_joined_are_a_fault() -> None:
    found = uart(("U0TXD", "output"), ("TXD", "output"))
    assert codes(found) == ["R_UART_TX_TO_TX"]
    assert "U1.U0TXD" in found.remarks[0].message


def test_two_uart_receive_inputs_with_nothing_sending_are_a_fault() -> None:
    assert codes(uart(("RXD0", "input"), ("UART_RX", "input"))) == ["R_UART_RX_TO_RX"]


def test_a_crossed_uart_and_a_can_transceivers_txd_are_sound() -> None:
    # Transmit to receive, as it should be.
    assert codes(uart(("TX", "output"), ("RXD", "input"))) == []
    # A CAN controller's TX drives its transceiver's TXD, an input.
    assert codes(uart(("CAN_TX", "output"), ("TXD", "input"))) == []
    # Names that only contain TX are not UART lines.
    assert codes(uart(("TXEN", "output"), ("CTX", "output"))) == []


def test_a_connectors_pins_may_be_named_from_either_side() -> None:
    parts = {"U1": "MCU:Controller", "U2": "Connector:Conn_01x02"}
    assert codes(uart(("TX", "output"), ("TX", "output"), parts)) == []


def test_a_prototype_is_asked_for_test_points_on_its_rails() -> None:
    found = review(policy={"prototype": True})
    assert codes(found) == ["S_RAIL_NO_TEST_POINT"]
    assert found.remarks[0].where == "+3V3, GND"
    probed = {
        **NETS,
        "+3V3": [*NETS["+3V3"], ("TP1", "1", "1", "passive")],
        "GND": [*NETS["GND"], ("TP2", "1", "1", "passive")],
    }
    parts = {**PARTS, "TP1": "Connector:TestPoint", "TP2": "Connector:TestPoint"}
    assert codes(review(probed, parts, policy={"prototype": True})) == []
    # Without the policy, as on a board for production, nothing is asked.
    assert codes(review()) == []


# A regulator U4 makes +3V3 from +5V and feeds U1 and U2 straight from its
# output: the sound design, with its supply made on the board.
REGULATED = {
    **NETS,
    "+3V3": [*NETS["+3V3"], ("U4", "2", "VOUT", "power_out")],
    "+5V": [("U4", "1", "VIN", "power_in"), ("J1", "1", "VBUS", "passive")],
    "GND": [*NETS["GND"], ("U4", "3", "GND", "power_in")],
}
REGULATED_PARTS = {**PARTS, "U4": "Regulator_Linear:AP2112K-3.3"}
REGULATED_ROLES = {**ROLES, "+5V": {"role": "power", "voltage": "5V"}}


def prototype_review(nets: dict[str, list[tuple[str, str, str, str]]]) -> Review:
    return review(nets, REGULATED_PARTS, REGULATED_ROLES, policy={"prototype": True})


def test_a_prototypes_regulated_rail_is_asked_for_a_light_and_a_link() -> None:
    found = prototype_review(REGULATED)
    by_code = {r.code: r for r in found.remarks}
    # D1 lights +3V3 through R4, so only the link is asked for there.
    assert "S_RAIL_NO_INDICATOR" not in by_code
    assert by_code["S_RAIL_NOT_ISOLATED"].where == "+3V3"
    # Without D1's resistor, +3V3 lights nothing.
    dark = {
        name: [p for p in pins if p[0] not in ("R4", "D1")]
        for name, pins in REGULATED.items()
    }
    assert {r.code: r for r in prototype_review(dark).remarks}[
        "S_RAIL_NO_INDICATOR"
    ].where == "+3V3"


def test_a_link_between_the_regulator_and_its_loads_is_isolation() -> None:
    # U4 now drives +3V3_REG, joined to +3V3 by a 0 ohm link R9.
    linked = {
        **REGULATED,
        "+3V3": [
            *[p for p in REGULATED["+3V3"] if p[0] != "U4"],
            ("R9", "2", "", "passive"),
        ],
        "+3V3_REG": [("U4", "2", "VOUT", "power_out"), ("R9", "1", "", "passive")],
    }
    parts = {**REGULATED_PARTS, "R9": "Device:R"}
    roles = {**REGULATED_ROLES, "+3V3_REG": {"role": "power", "voltage": "3.3V"}}
    found = review(linked, parts, roles, policy={"prototype": True})
    assert "S_RAIL_NOT_ISOLATED" not in [r.code for r in found.remarks]


def test_a_board_for_production_is_not_asked_for_lights_or_links() -> None:
    found = review(REGULATED, REGULATED_PARTS, REGULATED_ROLES)
    assert not {"S_RAIL_NO_INDICATOR", "S_RAIL_NOT_ISOLATED"} & {
        r.code for r in found.remarks
    }


def test_a_prototypes_spare_io_left_open_is_asked_to_be_brought_out() -> None:
    nets = {
        **NETS,
        "unconnected-(U1-PA3-Pad7)": [("U1", "7", "PA3", "bidirectional+no_connect")],
        "unconnected-(U2-NC-Pad8)": [("U2", "8", "NC", "passive+no_connect")],
    }
    found = review(nets, policy={"prototype": True})
    spare = [r for r in found.remarks if r.code == "S_SPARE_IO_UNREACHED"]
    # A chip's open I/O pin is spare; a pin with no function (NC) is not.
    assert [r.where for r in spare] == ["U1.PA3"]
    assert "S_SPARE_IO_UNREACHED" not in [r.code for r in review(nets).remarks]
