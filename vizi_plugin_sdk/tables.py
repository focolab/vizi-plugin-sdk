"""A ``table`` widget's rows as data (``control_plane_protocol.md`` §8.4): the
rules a host applies to ``set_table``, ``patch_table`` and a table's
``set_value``, in one place for everything that has to agree with them -- the
host's widget, a closed panel's hold, and this SDK's outbox when it merges.

A merge of two table ops is exact only when the table they land on is known:
whether a patch crosses the row bound depends on the rows already there, and
whether a row has too many cells on the declared columns.  So :class:`TableState`
is the table as the host holds it, and :func:`apply_op` either applies an op
to it or says why the host would refuse that op alone.
"""

from __future__ import annotations

from . import manifest as _manifest


class TableState:
    """A table's rows in display order (``keys``), each key's ``TableRow``, and
    the selected keys."""

    __slots__ = ("keys", "rows", "selection")

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.rows: dict[str, object] = {}
        self.selection: list[str] = []

    def copy(self) -> "TableState":
        other = TableState()
        other.keys = list(self.keys)
        other.rows = dict(self.rows)
        other.selection = list(self.selection)
        return other


def check_rows(rows, width: "int | None", widget_id: str) -> "str | None":
    """Why a host refuses these rows whole, or ``None``: a key empty or longer
    than ``TABLE_MAX_KEY_CHARS``, or more cells than ``width`` columns."""
    for row in rows:
        if not 0 < len(row.key) <= _manifest.TABLE_MAX_KEY_CHARS:
            return (
                f"INVALID_ARGUMENT: table {widget_id!r} row keys must be "
                f"non-empty and at most {_manifest.TABLE_MAX_KEY_CHARS} characters"
            )
        if width is not None and len(row.cells) > width:
            return (
                f"INVALID_ARGUMENT: table {widget_id!r} row {row.key!r} has "
                f"{len(row.cells)} cells for {width} columns"
            )
    return None


def _too_many(widget_id: str, count: int) -> str:
    return (
        f"INVALID_ARGUMENT: table {widget_id!r} holds at most "
        f"{_manifest.TABLE_MAX_ROWS} rows, not {count}"
    )


def apply_op(state: TableState, op, width: "int | None", widget_id: str) -> "str | None":
    """Apply a ``set_table`` or ``patch_table`` to ``state`` as a host does, or
    return why the host refuses it whole and leave ``state`` as it was.  Rows
    replaced keep the selection of the keys still there; a key a patch deletes
    loses it, even if the same patch upserts it again -- that is a fresh row."""
    kind = op.WhichOneof("op")
    if kind == "set_table":
        rows = list(op.set_table.rows)
        if len(rows) > _manifest.TABLE_MAX_ROWS:
            return _too_many(widget_id, len(rows))
        why = check_rows(rows, width, widget_id)
        if why is not None:
            return why
        keys = [row.key for row in rows]
        if len(set(keys)) != len(keys):
            return f"INVALID_ARGUMENT: table {widget_id!r} row keys must be distinct"
        state.keys = keys
        state.rows = {row.key: row for row in rows}
        gone: set = set()
    else:
        patch = op.patch_table
        why = check_rows(patch.upsert, width, widget_id)
        if why is not None:
            return why
        gone = set(patch.delete)
        keys = [k for k in state.keys if k not in gone]
        rows = {k: state.rows[k] for k in keys}
        for row in patch.upsert:
            if row.key not in rows:
                keys.append(row.key)
            rows[row.key] = row
        if len(keys) > _manifest.TABLE_MAX_ROWS:
            return _too_many(widget_id, len(keys))
        state.keys, state.rows = keys, rows
    state.selection = [k for k in state.selection if k in state.rows and k not in gone]
    return None


def select(state: TableState, value, multiple: bool, widget_id: str) -> "str | None":
    """A table's ``set_value``: one key (``Value.s``, ``""`` for none) or
    several (``Value.strings``); duplicates collapse, and an empty list of
    either kind -- ``Value.strings`` or ``Value.ints`` with no entries --
    clears (an SDK that encodes a bare ``[]`` as ints means no keys too).
    Refused with the reason, the selection unchanged, for a key the table does
    not hold or more than one key on a single-select table."""
    kind = value.WhichOneof("kind")
    if kind == "s":
        keys = [value.s] if value.s else []
    elif kind == "strings":
        keys = list(dict.fromkeys(value.strings.values))
    elif kind == "ints" and not value.ints.values:
        keys = []
    else:
        return f"table {widget_id!r} takes a row key or a list of keys"
    if len(keys) > 1 and not multiple:
        return f"table {widget_id!r} selects one row, not {len(keys)}"
    missing = [k for k in keys if k not in state.rows]
    if missing:
        return f"table {widget_id!r} has no row {missing[0]!r}"
    state.selection = keys
    return None
