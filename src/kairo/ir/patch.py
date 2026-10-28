"""Small, named changes to a design.

After a design exists it is changed through these operations, never by
sending the whole description again, so an edit says exactly what it
touches and stays easy to review. A patch is applied to a copy and the
result is validated as a whole: either every operation applies and the
new design is valid, or the design is unchanged and the error says which
operation failed.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from kairo.ir.models import PARAMETERS, Component, Design, Net, split_pin


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddNet(_Op):
    """Add a net.

    Attributes:
        op: ``add_net``.
        name: The new net's name.
        net: Its role and, for a power net, its voltage.
    """

    op: Literal["add_net"]
    name: str
    net: Net


class AddComponent(_Op):
    """Add a part.

    Attributes:
        op: ``add_component``.
        ref: The new part's reference.
        component: The part.
        block: A block to place it with, created if absent.
    """

    op: Literal["add_component"]
    ref: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    component: Component
    block: str | None = None


class Connect(_Op):
    """Put pins on a net.

    Attributes:
        op: ``connect``.
        net: The net, which must exist.
        pins: The pins, as ``REF.PIN``. A pin already on another net is an
            error; disconnect it first.
    """

    op: Literal["connect"]
    net: str
    pins: list[str] = Field(min_length=1)


class Disconnect(_Op):
    """Take pins off whatever net they are on.

    Attributes:
        op: ``disconnect``.
        pins: The pins, as ``REF.PIN``, written as the design has them.
    """

    op: Literal["disconnect"]
    pins: list[str] = Field(min_length=1)


class SetParam(_Op):
    """Set one fact about a part.

    Attributes:
        op: ``set_param``.
        ref: The part.
        name: A parameter name, or ``value``, ``footprint`` or ``source``.
        value: The new value as text with its unit, or ``None`` to remove
            a parameter.
    """

    op: Literal["set_param"]
    ref: str
    name: str
    value: str | None


class Remove(_Op):
    """Remove a part, with its connections and its place in any block.

    Attributes:
        op: ``remove``.
        ref: The part. A part a requirement names cannot be removed until
            the requirement is.
    """

    op: Literal["remove"]
    ref: str


PatchOp = Annotated[
    AddNet | AddComponent | Connect | Disconnect | SetParam | Remove,
    Field(discriminator="op"),
]
_OPS: TypeAdapter[list[PatchOp]] = TypeAdapter(list[PatchOp])


class PatchError(ValueError):
    """A patch does not apply; the design is unchanged.

    Attributes:
        index: Which operation failed, from 0, or ``None`` if the patch as
            a whole left the design invalid.
    """

    def __init__(self, message: str, index: int | None = None) -> None:
        """Record what failed.

        Args:
            message: What went wrong.
            index: Which operation, if one.
        """
        where = f"operation {index}: " if index is not None else ""
        super().__init__(f"E_PATCH_INVALID: {where}{message}")
        self.index = index


def parse_patch(operations: object) -> list[PatchOp]:
    """Validate a list of operations given as plain data.

    Args:
        operations: A list of objects, each with an ``op`` field.

    Returns:
        The operations.

    Raises:
        PatchError: If the data is not a list of known operations.
    """
    try:
        return _OPS.validate_python(operations)
    except ValidationError as exc:
        raise PatchError(str(exc)) from exc


def apply_patch(design: Design, operations: list[PatchOp]) -> Design:
    """Apply operations to a design and return the new design.

    Args:
        design: The design, which is not changed.
        operations: The operations, applied in order.

    Returns:
        The new design, validated as a whole.

    Raises:
        PatchError: If an operation cannot apply or the result is not a
            valid design.
    """
    data: dict[str, Any] = copy.deepcopy(
        design.model_dump(mode="json", exclude_defaults=True)
    )
    data.setdefault("blocks", {})
    for index, operation in enumerate(operations):
        try:
            _apply(data, operation)
        except ValueError as exc:
            raise PatchError(str(exc), index) from exc
    try:
        return Design.model_validate(data)
    except ValidationError as exc:
        raise PatchError(str(exc)) from exc


def _apply(data: dict[str, Any], operation: PatchOp) -> None:
    nets: dict[str, Any] = data["nets"]
    parts: dict[str, Any] = data["components"]
    connections: dict[str, list[str]] = data["connections"]
    if isinstance(operation, AddNet):
        if operation.name in nets:
            raise ValueError(f"net {operation.name!r} already exists")
        nets[operation.name] = operation.net.model_dump(
            mode="json", exclude_defaults=True
        )
    elif isinstance(operation, AddComponent):
        if operation.ref in parts:
            raise ValueError(f"part {operation.ref!r} already exists")
        parts[operation.ref] = operation.component.model_dump(
            mode="json", exclude_defaults=True
        )
        if operation.block is not None:
            data["blocks"].setdefault(operation.block, []).append(operation.ref)
    elif isinstance(operation, Connect):
        if operation.net not in nets:
            raise ValueError(
                f"net {operation.net!r} does not exist; add it with add_net first"
            )
        on = {pin: net for net, pins in connections.items() for pin in pins}
        for pin in operation.pins:
            split_pin(pin)
            if on.get(pin, operation.net) != operation.net:
                raise ValueError(f"{pin} is on {on[pin]}; disconnect it first")
            if pin not in on:
                connections.setdefault(operation.net, []).append(pin)
                on[pin] = operation.net
    elif isinstance(operation, Disconnect):
        for pin in operation.pins:
            holder = next((n for n, pins in connections.items() if pin in pins), None)
            if holder is None:
                raise ValueError(f"{pin} is not connected to anything")
            connections[holder].remove(pin)
    elif isinstance(operation, SetParam):
        if operation.ref not in parts:
            raise ValueError(f"part {operation.ref!r} does not exist")
        part: dict[str, Any] = parts[operation.ref]
        if operation.name in ("value", "footprint", "source"):
            if operation.value is None:
                part.pop(operation.name, None)
            else:
                part[operation.name] = operation.value
        elif operation.name in PARAMETERS:
            params: dict[str, Any] = part.setdefault("params", {})
            if operation.value is None:
                params.pop(operation.name, None)
            else:
                params[operation.name] = operation.value
        else:
            raise ValueError(
                f"unknown parameter {operation.name!r}; known: value, footprint, "
                f"source, {', '.join(sorted(PARAMETERS))}"
            )
    else:
        if operation.ref not in parts:
            raise ValueError(f"part {operation.ref!r} does not exist")
        del parts[operation.ref]
        for net, pins in connections.items():
            connections[net] = [
                p for p in pins if split_pin(p, parts)[0] != operation.ref
            ]
        for block, refs in data["blocks"].items():
            data["blocks"][block] = [r for r in refs if r != operation.ref]
