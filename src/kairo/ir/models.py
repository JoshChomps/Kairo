"""The circuit description a model writes: intent, never geometry.

Parts are named by KiCad library ID, connections by pin name, nets by
role, and requirements as assertions Kairo can check. Pin numbers,
coordinates, identifiers and file syntax are Kairo's business and have no
place here. Everything is validated strictly: an unknown field, parameter
or requirement type is an error, because a silently ignored line would be
a design the user thinks was checked and was not.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_serializer,
    model_validator,
)

from kairo.units import Quantity, QuantityError, format_quantity, parse_quantity

#: What a parameter name means and the unit its value must have. ``None``
#: marks a parameter that is text, not a quantity.
PARAMETERS: dict[str, str | None] = {
    "tolerance": "%",
    "tempco": None,
    "drift": None,
    "dielectric": None,
    "mpn": None,
    "v_rating": "V",
    "p_rating": "W",
    "i_rating": "A",
    "v_f": "V",
    "v_f_tolerance": "%",
    "v_clamp": "V",
    "i_f": "A",
    "v_out": "V",
    "v_out_tolerance": "%",
    "v_ref": "V",
    "v_dropout": "V",
    "i_out_max": "A",
    "i_q": "A",
    "theta_ja": "degC/W",
    "t_j_max": "degC",
    "esr": "ohm",
    "esr_min": "ohm",
    "esr_max": "ohm",
    "c_out_min": "F",
    "c_out_esr_min": "ohm",
    "c_out_esr_max": "ohm",
    "v_ref_tolerance": "%",
    "i_adj": "A",
    "efficiency": "%",
    "i_supply": "A",
    "i_sat": "A",
    "i_limit": "A",
    "f_sw": "Hz",
    "k_charge": "V",
    "k_charge_tolerance": "%",
    "k_input": "V",
    "k_input_tolerance": "%",
    "regulator": None,
}
#: Where a value came from when the design does not say.
USER_STATED = "user-stated"

_PIN = re.compile(r"(?P<ref>[A-Za-z_#][A-Za-z0-9_]*)\.(?P<pin>.+)")


def quantity_field(unit: str | None) -> Any:
    """A pydantic field type holding a `Quantity` in one unit."""

    def read(value: object) -> Quantity:
        if isinstance(value, Quantity):
            return value
        if not isinstance(value, str):
            raise ValueError(
                f"write a quantity as text with its unit, for example "
                f"'{_EXAMPLES.get(unit or '', '10k')}'; got {value!r}"
            )
        try:
            return parse_quantity(value, unit)
        except QuantityError as exc:
            raise ValueError(str(exc)) from exc

    return Annotated[
        Quantity,
        BeforeValidator(read),
        PlainSerializer(format_quantity, return_type=str),
    ]


_EXAMPLES = {"V": "3.3V", "A": "300mA", "W": "0.125W", "%": "5%", "degC": "40degC"}

if TYPE_CHECKING:
    # To a type checker each of these is simply a quantity; at run time
    # each also reads text and checks the unit.
    Volts = Quantity
    Amps = Quantity
    Farads = Quantity
    Percent = Quantity
    Celsius = Quantity
    Hertz = Quantity
    Seconds = Quantity
    Decibels = Quantity
    Millimetres = Quantity
else:
    Hertz = quantity_field("Hz")
    Seconds = quantity_field("s")
    Decibels = quantity_field("dB")
    Volts = quantity_field("V")
    Amps = quantity_field("A")
    Farads = quantity_field("F")
    Percent = quantity_field("%")
    Celsius = quantity_field("degC")
    Millimetres = quantity_field("mm")


class _Strict(BaseModel):
    """A model that refuses fields it does not know."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class Net(_Strict):
    """A net and what it is for.

    Attributes:
        role: ``power``, ``ground`` or ``signal``.
        voltage: The nominal voltage; a power net needs it or min and max.
        tolerance: How far a supply from outside the design may be from
            nominal, for example ``5%``.
        min: The lowest it is in use (a battery's 32.5V), with max and
            instead of tolerance.
        max: The highest it is in use.
        surge_max: The highest a transient takes it; read by pin ratings.
        surge_min: The lowest a transient takes it.
        current: The most a track carries.
    """

    role: Literal["power", "ground", "signal"]
    voltage: Volts | None = None
    tolerance: Percent | None = None
    min: Volts | None = None
    max: Volts | None = None
    surge_max: Volts | None = None
    surge_min: Volts | None = None
    current: Amps | None = None

    @model_validator(mode="after")
    def _power_has_a_voltage(self) -> Net:
        if (self.min is None) != (self.max is None):
            raise ValueError("give a net's min and max together")
        if self.min is not None and self.max is not None:
            if self.min.value > self.max.value:
                raise ValueError("a net's min is above its max")
            if self.tolerance is not None:
                raise ValueError("give a net's tolerance or its min and max, not both")
            if self.voltage is None:
                self.voltage = Quantity((self.min.value + self.max.value) / 2, "V")
            elif not self.min.value <= self.voltage.value <= self.max.value:
                raise ValueError("a net's voltage must lie between its min and max")
        if self.role == "power" and self.voltage is None:
            raise ValueError(
                "a power net needs its nominal voltage, e.g. voltage: 5V, or its "
                "min and max"
            )
        if self.role == "ground" and any(
            v is not None for v in (self.min, self.surge_max, self.surge_min)
        ):
            raise ValueError("a ground net is 0 V; it takes no range")
        top = self.max if self.max is not None else self.voltage
        if (
            self.surge_max is not None
            and top is not None
            and self.surge_max.value < top.value
        ):
            raise ValueError("a net's surge_max is below its highest steady value")
        bottom = self.min if self.min is not None else self.voltage
        if (
            self.surge_min is not None
            and bottom is not None
            and self.surge_min.value > bottom.value
        ):
            raise ValueError("a net's surge_min is above its lowest steady value")
        return self


