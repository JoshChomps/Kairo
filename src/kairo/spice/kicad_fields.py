"""A part's registered model, written as KiCad's own simulation fields.

With these fields KiCad's simulator runs the same model Kairo did. The
form is KiCad's, measured on KiCad 10.0.6: its LM317 demo, re-saved with
``kicad-cli sch upgrade``, names the model file in ``Sim.Library``, the
subcircuit in ``Sim.Name``, and in ``Sim.Pins`` each symbol pin's number
with the position of the subcircuit node it connects to, ``3=1 1=2 2=3``.
kicad-cli's SPICE netlist then gives ``XU1 <nets in node order> LM317``.
A symbol pin listed twice feeds both nodes (measured: ``1=2 1=4`` gave
the pin's net in places 2 and 4).

``Sim.Library`` holds the registered file's absolute path, so KiCad reads
the very file whose hash Kairo checked; a project moved to another
computer needs the file there too (A175).
"""

from __future__ import annotations

from kairo.library.grounding import Grounded
from kairo.spice.registry import GROUND_NODE, ModelRegistry, RegistryError

#: KiCad's names for the three fields.
LIBRARY, NAME, PINS = "Sim.Library", "Sim.Name", "Sim.Pins"


def sim_fields(
    grounded: Grounded, registry: ModelRegistry | None
) -> tuple[dict[str, tuple[tuple[str, str], ...]], list[str]]:
    """The simulation fields for every part with a registered model.

    Args:
        grounded: The design with its pins resolved.
        registry: The models folder; ``None`` gives no fields.

    Returns:
        The fields by reference, each as ``(name, value)`` pairs, and a
        line for each part that has a model whose fields cannot be
        written, saying why.
    """
    fields: dict[str, tuple[tuple[str, str], ...]] = {}
    skipped: list[str] = []
    if registry is None:
        return fields, skipped
    for ref, part in grounded.design.components.items():
        number = part.params.get("mpn")
        try:
            entry = registry.find(
                mpn=number if isinstance(number, str) else None, lib_id=part.lib_id
            )
        except RegistryError as exc:
            skipped.append(f"{ref}: {exc}")
            continue
        if entry is None:
            continue
        by_name: dict[str, str] = {}
        for pin in grounded.pins:
            if pin.ref == ref:
                by_name.setdefault(pin.pin.name, pin.pin.number)
        pairs: list[str] = []
        why = ""
        for position, name in enumerate(entry.nodes, start=1):
            if name == GROUND_NODE:
                why = (
                    f"its model ties node {position} to ground, which KiCad's "
                    "fields can only give through a pin"
                )
                break
            if name not in by_name:
                why = f"its model's node {position} needs pin {name} connected"
                break
            pairs.append(f"{by_name[name]}={position}")
        if why:
            skipped.append(f"{ref}: {why}")
            continue
        # In the order KiCad's own re-save gives them.
        fields[ref] = (
            (LIBRARY, registry.path(entry).as_posix()),
            (NAME, entry.subckt),
            (PINS, " ".join(pairs)),
        )
    return fields, skipped
