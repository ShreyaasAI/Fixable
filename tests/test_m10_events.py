"""M10a T1: the event type list (m10a-contract §1.1, decision 16).

`swf.events` is the single list; `append_event` (and so `PgTaskChannel.emit`) refuses unknown
types; every literal type emitted under `src/swf` is on the list; `web/src/events.ts` names the
same types and the same noise subset.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from m4_fakes import queued_task

from swf import events, store
from swf.db import Database

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "swf"
EVENTS_TS = ROOT / "web" / "src" / "events.ts"

# Positional index of the event type argument.
_TYPE_ARG = {"emit": 0, "append_event": 2}


def test_check_type_accepts_every_known_type() -> None:
    assert events.EVENT_TYPES
    for name in sorted(events.EVENT_TYPES):
        assert events.check_type(name) == name


@pytest.mark.parametrize("bad", ["note", "", "Status", "status ", "tool-call"])
def test_check_type_rejects_unknown_types(bad: str) -> None:
    with pytest.raises(ValueError, match="unknown event type"):
        events.check_type(bad)


def test_noise_types_are_a_subset_of_event_types() -> None:
    assert events.NOISE_TYPES
    assert events.NOISE_TYPES <= events.EVENT_TYPES


def test_append_event_refuses_an_unknown_type_and_inserts_nothing(db: Database) -> None:
    task = queued_task(db)
    with db.backend() as conn:
        before = conn.execute(
            "select count(*) as n, (select event_seq from public.tasks where id = %s) as seq"
            " from public.events where task_id = %s",
            [task.id, task.id],
        ).fetchone()
    with pytest.raises(ValueError, match="unknown event type"), db.backend() as conn:
        store.append_event(conn, task.id, "definitely_not_an_event", {})
    with db.backend() as conn:
        after = conn.execute(
            "select count(*) as n, (select event_seq from public.tasks where id = %s) as seq"
            " from public.events where task_id = %s",
            [task.id, task.id],
        ).fetchone()
    assert after == before


def test_append_event_still_accepts_a_known_type(db: Database) -> None:
    task = queued_task(db)
    with db.backend() as conn:
        event = store.append_event(conn, task.id, "message", {"role": "user", "content": "x"})
    assert event.type == "message"


def _literal_types() -> list[tuple[str, int, str]]:
    """(file, line, type) for each string-literal type passed to `emit(...)`/`append_event(...)`."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else ""
            )
            if name not in _TYPE_ARG:
                continue
            arg: ast.expr | None = None
            for kw in node.keywords:
                if kw.arg == "type":
                    arg = kw.value
            if arg is None and len(node.args) > _TYPE_ARG[name]:
                arg = node.args[_TYPE_ARG[name]]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append((str(path.relative_to(ROOT)), node.lineno, arg.value))
    return found


def test_every_literal_emitted_type_is_on_the_list() -> None:
    found = _literal_types()
    assert len(found) >= 25, f"the scan found only {len(found)} literals; is it still matching?"
    unknown = sorted({f"{f}:{line} {t!r}" for f, line, t in found if t not in events.EVENT_TYPES})
    assert unknown == []


def _ts_array(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name} = \[(.*?)\] as const;", text, re.DOTALL)
    assert m is not None, f"web/src/events.ts has no `export const {name} = [...] as const;`"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_web_events_ts_matches_the_python_sets() -> None:
    text = EVENTS_TS.read_text(encoding="utf-8")
    event_types = _ts_array(text, "EVENT_TYPES")
    noise_types = _ts_array(text, "NOISE_TYPES")
    assert len(event_types) == len(set(event_types))
    assert len(noise_types) == len(set(noise_types))
    assert set(event_types) == set(events.EVENT_TYPES)
    assert set(noise_types) == set(events.NOISE_TYPES)