class PinRating(_Strict):
    """A pin's absolute maximum rating, from its datasheet.

    Attributes:
        max: The highest the pin may be, from the ``to`` pin.
        min: The lowest, such as ``-0.3V``.
        to: The part's pin it is measured from (``VSS``, a FET's ``S``);
            ground if not given.
        source: Where the figure is from, if not the part's source.
    """

    max: Volts | None = None
    min: Volts | None = None
    to: str | None = None
    source: str | None = None

    @model_validator(mode="after")
    def _bounded(self) -> PinRating:
        if self.max is None and self.min is None:
            raise ValueError("a pin rating needs its max, its min, or both")
        if (
            self.max is not None
            and self.min is not None
            and self.min.value > self.max.value
        ):
            raise ValueError("a pin rating's min is above its max")
        return self


class Component(_Strict):
    """One part.

    Attributes:
        lib_id: The KiCad symbol, as ``Library:Symbol``.
        footprint: The KiCad footprint, as ``Library:Footprint``.
        value: The value to show, for example ``10uF``; the symbol's own
            value is used when this is absent.
        unit: Which unit of a multi-unit symbol this is.
        params: Electrical facts calculations need, each a quantity with
            its unit or, for a few, text. See `PARAMETERS`.
        source: Where the params came from: a datasheet reference, or
            ``user-stated``.
        limits: The params whose stated value is the datasheet's limit
            rather than a nominal value, each with which limit it is:
            ``{"i_q": "max", "i_out_max": "min"}``. A check that needs
            that side of the parameter takes it with no further
            tolerance: the part is never past it. A plain list of names
            is still read, but says only that each is some limit, and a
            check cannot rely on a limit whose side it does not know.
        pin_ratings: Absolute maximum ratings by pin name, each checked
            against the range its net can reach (see `PinRating`).
    """

    # The library may be empty: a drawn board can hold a symbol whose
    # library link is gone, and KiCad's netlist then names none.
    lib_id: str = Field(pattern=r"^[^:\s]*:[^:\s]+$")
    footprint: str | None = Field(default=None, pattern=r"^[^:\s]+:[^:\s]+$")
    value: str | None = None
    unit: int = Field(default=1, ge=1)
    params: dict[str, Quantity | str] = Field(default_factory=dict[str, Quantity | str])
    source: str = USER_STATED
    limits: dict[str, Literal["max", "min"]] | list[str] = Field(
        default_factory=dict[str, Literal["max", "min"]]
    )
    pin_ratings: dict[str, PinRating] = Field(default_factory=dict[str, PinRating])

    def limit_side(self, name: str) -> Literal["max", "min", "unstated"] | None:
        """Which datasheet limit a parameter's stated value is.

        Args:
            name: The parameter name.

        Returns:
            ``max`` or ``min``; ``unstated`` if the design marks it as a
            limit without saying which; ``None`` if it is not a limit.
        """
        if name not in self.limits:
            return None
        if isinstance(self.limits, dict):
            return self.limits[name]
        return "unstated"

    @model_validator(mode="after")
    def _limits_are_params(self) -> Component:
        absent = [name for name in self.limits if name not in self.params]
        if absent:
            raise ValueError(
                f"limits names {', '.join(absent)}, which params does not give"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _read_params(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(cast("dict[str, object]", data))
        given = data.get("params")
        if not isinstance(given, dict):
            return data
        params = dict(cast("dict[str, object]", given))
        # The plan's examples write the source among the params.
        if "source" in params:
            if "source" in data:
                raise ValueError("give source once, not in both the part and params")
            data["source"] = params.pop("source")
        read: dict[str, Quantity | str] = {}
        for name, value in params.items():
            if name not in PARAMETERS:
                known = ", ".join(sorted(PARAMETERS))
                raise ValueError(f"unknown parameter {name!r}; known: {known}")
            unit = PARAMETERS[name]
            if not isinstance(value, str):
                raise ValueError(f"write {name} as text with its unit; got {value!r}")
            if unit is None:
                read[name] = value
                continue
            try:
                read[name] = parse_quantity(value, unit)
            except QuantityError as exc:
                raise ValueError(f"{name}: {exc}") from exc
        data["params"] = read
        return data

    @field_serializer("params")
    def _write_params(self, params: dict[str, Quantity | str]) -> dict[str, str]:
        return {
            name: format_quantity(value) if isinstance(value, Quantity) else value
            for name, value in params.items()
        }

    def quantity(self, name: str) -> Quantity | None:
        """A parameter that is a quantity, or ``None`` if it was not given.

        Args:
            name: The parameter name.

        Returns:
            The quantity, in its base unit.
        """
        value = self.params.get(name)
        return value if isinstance(value, Quantity) else None


class Load(_Strict):
    """Current drawn from a net by something outside the design.

    Attributes:
        net: The net it draws from.
        current: How much.
        note: What it stands for.
    """

    net: str
    current: Amps
    note: str = ""


class MinCapacitance(_Strict):
    """At least this much capacitance between two nets.

    Attributes:
        type: ``min_capacitance``.
        between: The two nets.
        value: The capacitance needed.
        basis: ``effective`` counts what the parts deliver under DC bias
            and tolerance; ``nominal`` counts marked values and is reported
            as such.
    """

    type: Literal["min_capacitance"]
    between: tuple[str, str]
    value: Farads
    basis: Literal["effective", "nominal"] = "effective"


class RailVoltage(_Strict):
    """A net's voltage stays within a tolerance of nominal.

    Attributes:
        type: ``rail_voltage``.
        net: The net.
        nominal: The voltage wanted.
        tolerance: How far it may be from nominal.
    """

    type: Literal["rail_voltage"]
    net: str
    nominal: Volts
    tolerance: Percent


class MaxJunctionTemp(_Strict):
    """A part's junction stays at or below a limit.

    Attributes:
        type: ``max_junction_temp``.
        component: The part.
        ambient: The ambient temperature to assume.
        limit: The highest junction temperature allowed.
    """

    type: Literal["max_junction_temp"]
    component: str
    ambient: Celsius
    limit: Celsius


class MaxComponentPower(_Strict):
    """A part dissipates no more than a share of its rating.

    Attributes:
        type: ``max_component_power``.
        component: The part.
        derating: The share of the power rating that may be used.
    """

    type: Literal["max_component_power"]
    component: str
    derating: Percent


class MinAttenuation(_Strict):
    """A first-order RC low-pass loses at least this much at a frequency.

    Attributes:
        type: ``min_attenuation``.
        input: The net the signal comes in on.
        output: The net it leaves on, after the resistor.
        frequency: The frequency the loss is wanted at.
        value: The least loss, in decibels.
    """

    type: Literal["min_attenuation"]
    input: str
    output: str
    frequency: Hertz
    value: Decibels


class MaxInductorCurrent(_Strict):
    """A switching regulator's inductor never carries more than it is rated for.

    The greatest peak current, over the tolerances, is compared with the
    inductor's saturation current (``i_sat``).

    Attributes:
        type: ``max_inductor_current``.
        component: The inductor.
    """

    type: Literal["max_inductor_current"]
    component: str


class MaxRipple(_Strict):
    """A switching regulator's output ripples by no more than this.

    Attributes:
        type: ``max_ripple``.
        net: The rail the regulator makes.
        value: The greatest ripple allowed, peak to peak.
    """

    type: Literal["max_ripple"]
    net: str
    value: Volts


class MinHoldupTime(_Strict):
    """A rail's capacitors hold it above a floor this long with no supply.

    Attributes:
        type: ``min_holdup_time``.
        net: The rail, whose capacitors to ground are the reservoir.
        floor: The voltage the rail must not fall below in that time.
        time: How long it must hold.
    """

    type: Literal["min_holdup_time"]
    net: str
    floor: Volts
    time: Seconds


class MaxRailCurrent(_Strict):
    """A rail draws at most this over the tolerances, regulators included.

    Attributes:
        type: ``max_rail_current``.
        net: The rail, such as a USB supply.
        value: The most it may draw.
    """

    type: Literal["max_rail_current"]
    net: str
    value: Amps


class ProgrammedCurrent(_Strict):
    """A current a chip sets as I = K / R, R to ground on a pin, stays in range.

    K is the datasheet's factor (A*ohm, written in volts), given to the
    chip as ``k_charge`` or ``k_input`` with its tolerance.

    Attributes:
        type: ``programmed_current``.
        component: The chip.
        pin: The pin, by name.
        factor: The chip's parameter holding K.
        min: The least it may be.
        max: The most it may be.
    """

    type: Literal["programmed_current"]
    component: str
    pin: str
    factor: Literal["k_charge", "k_input"]
    min: Amps | None = None
    max: Amps | None = None

    @model_validator(mode="after")
    def _bounded(self) -> ProgrammedCurrent:
        if self.min is None and self.max is None:
            raise ValueError("give the current's min, its max, or both")
        return self


if TYPE_CHECKING:
    AnyQuantity = Quantity
else:
    AnyQuantity = quantity_field(None)


class Setting(_Strict):
    """A programmed value a chip holds only as offset + N * step, in range.

    Attributes:
        type: ``setting``.
        component: The chip.
        name: The setting's datasheet name, such as ``COV``.
        value: The value the design relies on.
        step: One step, in the same unit.
        offset: The value at step 0; 0 if not given.
        min: The least the chip accepts.
        max: The most the chip accepts.
        source: Where step and range come from, with the page.
    """

    type: Literal["setting"]
    component: str
    name: str
    value: AnyQuantity
    step: AnyQuantity
    offset: AnyQuantity | None = None
    min: AnyQuantity | None = None
    max: AnyQuantity | None = None
    source: str = USER_STATED

    @model_validator(mode="after")
    def _one_unit(self) -> Setting:
        given = [self.value, self.step, self.offset, self.min, self.max]
        units = {q.unit for q in given if q is not None}
        if len(units) > 1:
            raise ValueError(
                f"{self.name}: value, step, offset, min and max must share one "
                f"unit; got {', '.join(sorted(units))}"
            )
        if self.step.value <= 0:
            raise ValueError(f"{self.name}: a step must be above zero")
        if self.min is None and self.max is None:
            raise ValueError(f"{self.name}: give the range the chip accepts")
        return self


Requirement = Annotated[
    MinCapacitance
    | RailVoltage
    | MaxJunctionTemp
    | MaxComponentPower
    | MinAttenuation
    | MaxInductorCurrent
    | MaxRipple
    | MinHoldupTime
    | MaxRailCurrent
    | ProgrammedCurrent
    | Setting,
    Field(discriminator="type"),
]


class Placement(_Strict):
    """Relative placement hints; never coordinates.

    Attributes:
        flow: The direction the sheet reads.
        order: Blocks in reading order.
    """

    flow: Literal["left_to_right"] = "left_to_right"
    order: list[str] = Field(default_factory=list[str])


#: The kinds of spread Kairo has a default margin for.
MarginKind = Literal[
    "resistor",
    "capacitor",
    "inductor",
    "supply",
    "forward_voltage",
    "regulator_output",
]


class Policy(_Strict):
    """Choices a project makes about how its checks reckon.

    A check that used one says so in its result.

    Attributes:
        margins: ``default`` to reckon with Kairo's own generous margin
            wherever the design states no tolerance; ``stated`` to use
            only what the design states, so that a missing tolerance
            leaves a result unverified. ``stated`` is for the check
            before a board is ordered.
        default_margins: A project's own figure for any of Kairo's
            default margins, by kind: ``resistor``, ``capacitor``,
            ``inductor``, ``supply``, ``forward_voltage``,
            ``regulator_output``.
        theta_ja_margin: How far above a datasheet's typical (JEDEC board)
            theta_JA to reckon, for example ``20%``; with none, a junction
            temperature cannot be proven.
        prototype: A first board: the review asks for rail test points.
    """

    margins: Literal["default", "stated"] = "default"
    default_margins: dict[MarginKind, Percent] = Field(
        default_factory=dict[MarginKind, Percent]
    )
    theta_ja_margin: Percent | None = None
    prototype: bool = False


class Sheet(_Strict):
    """One sheet of a design drawn on several.

    Attributes:
        title: The sheet's title, shown in its title block and on its
            symbol on the root sheet.
        blocks: The blocks drawn on it, in reading order.
    """

    title: str = Field(min_length=1)
    blocks: list[str] = Field(min_length=1)


class TitleBlock(_Strict):
    """What every sheet's title block says besides its title.

    Attributes:
        company: Who the drawing is for or from.
        revision: The design's revision, such as ``C.5``.
        date: The revision's date, as written, such as ``2026-10-23``.
        comments: Up to four lines, such as the drawing's notes.
    """

    company: str = ""
    revision: str = ""
    date: str = ""
    comments: list[str] = Field(default_factory=list[str], max_length=4)


#: The board edges a part may be held against.
Edge = Literal["north", "south", "east", "west"]


class Pour(_Strict):
    """A net carried by copper pours rather than tracks.

    Attributes:
        net: The net.
        layers: The copper layers it is poured on; every copper layer if
            none are named.
        blocks: The blocks whose pads it serves, where it is poured; a pad
            of its net on any other part is joined by a track. Every pad of
            the net if none are named.
    """

    net: str
    layers: list[str] = Field(default_factory=list[str])
    blocks: list[str] = Field(default_factory=list[str])


class NetClass(_Strict):
    """Nets that share a track width and a clearance.

    A class named ``Default`` holds no nets: it sets the figures for every
    net no other class names, as KiCad's own Default class does.

    Attributes:
        name: The class's name, as KiCad shows it.
        nets: The nets in it, by the design's names.
        track: The width of its tracks.
        clearance: Its copper's least distance to another net's.
        via_diameter: Its vias' outer diameter, if not the Default's.
        via_drill: Its vias' drill, if not the Default's.
    """

    name: str
    nets: list[str] = Field(default_factory=list[str])
    track: Millimetres
    clearance: Millimetres
    via_diameter: Millimetres | None = None
    via_drill: Millimetres | None = None


class RouteGuide(_Strict):
    """A net routed before the rest, as if by hand.

    Attributes:
        net: The net.
        layers: Copper layers it may use; any if none.
    """

    net: str
    layers: list[str] = Field(default_factory=list[str])


class FabRules(_Strict):
    """The board maker's least figures, which KiCad's DRC then holds the board to.

    Each is the maker's published capability for the stackup ordered (for
    JLCPCB, its PCB capabilities page); none has a default, since a wrong
    one passes a board the maker cannot build.

    Attributes:
        min_track: The narrowest track.
        min_clearance: The least copper-to-copper distance.
        min_drill: The smallest plated hole.
        min_via_diameter: The smallest via pad.
        min_annular: The least annular ring of a via.
        min_hole_clearance: The least distance from a hole to copper.
        min_hole_to_hole: The least distance between holes.
        copper_edge_clearance: The least distance from copper to the edge.
    """

    min_track: Millimetres
    min_clearance: Millimetres
    min_drill: Millimetres
    min_via_diameter: Millimetres
    min_annular: Millimetres
    min_hole_clearance: Millimetres
    min_hole_to_hole: Millimetres
    copper_edge_clearance: Millimetres


class Board(_Strict):
    """What the printed circuit board must be, as constraints, never coordinates.

    Attributes:
        width: The board's width.
        height: The board's height.
        copper_layers: 2 or 4.
        outer_oz: Outer copper weight, in ounces per square foot.
        inner_oz: Inner copper weight, in ounces per square foot.
        thickness: The finished board's thickness.
        sides: Each block's place on the board, as a compass side:
            ``west``, ``center``, ``east`` across and ``north``,
            ``middle``, ``south`` down, alone or joined (``north-east``).
        edges: Parts held against a board edge, by reference: a
            connector, a radio module's antenna end, a bolt terminal. The
            part is turned so the side its footprint draws at the top
            faces out.
        pours: Nets carried by copper pours, such as a high-current path
            or a ground plane.
        clearance: The room kept between parts' courtyards.
        net_classes: Track widths and clearances by net; with any given,
            one must be ``Default``.
        rules: The board maker's least figures, for KiCad's DRC.
        spacing: The IPC-2221 column that sets the least spacing between
            two nets for the voltage between them, from each net's stated
            voltage (or min and max): ``B2`` for a bare board, ``B4`` for
            one with a permanent polymer coating. None keeps the classes'
            clearances alone.
        trace_temp_rise: How far a track may warm above the board, in
            degrees Celsius, when its copper is checked against its net's
            current (IPC-2221): the design's choice; 10 is a common,
            cautious one.
        routes: Nets routed before the rest, one at a time in this order,
            each then fixed: the hard or critical ones, as a designer
            routes them by hand before the autorouter.
    """

    width: Millimetres
    height: Millimetres
    copper_layers: Literal[2, 4] = 4
    outer_oz: float = Field(default=1.0, gt=0)
    inner_oz: float = Field(default=1.0, gt=0)
    thickness: Millimetres = Field(
        default_factory=lambda: Quantity(1.6, "mm")  # KiCad's default board.
    )
    sides: dict[str, str] = Field(default_factory=dict[str, str])
    edges: dict[str, Edge] = Field(default_factory=dict[str, Edge])
    pours: list[Pour] = Field(default_factory=list[Pour])
    clearance: Millimetres = Field(default_factory=lambda: Quantity(0.5, "mm"))
    net_classes: list[NetClass] = Field(default_factory=list[NetClass])
    rules: FabRules | None = None
    spacing: Literal["B1", "B2", "B3", "B4", "A6", "A7"] | None = None
    trace_temp_rise: float = Field(default=10.0, gt=0)
    routes: list[RouteGuide] = Field(default_factory=list[RouteGuide])


class Design(_Strict):
    """A whole circuit.

    Attributes:
        design: Its name, which becomes the project's file name.
        kicad_version: The KiCad major version to write for.
        nets: Every net by name.
        components: Every part by reference.
        connections: For each net, the pins on it as ``REF.PIN``.
        no_connect: Pins meant to be left open, as ``REF.PIN``. An open
            pin not listed here is reported as a fault.
        loads: Currents drawn by things outside the design.
        requirements: What must hold.
        blocks: Parts grouped for placement.
        placement: Relative placement hints.
        policy: The project's own choices about how checks reckon.
        sheets: For a design drawn on several sheets, each sheet by name
            (the name becomes its file's), in page order. Every part must
            then be in a block, and every block on exactly one sheet; the
            root sheet shows each as a sheet symbol, and nets that run
            between sheets as its pins.
        title_block: The title block's company, revision, date and notes.
        board: What the printed circuit board must be, for a design that
            is laid out (`kairo.pcb`).
    """

    design: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    kicad_version: Literal["9", "10"] = "10"
    nets: dict[str, Net]
    components: dict[str, Component]
    connections: dict[str, list[str]]
    no_connect: list[str] = Field(default_factory=list[str])
    loads: list[Load] = Field(default_factory=list[Load])
    requirements: list[Requirement] = Field(default_factory=list[Requirement])
    blocks: dict[str, list[str]] = Field(default_factory=dict[str, list[str]])
    placement: Placement = Field(default_factory=Placement)
    policy: Policy = Field(default_factory=Policy)
    sheets: dict[str, Sheet] = Field(default_factory=dict[str, Sheet])
    title_block: TitleBlock = Field(default_factory=TitleBlock)
    board: Board | None = None

    @model_validator(mode="after")
    def _names_agree(self) -> Design:
        problems: list[str] = []
        seen: dict[str, str] = {}
        for net, pins in self.connections.items():
            if net not in self.nets:
                problems.append(f"connections names net {net!r}, which nets does not")
            for pin in pins:
                ref, _ = split_pin(pin, self.components)
                if ref not in self.components:
                    problems.append(
                        f"{net}: {pin} names part {ref!r}, which is not one"
                    )
                if pin in seen:
                    problems.append(f"{pin} is on both {seen[pin]} and {net}")
                seen[pin] = net
        for pin in self.no_connect:
            ref, _ = split_pin(pin, self.components)
            if ref not in self.components:
                problems.append(
                    f"no_connect: {pin} names part {ref!r}, which is not one"
                )
        for load in self.loads:
            if load.net not in self.nets:
                problems.append(f"a load names net {load.net!r}, which nets does not")
        for requirement in self.requirements:
            for net in _nets_of(requirement):
                if net not in self.nets:
                    problems.append(
                        f"{requirement.type} names net {net!r}, which nets does not"
                    )
            ref = getattr(requirement, "component", None)
            if ref is not None and ref not in self.components:
                problems.append(
                    f"{requirement.type} names part {ref!r}, which is not one"
                )
        placed: dict[str, str] = {}
        for block, refs in self.blocks.items():
            for ref in refs:
                if ref not in self.components:
                    problems.append(
                        f"block {block} names part {ref!r}, which is not one"
                    )
                if ref in placed:
                    problems.append(f"{ref} is in both block {placed[ref]} and {block}")
                placed[ref] = block
        for block in self.placement.order:
            if block not in self.blocks:
                problems.append(
                    f"placement.order names block {block!r}, which is not one"
                )
        if self.sheets:
            problems += self._sheets_agree(placed)
        if self.board is not None:
            problems += [
                f"board.sides names block {b!r}, which is not one"
                for b in self.board.sides
                if b not in self.blocks
            ]
            problems += [
                f"board.edges names part {r!r}, which is not one"
                for r in self.board.edges
                if r not in self.components
            ]
            problems += [
                f"board.pours names net {p.net!r}, which nets does not"
                for p in self.board.pours
                if p.net not in self.nets
            ]
            problems += [
                f"board.pours {p.net} names block {b!r}, which is not one"
                for p in self.board.pours
                for b in p.blocks
                if b not in self.blocks
            ]
            problems += self._classes_agree(self.board.net_classes)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _classes_agree(self, classes: list[NetClass]) -> list[str]:
        """Known nets, each in one class, and a Default when any class is given."""
        problems: list[str] = []
        names = [c.name for c in classes]
        if classes and "Default" not in names:
            problems.append("board.net_classes needs a class named Default")
        problems += [
            f"board.net_classes names class {n!r} twice"
            for n in sorted(set(names))
            if names.count(n) > 1
        ]
        seen: dict[str, str] = {}
        for c in classes:
            if c.name == "Default" and c.nets:
                problems.append("board.net_classes Default holds no nets")
            for net in c.nets:
                if net not in self.nets:
                    problems.append(
                        f"board.net_classes {c.name} names net {net!r}, "
                        "which nets does not"
                    )
                elif net in seen:
                    problems.append(f"net {net} is in classes {seen[net]} and {c.name}")
                seen[net] = c.name
        return problems

    def _sheets_agree(self, placed: dict[str, str]) -> list[str]:
        """Every part in a block, every block on exactly one sheet."""
        problems: list[str] = []
        on: dict[str, str] = {}
        for name, sheet in self.sheets.items():
            if not _SHEET_NAME.fullmatch(name):
                problems.append(
                    f"sheet name {name!r} must be lower case letters, digits, _ or -"
                )
            for block in sheet.blocks:
                if block not in self.blocks:
                    problems.append(
                        f"sheet {name} names block {block!r}, which is not one"
                    )
                elif block in on:
                    problems.append(
                        f"block {block} is on both sheet {on[block]} and {name}"
                    )
                on[block] = name
        problems += [
            f"block {block} is on no sheet" for block in self.blocks if block not in on
        ]
        problems += [
            f"{ref} is in no block, and a design with sheets draws only blocks"
            for ref in self.components
            if ref not in placed
        ]
        if self.placement.order:
            problems.append(
                "placement.order is not used with sheets: "
                "each sheet's blocks are its order"
            )
        return problems

    def sheet_of(self) -> dict[str, str]:
        """Each part's sheet, for a design with sheets."""
        block_sheet = {
            block: name for name, sheet in self.sheets.items() for block in sheet.blocks
        }
        return {
            ref: block_sheet[block]
            for block, refs in self.blocks.items()
            for ref in refs
        }


_SHEET_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def split_pin(pin: str, refs: Collection[str] | None = None) -> tuple[str, str]:
    """Split ``REF.PIN`` into the part's reference and the pin's name.

    A drawn board may give a part a reference with a dot in it (``C.0.1``
    on a sheet used several times), which the text alone cannot tell from
    a pin name. Given the design's references, the longest one the text
    starts with is the part.

    Args:
        pin: The pin as a design writes it, for example ``U1.VO``.
        refs: The design's references, when the caller has them.

    Returns:
        The reference and the pin name. The name may itself hold dots.

    Raises:
        ValueError: If the text is not ``REF.PIN``.
    """
    if refs is not None:
        cut = pin.find(".")
        longest = ""
        while cut != -1:
            if pin[:cut] in refs and cut + 1 < len(pin):
                longest = pin[:cut]
            cut = pin.find(".", cut + 1)
        if longest:
            return longest, pin[len(longest) + 1 :]
    found = _PIN.fullmatch(pin)
    if found is None:
        raise ValueError(f"{pin!r} is not REF.PIN, for example U1.VO")
    return found.group("ref"), found.group("pin")


def _nets_of(requirement: object) -> tuple[str, ...]:
    if isinstance(requirement, MinCapacitance):
        return requirement.between
    if isinstance(requirement, RailVoltage):
        return (requirement.net,)
    if isinstance(requirement, MinAttenuation):
        return (requirement.input, requirement.output)
    return ()
