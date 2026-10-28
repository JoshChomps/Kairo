# What a model and a tool send back is plain JSON, which a strict type
# checker cannot follow.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
"""Let a local model design the reference circuits through Kairo, and score it.

A model served by Ollama on this machine is given one reference design's
spec in words and Kairo's own tools, exactly as an MCP client would offer
them, and is left to work until it stops calling tools or runs out of
turns. Everything it did is saved: each tool call with its arguments and
result, the tokens Ollama counted, and KiCad's netlist of whatever it
built. The verdict is then worked out from those files alone, so::

    uv run python evals/model_run.py run --model qwen3.5:9b --design rc_lowpass
    uv run python evals/model_run.py rescore evals/results/<run>

``rescore`` calls no model and needs neither KiCad nor Ollama. A run is
one attempt: the plan's protocol asks for three per design and model
before any comparison is drawn, and this script makes one at a time.

What is judged:

- a design spec passes if the model's last write was accepted by Kairo
  (every verifier layer and KiCad's rule check) and KiCad's netlist of
  it has the same parts joined the same way as the reference's, whatever
  the model called its parts and nets;
- a refusal spec passes if Kairo refused the model's design for the
  expected requirement and nothing was written.

Two netlists are compared by pairing their parts and nets (see `matches`):
the same symbols, joined the same way by the same pin numbers.

Only the address ``127.0.0.1`` is called. No model outside this machine
is used.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from kairo import __version__
from kairo.kicad.netlist import parse_netlist

HERE = Path(__file__).resolve().parent
DESIGNS = HERE / "reference_designs"
RESULTS = HERE / "results"
OLLAMA = "http://127.0.0.1:11434"
#: Turns a model may take. Kairo's own stop rule ends write attempts sooner.
MAX_TURNS = 30
#: The longest tool result passed back to the model, in characters: the
#: most any Kairo tool returns, so nothing a tool says is cut.
RESULT_LIMIT = 25_000
#: The context Ollama is asked for, in tokens: room for the tool list, the
#: design schema and a few rounds of findings.
CONTEXT = 32_768

BRIEF = """You design electronic circuits with the Kairo tools.

Work like this:
1. Call design_schema once to learn the exact shape of a design.
2. Find each part with find_symbol and read its pins with get_symbol_pins. \
Never write a pin name or number from memory.
3. Check the design with plan_design and fix what it reports.
4. Build it with apply_design into the folder you are given.
5. If Kairo says a requirement cannot be met, do not loosen it: stop and \
say so with the numbers Kairo gave.

State a value only if the spec gives it. Stop when the design is built \
or refused, and say in one sentence which."""


def _pins(nets: dict[str, list[tuple[str, str]]]) -> dict[str, dict[str, str]]:
    """Each part's pins: the net each pin number is on."""
    pins: dict[str, dict[str, str]] = {}
    for name, members in nets.items():
        for ref, number in members:
            pins.setdefault(ref, {})[number] = name
    return pins


def matches(
    got_parts: dict[str, str],
    got_nets: dict[str, list[tuple[str, str]]],
    want_parts: dict[str, str],
    want_nets: dict[str, list[tuple[str, str]]],
) -> bool:
    """Whether two netlists are one circuit under different names.

    They are if the parts can be paired, each with one of the same
    symbol, and the nets paired too, so that every pin number of a part
    sits on the net its partner's pin of that number sits on. The
    pairing is found by trying each candidate in turn and going back on
    a contradiction, which is quick for circuits of this size because a
    part can only pair with one of the same symbol and pin numbers.

    Args:
        got_parts: One netlist's parts: symbol by reference.
        got_nets: Its nets: pins as (reference, pin number) by net.
        want_parts: The other's parts.
        want_nets: The other's nets.

    Returns:
        True if such a pairing exists.
    """
    got_pins, want_pins = _pins(got_nets), _pins(want_nets)
    if sorted(got_parts.values()) != sorted(want_parts.values()):
        return False
    if sorted(len(m) for m in got_nets.values()) != sorted(
        len(m) for m in want_nets.values()
    ):
        return False
    order = sorted(got_parts, key=lambda ref: (-len(got_pins.get(ref, {})), ref))

    def pair(index: int, used: frozenset[str], net_of: dict[str, str]) -> bool:
        if index == len(order):
            return True
        ref = order[index]
        mine = got_pins.get(ref, {})
        for other in want_parts:
            theirs = want_pins.get(other, {})
            if (
                other in used
                or want_parts[other] != got_parts[ref]
                or mine.keys() != theirs.keys()
            ):
                continue
            trial = dict(net_of)
            taken = set(trial.values())
            agrees = True
            for number, net in mine.items():
                if net in trial:
                    agrees = trial[net] == theirs[number]
                else:
                    agrees = theirs[number] not in taken
                    trial[net] = theirs[number]
                    taken.add(theirs[number])
                if not agrees:
                    break
            if agrees and pair(index + 1, used | {other}, trial):
                return True
        return False

    return pair(0, frozenset(), {})


