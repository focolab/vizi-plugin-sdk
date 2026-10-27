#!/usr/bin/env python3
"""Check closed-panel state and refusal behavior through the shared host emulator."""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "python"))
from hostsim import DATASET_A, Host, PANEL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", type=Path, default=HERE)
    with Host(parser.parse_args().plugin_dir, DATASET_A) as host:
        host.check_handshake()
        done = host.invoke("movie_seg.threshold", value=" 800 ")
        assert done.ok and done.message == "threshold 800; 0 wanted volume(s) to serve again", done
        for band, name in enumerate(("dim", "mid", "bright"), 1):
            done = host.invoke(f"movie_seg.band_{band}", value=False)
            assert done.ok and done.message == f"band {band} ({name}) hidden", done
        assert host.invoke("movie_seg.band_visible", band=2, visible=True).ok
        host.open_panel(PANEL)
        for widget, value in (("threshold", "800"), ("band_1", "False"),
                              ("band_2", "True"), ("band_3", "False")):
            assert host.await_status(PANEL, value, widget=widget) == value
        print("closed-panel commands: threshold and all three checkboxes retain their state")

        before = len(host.patches)
        for command, args in (
            ("movie_seg.threshold", {"value": "high"}),
            ("movie_seg.threshold", {"value": "NaN"}),
            ("movie_seg.threshold", {"value": "-1"}),
            ("movie_seg.threshold", {"value": True}),
            ("movie_seg.band_visible", {"band": 7, "visible": True}),
            *((f"movie_seg.band_{band}", {"value": "no"}) for band in (1, 2, 3)),
        ):
            done = host.invoke(command, **args)
            assert not done.ok and done.message, (command, args, done)
        # This later panel patch drains after any patches emitted by the refused
        # calls. Wait for its value rather than assuming the reply drains pushes.
        assert host.invoke("movie_seg.threshold", value=" 125.5 ").ok
        assert host.await_status(PANEL, "125.5", widget="threshold") == "125.5"
        assert host.patches[before:] == [(PANEL, "threshold", "125.5")], host.patches[before:]
        assert not host.refusals and not host.violations and not host.unhandled
        assert host.shutdown()
        print("refusals: no panel patches; accepted numeric text shows the formatted value")


if __name__ == "__main__":
    main()
