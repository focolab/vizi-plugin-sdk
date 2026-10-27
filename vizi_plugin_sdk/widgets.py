"""Panel widgets for :func:`~.declare.panel`, one constructor per kind (§3.3.1).

Each returns the widget's manifest object -- a plain dict, so a hand-written
one is equally welcome anywhere these are.  The id comes first; ``command``
takes the ``@command`` handler itself or its id.  Fields left out are left to
the host's defaults, as everywhere in :mod:`.declare`.
"""

from __future__ import annotations

from typing import Any, Callable, Union

from .declare import UNSET, _given

Command = Union[str, Callable]


def label(id: str, text: str = UNSET, *, text_format: str = UNSET) -> dict:
    return {"kind": "label", "id": id, **_given(text=text, text_format=text_format)}


def button(id: str, text: str = UNSET, *, command: Command, moves_view: bool = UNSET) -> dict:
    return {
        "kind": "button",
        "id": id,
        **_given(text=text, moves_view=moves_view),
        "command": command,
    }


def slider(
    id: str,
    *,
    command: Command,
    min: float = UNSET,
    max: float = UNSET,
    step: float = UNSET,
    value: float = UNSET,
    float: bool = UNSET,
    moves_view: bool = UNSET,
) -> dict:
    return {
        "kind": "slider",
        "id": id,
        "command": command,
        **_given(min=min, max=max, step=step, value=value, float=float, moves_view=moves_view),
    }


def dropdown(
    id: str,
    options: "list[str]",
    *,
    command: Command,
    value: str = UNSET,
    moves_view: bool = UNSET,
) -> dict:
    return {
        "kind": "dropdown",
        "id": id,
        "command": command,
        "options": list(options),
        **_given(value=value, moves_view=moves_view),
    }


def checkbox(
    id: str,
    text: str = UNSET,
    *,
    command: Command,
    value: bool = UNSET,
    moves_view: bool = UNSET,
) -> dict:
    return {
        "kind": "checkbox",
        "id": id,
        "command": command,
        **_given(text=text, value=value, moves_view=moves_view),
    }


def line_edit(
    id: str,
    *,
    command: Command,
    placeholder: str = UNSET,
    value: str = UNSET,
    moves_view: bool = UNSET,
) -> dict:
    return {
        "kind": "line_edit",
        "id": id,
        "command": command,
        **_given(placeholder=placeholder, value=value, moves_view=moves_view),
    }


def image(id: str, *, command: Command = UNSET, moves_view: bool = UNSET) -> dict:
    """The target of ``push_image``, scaled to the space its panel gives it.
    A :func:`widget_geometry` handler hears that space; an image pushed at its
    ``pixels`` is shown one image pixel per screen pixel.  With a ``command``, a
    click fires it with ``{"x", "y", "button", "modifiers", "image"}``, in the
    image's own pixels whatever the scale."""
    return {"kind": "image", "id": id, **_given(command=command, moves_view=moves_view)}


def progress(id: str, *, value: float = UNSET, indeterminate: bool = UNSET) -> dict:
    return {"kind": "progress", "id": id, **_given(value=value, indeterminate=indeterminate)}


# -- host pickers: the choices are vizi's, not the manifest's ----------


def channel_picker(
    id: str,
    *,
    command: Command,
    multiple: bool = UNSET,
    value: "int | list[int]" = UNSET,
    moves_view: bool = UNSET,
) -> dict:
    """The open dataset's channels.  Fires ``{"value": index}``, or with
    ``multiple`` a tuple of indices."""
    return {
        "kind": "channel_picker",
        "id": id,
        "command": command,
        **_given(multiple=multiple, value=value, moves_view=moves_view),
    }


def color_picker(
    id: str, *, command: Command, value: str = UNSET, moves_view: bool = UNSET
) -> dict:
    """An opaque color, as vizi's own color choices are.  Fires
    ``{"value": "#RRGGBB"}``."""
    return {
        "kind": "color_picker",
        "id": id,
        "command": command,
        **_given(value=value, moves_view=moves_view),
    }


def colormap_picker(
    id: str, *, command: Command, value: str = UNSET, moves_view: bool = UNSET
) -> dict:
    """Every colormap vizi renders, the user's own included.  Fires
    ``{"value": name}``; ``ctx.host.get_colormap(name)`` is its table."""
    return {
        "kind": "colormap_picker",
        "id": id,
        "command": command,
        **_given(value=value, moves_view=moves_view),
    }


def roi_picker(
    id: str,
    *,
    command: Command,
    value: str = UNSET,
    follows_selection: bool = UNSET,
    filter: str = UNSET,
    moves_view: bool = UNSET,
) -> dict:
    """The open dataset's regions.  Fires ``{"value": region_id, "name":
    name}``.  With ``follows_selection`` it shows vizi's selected region, and
    choosing one selects it in vizi; ``filter="mine"`` lists only this
    plugin's regions."""
    return {
        "kind": "roi_picker",
        "id": id,
        "command": command,
        **_given(
            value=value, follows_selection=follows_selection, filter=filter, moves_view=moves_view
        ),
    }


def row(id: str, *children: Any) -> dict:
    return {"kind": "row", "id": id, "children": list(children)}


def column(id: str, *children: Any) -> dict:
    return {"kind": "column", "id": id, "children": list(children)}


def list_rows(
    id: str,
    row: "list[dict]",
    *,
    command: Command = UNSET,
    selectable: bool = UNSET,
    max_rows: int = UNSET,
    visible_rows: int = UNSET,
    moves_view: bool = UNSET,
) -> dict:
    """A ``list``: rows the plugin supplies at runtime with
    ``ctx.panel(...).set_rows(id, keys)``, each built from ``row`` -- leaf
    widgets only, pickers and images included.  Events from a row carry
    ``args["row"]``; with ``selectable`` the list's own ``command`` fires with
    ``{"row": key}``."""
    return {
        "kind": "list",
        "id": id,
        "row": list(row),
        **_given(
            command=command,
            selectable=selectable,
            max_rows=max_rows,
            visible_rows=visible_rows,
            moves_view=moves_view,
        ),
    }


def table_column(
    id: str,
    header: str = UNSET,
    *,
    type: str = UNSET,
    precision: int = UNSET,
    unit: str = UNSET,
) -> dict:
    """One ``table`` column: ``type`` ``"string"`` (the default), ``"number"``
    or ``"bool"``; a number column may give ``precision`` (decimals, default 3)
    and a ``unit`` shown in its header."""
    return {"id": id, **_given(header=header, type=type, precision=precision, unit=unit)}


def table(
    id: str,
    columns: "list[dict]",
    *,
    command: Command = UNSET,
    selectable: bool = UNSET,
    multiple: bool = UNSET,
    visible_rows: int = UNSET,
    sortable: bool = UNSET,
    moves_view: bool = UNSET,
) -> dict:
    """A ``table``: rows of typed cells under ``columns``, set at runtime with
    ``ctx.panel(...).set_table(id, rows)`` or changed by key with
    ``patch_table``; up to 100,000 rows.  With ``selectable`` its ``command``
    fires with ``{"rows": [key, ...]}``, several with ``multiple``; with
    ``sortable`` a header click sorts on vizi's side."""
    return {
        "kind": "table",
        "id": id,
        "columns": list(columns),
        **_given(
            command=command,
            selectable=selectable,
            multiple=multiple,
            visible_rows=visible_rows,
            sortable=sortable,
            moves_view=moves_view,
        ),
    }