def same_circuit(
    want_parts: dict[str, str],
    want_nets: dict[str, list[str]],
    netlist_text: str,
) -> bool:
    """Whether KiCad's netlist is the reference circuit under other names.

    Args:
        want_parts: The reference's parts: symbol by reference.
        want_nets: The reference's nets: pins as ``REF.NUMBER`` by net.
        netlist_text: KiCad's netlist of what was built.

    Returns:
        True if the two have the same parts joined the same way.
    """
    got = parse_netlist(netlist_text)
    wanted: dict[str, list[tuple[str, str]]] = {}
    for name, pins in want_nets.items():
        wanted[name] = []
        for pin in pins:
            ref, _, number = pin.rpartition(".")
            wanted[name].append((ref, number))
    return matches(
        {part.ref: part.lib_id for part in got.components},
        {net.name: [(n.ref, n.pin) for n in net.nodes] for net in got.nets},
        want_parts,
        wanted,
    )


def verdict(saved: Path) -> dict[str, Any]:
    """Score one design from the files a run saved for it.

    Args:
        saved: The run's folder for the design.

    Returns:
        ``passed`` and one line per check.
    """
    expected = json.loads((saved / "expected.json").read_text(encoding="utf-8"))
    reference = json.loads((saved / "reference.json").read_text(encoding="utf-8"))
    calls = json.loads((saved / "transcript.json").read_text(encoding="utf-8"))
    writes = [c for c in calls if c["tool"] == "apply_design" and not c["error"]]
    last = writes[-1]["result"] if writes else None
    netlist = saved / "netlist.net"
    checks: dict[str, bool] = {}
    if expected["outcome"] == "refusal":
        refused = [
            w["result"]
            for w in writes
            if w["result"].get("code") == "E_REQUIREMENT_UNSATISFIABLE"
        ]
        checks["Kairo refused the design"] = bool(refused)
        checks["refused for the expected requirement"] = any(
            expected["refused_for"] in str(r.get("failed_requirements"))
            for r in refused
        )
        checks["nothing was written"] = not any(
            w["result"].get("written") for w in writes
        )
    else:
        checks["the last write was accepted"] = bool(last and last.get("written"))
        checks["KiCad's netlist is the reference circuit"] = (
            netlist.is_file()
            and same_circuit(
                {ref: part["lib_id"] for ref, part in reference["components"].items()},
                expected["netlist"],
                netlist.read_text(encoding="utf-8"),
            )
        )
    return {"passed": all(checks.values()), "checks": checks}


def _chat(model: str, messages: list[dict[str, Any]], tools: list[Any]) -> Any:
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {"num_ctx": CONTEXT, "temperature": 0},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA}/api/chat", body, {"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=1800) as reply:
            return json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        # Ollama says why in the body (a model that ran out of memory while
        # loading, say); the status alone says only that something failed.
        detail = exc.read().decode("utf-8", "replace").strip()
        raise RuntimeError(f"Ollama answered {exc.code}: {detail}") from exc


