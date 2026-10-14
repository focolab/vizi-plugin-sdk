#!/usr/bin/env python3
"""The emulator's tool fixture (§8.15): a tool that declares
``view_targets``, so each FIRE is a cause the plugin answers with
``ToolContext.move_view``.  ``aim.next`` says what the next FIRE asks; the
answer is read back with ``aim.got`` (not a panel label: an unopened
panel's label is held until it opens).  A
separate fixture because the main one declares no tool."""

from __future__ import annotations

import json
import threading

from vizi_plugin_sdk import CommandContext, ToolContext, command, serve, tool

_NEXT = {"how": "t", "t": 0}
_GOT: dict[int, list] = {}
_GOT_CHANGED = threading.Condition()


@command("aim.next")
def next_fire(ctx: CommandContext) -> str:
    _NEXT.clear()
    _NEXT.update(ctx.args)
    return "armed"


@command("aim.got")
def got(ctx: CommandContext) -> str:
    """The answer to FIRE ``n``, waiting for it up to 3 s."""
    n = int(ctx.args["n"])
    with _GOT_CHANGED:
        _GOT_CHANGED.wait_for(lambda: n in _GOT, timeout=3.0)
        return json.dumps(_GOT.get(n))


@tool("aim")
class Aim:
    def fire(self, ctx: ToolContext, ev) -> None:
        ask = dict(_NEXT)

        def answer() -> None:  # never from the doorbell listener
            if ask.get("how") == "look":
                res = ctx.move_view(ev, look_at=(1.0, 2.0, 3.0))
            elif ask.get("how") == "decline":
                res = ctx.decline_view(ev)
            else:
                res = ctx.move_view(ev, t=int(ask.get("t", 0)))
            with _GOT_CHANGED:
                _GOT[int(ask.get("n", 0))] = [res.outcome, res.reason]
                _GOT_CHANGED.notify_all()

        threading.Thread(target=answer, daemon=True).start()


if __name__ == "__main__":
    serve()
