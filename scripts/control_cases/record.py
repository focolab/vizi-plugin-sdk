"""Record every HostMsg and PluginMsg the emulator's drivers put on the wire.

    python -I record.py --all <out.jsonl>
    python -I record.py <out.jsonl> <script.py> [args...]

``--all`` runs the emulator self-check (``examples/vizi_hostsim/selfcheck.py``)
and every example driver (``examples/*/python/hostsim.py``), each in its own
process, appending to ``<out.jsonl>``.  It writes nothing else, and it prints
one line per driver with its exit status; a driver that fails makes the run
fail.

The second form records one script.  ``PluginStub`` is wrapped so that the
Session stream's request iterator (HostMsg, host->plugin) and response iterator
(PluginMsg, plugin->host) are both teed, as serialized bytes with the arm name,
to ``<out.jsonl>``.  The script runs unmodified under ``runpy``, and nothing
here changes the SDK's behavior.

Each line holds ``script``, ``session``, ``seq`` (a per-process counter),
``direction``, ``kind``, ``id`` and ``b64``.
"""

from __future__ import annotations

import base64
import json
import os
import runpy
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def scripts() -> list[Path]:
    return [ROOT / "examples/vizi_hostsim/selfcheck.py", *sorted(ROOT.glob("examples/*/python/hostsim.py"))]


def record_all(out_path: str) -> int:
    out = Path(out_path).resolve()
    out.unlink(missing_ok=True)
    failed = 0
    with tempfile.TemporaryDirectory(prefix="vizi-control-cases-") as scratch:
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        env.update(
            QT_QPA_PLATFORM="offscreen",
            VIZI_SETTINGS_INI_DIR=f"{scratch}/ini",
            XDG_CONFIG_HOME=f"{scratch}/cfg",
            XDG_CACHE_HOME=f"{scratch}/cache",
            XDG_DATA_HOME=f"{scratch}/data",
            VIZI_PLUGINS_DIR=f"{scratch}/plugins",
            VIZI_HOSTSIM_PYTHON=sys.executable,
            PYTHONDONTWRITEBYTECODE="1",
        )
        for d in ("ini", "cfg", "cache", "data", "plugins"):
            os.makedirs(f"{scratch}/{d}", exist_ok=True)
        for script in scripts():
            rel = script.relative_to(ROOT)
            log = Path(scratch) / (str(rel).replace("/", "_") + ".log")
            with open(log, "w") as fh:
                try:
                    rc = subprocess.run(
                        [sys.executable, "-I", "-B", __file__, str(out), str(script)],
                        cwd=script.parent,
                        env=env,
                        stdout=fh,
                        stderr=subprocess.STDOUT,
                        timeout=600,
                    ).returncode
                except subprocess.TimeoutExpired:
                    rc = "timeout"
            print(f"record: {rel} rc={rc}", flush=True)
            if rc != 0:
                failed += 1
                sys.stdout.write(log.read_text()[-4000:])
    if failed:
        return 1
    with open(out, "a") as fh:
        fh.write(json.dumps({"meta": tree_state()}) + "\n")
    return 0


def tree_state() -> dict:
    """Whether the files that shape the recording (the SDK, the examples,
    the proto) were clean in git when it was made.  No commit is kept: the
    recording ships, and a development commit names nothing a reader has."""

    def git(*args):
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True).stdout.strip()

    dirty = git("status", "--porcelain", "--", "vizi_plugin_sdk", "examples", "proto")
    return {"sdk_and_examples_clean": not dirty}


def record_one(out_path: str, script: str, args: list[str]) -> None:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "examples"))
    sys.path.insert(0, str(Path(script).resolve().parent))

    from vizi_plugin_sdk import pb2_grpc

    lock = threading.Lock()
    out = open(out_path, "a")
    counter = {"session": 0, "seq": 0}
    rel = str(Path(script).resolve().relative_to(ROOT))

    def rec(direction, msg, session):
        with lock:
            counter["seq"] += 1
            line = {
                "script": rel,
                "session": session,
                "seq": counter["seq"],
                "direction": direction,
                "kind": msg.WhichOneof("msg"),
                "id": msg.id,
                "b64": base64.b64encode(msg.SerializeToString()).decode(),
            }
            out.write(json.dumps(line) + "\n")
            out.flush()

    original = pb2_grpc.PluginStub

    class RecordingStub:
        def __init__(self, channel):
            self._inner = original(channel)

        def Session(self, requests, *a, **kw):
            with lock:
                counter["session"] += 1
                p = Path(script)
                session = f"{p.parent.parent.name}/{p.parent.name}#{counter['session']}"

            def tee_requests():
                for m in requests:
                    rec("host->plugin", m, session)
                    yield m

            responses = self._inner.Session(tee_requests(), *a, **kw)

            class Tee:
                def __iter__(self_inner):
                    return self_inner

                def __next__(self_inner):
                    m = next(responses)
                    rec("plugin->host", m, session)
                    return m

                def __getattr__(self_inner, name):
                    return getattr(responses, name)

            return Tee()

    pb2_grpc.PluginStub = RecordingStub
    sys.argv = [script] + args
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        if e.code not in (None, 0):
            print(f"record: {script} exited {e.code}", file=sys.stderr)
            raise


if __name__ == "__main__":
    if sys.argv[1:2] == ["--all"]:
        sys.exit(record_all(sys.argv[2]))
    record_one(sys.argv[1], sys.argv[2], sys.argv[3:])