async def attempt(model: str, name: str, saved: Path, work: Path) -> dict[str, Any]:
    """Let a model work on one design and save what it did.

    Args:
        model: The model's name in Ollama.
        name: The reference design's folder name.
        saved: Where this design's records go.
        work: A folder the model may build into.

    Returns:
        The run's figures: turns, tool calls, tokens, seconds and how it
        ended.
    """
    from mcp import Client

    from kairo.server import Settings, build_server

    source = DESIGNS / name
    saved.mkdir(parents=True)
    for file, target in (
        ("expected.json", "expected.json"),
        ("design.json", "reference.json"),
        ("spec.md", "spec.md"),
    ):
        (saved / target).write_bytes((source / file).read_bytes())
    project = work / name
    # A local model costs nothing to run, so only the turn limit here and
    # Kairo's own repeat and write-attempt rules stop it.
    settings = Settings(
        (work,), work / "_cache", None, tier="write", calls_per_ten_minutes=0
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": BRIEF},
        {
            "role": "user",
            "content": (source / "spec.md").read_text(encoding="utf-8")
            + f"\n\nBuild it into the folder {project.as_posix()}.",
        },
    ]
    calls: list[dict[str, Any]] = []
    figures: dict[str, Any] = {
        "turns": 0,
        "prompt_tokens": 0,
        "output_tokens": 0,
        "ended": "turns",
    }
    started = time.perf_counter()
    async with Client(build_server(settings)) as client:
        offered = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in (await client.list_tools()).tools
        ]
        for _ in range(MAX_TURNS):
            reply = await asyncio.to_thread(_chat, model, messages, offered)
            figures["turns"] += 1
            figures["prompt_tokens"] += reply.get("prompt_eval_count", 0)
            figures["output_tokens"] += reply.get("eval_count", 0)
            message = reply["message"]
            messages.append(message)
            wanted = message.get("tool_calls") or []
            if not wanted:
                figures["ended"] = "the model stopped"
                (saved / "final.txt").write_text(
                    message.get("content", ""), encoding="utf-8"
                )
                break
            for call in wanted:
                tool = call["function"]["name"]
                arguments = call["function"].get("arguments") or {}
                try:
                    result = await client.call_tool(tool, arguments)
                    failed = bool(result.is_error)
                    content: Any = (
                        result.structured_content
                        if result.structured_content is not None and not failed
                        else " ".join(
                            getattr(part, "text", "") for part in result.content
                        )
                    )
                except Exception as exc:
                    failed, content = True, f"{type(exc).__name__}: {exc}"
                calls.append(
                    {
                        "tool": tool,
                        "arguments": arguments,
                        "error": failed,
                        "result": content,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": tool,
                        "content": json.dumps(content)[:RESULT_LIMIT],
                    }
                )
                # Saved as it goes, so a slow run can be watched and a
                # run that is stopped still leaves what it did.
                (saved / "transcript.json").write_text(
                    json.dumps(calls, indent=1), encoding="utf-8"
                )
                print(
                    f"    turn {figures['turns']}: {tool}"
                    f"{' (error)' if failed else ''}",
                    file=sys.stderr,
                    flush=True,
                )
        schematic = project / f"{project.name}.kicad_sch"
        found = sorted(project.glob("*.kicad_sch")) if project.is_dir() else []
        if found:
            # KiCad itself says what was built, through Kairo's own runner.
            from kairo.kicad.cli import KicadCli

            cli, _ = KicadCli.find()
            if cli is not None:
                text = cli.export_netlist(
                    schematic if schematic in found else found[0]
                ).text
                if text is not None:
                    (saved / "netlist.net").write_text(text, encoding="utf-8")
    figures["seconds"] = round(time.perf_counter() - started, 1)
    figures["tool_calls"] = len(calls)
    (saved / "transcript.json").write_text(
        json.dumps(calls, indent=1), encoding="utf-8"
    )
    (saved / "figures.json").write_text(json.dumps(figures, indent=1), encoding="utf-8")
    return figures


def run(model: str, names: list[str]) -> int:
    """Run a model on some reference designs and record the run.

    Args:
        model: The model's name in Ollama.
        names: The designs to attempt; all of them if empty.

    Returns:
        0 if every design's verdict is a pass, otherwise 1.
    """
    import tempfile

    names = names or sorted(
        p.name for p in DESIGNS.iterdir() if (p / "design.json").is_file()
    )
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H%M%SZ")
    output = RESULTS / f"{stamp}-model-{model.replace(':', '-').replace('/', '-')}"
    summary: dict[str, Any] = {
        "kairo": __version__,
        "kind": "one attempt per design by a local model; not a comparison",
        "model": model,
        "client": "evals/model_run.py through Ollama's chat API",
        "designs": {},
    }
    for name in names:
        # The server's library index is still open when the folder is
        # removed, which Windows refuses; what is left is in the system's
        # temporary folder and is not part of the record.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
            figures = asyncio.run(attempt(model, name, output / name, Path(work)))
        scored = verdict(output / name)
        summary["designs"][name] = {**scored, **figures}
        print(
            f"{'PASS' if scored['passed'] else 'MISS'}  {name}  "
            f"{figures['tool_calls']} calls, {figures['turns']} turns, "
            f"{figures['prompt_tokens'] + figures['output_tokens']} tokens, "
            f"{figures['seconds']} s, {figures['ended']}"
        )
    (output / "summary.json").write_text(
        json.dumps(summary, indent=1), encoding="utf-8"
    )
    print(f"recorded in {output}")
    return 0 if all(d["passed"] for d in summary["designs"].values()) else 1


def rescore(output: Path) -> int:
    """Work the verdicts out again from a run's files and compare.

    Args:
        output: A run's folder.

    Returns:
        0 if every recomputed verdict equals the recorded one, otherwise 1.
    """
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    same = True
    for name, recorded in summary["designs"].items():
        again = verdict(output / name)
        agrees = again["passed"] == recorded["passed"]
        same = same and agrees
        print(f"{'same' if agrees else 'DIFFERENT'}  {name}  passed={again['passed']}")
    return 0 if same else 1


def main() -> None:
    """Run or rescore from the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    running = commands.add_parser("run")
    running.add_argument("--model", required=True)
    running.add_argument("--design", action="append", default=[])
    scoring = commands.add_parser("rescore")
    scoring.add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.command == "run":
        sys.exit(run(args.model, args.design))
    sys.exit(rescore(args.folder))


if __name__ == "__main__":
    main()
