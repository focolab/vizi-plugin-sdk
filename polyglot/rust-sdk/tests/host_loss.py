"""Linux process check: SIGKILL the emulator, then reap the plugin's own exit.

From the repository root, build the command_plugin example and run this with
the repository .venv/bin/python. Requires the shared emulator's dependencies
and local socket permissions. No Shutdown or signal is sent to the plugin
unless the check fails and needs cleanup.
"""

import ctypes
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
SDK = ROOT / "polyglot" / "rust-sdk"


def emulate(plugin_dir):
    sys.path.insert(0, str(ROOT / "examples"))
    import numpy as np
    from vizi_hostsim import Dataset, Host, Segment, pb2

    data = np.zeros((1, 1, 1), dtype=np.uint8)
    dataset = Dataset((1, 1, 1, 1, 1), data.dtype, pb2.DTYPE_U8, 1,
                      lambda _t, _c: data)
    host = Host(plugin_dir, dataset)
    try:
        host.__enter__()
        # Report ownership before the checks, so the observer can always reap
        # the plugin if a handshake or grant assertion fails.
        print(json.dumps({"pid": host._plugin.pid, "directory": str(host.directory)}), flush=True)
        host.check_handshake()
        host.add(Segment.plugin_arena(1, "P", 4096, host.directory, subject="status"))
        assert host.grant(1).WhichOneof("outcome") == "installed"
        assert host.invoke("hello").ok
        print("ready", flush=True)
        # Deliberately avoid Host.__exit__: it sends Shutdown and terminates
        # the plugin, which would conceal precisely the bug being tested.
        while True:
            signal.pause()
    except BaseException:
        if host._plugin is not None and host._plugin.poll() is None:
            host._plugin.kill()
            host._plugin.wait()
        host._tempdir.cleanup()
        raise


def line(process):
    if not select.select([process.stdout], [], [], 35)[0]:
        raise AssertionError("emulator startup timed out")
    value = process.stdout.readline()
    if not value:
        raise AssertionError("emulator exited before readiness")
    return value


def main():
    if sys.platform != "linux":
        raise SystemExit("This process-reaping check requires Linux.")
    binary = SDK / "target" / "debug" / "examples" / "command_plugin"
    if not binary.is_file():
        raise SystemExit("Build first: cargo build --locked --manifest-path polyglot/rust-sdk/Cargo.toml --example command_plugin")
    # Reparent the orphan to this observer, not PID 1, so waitpid proves an
    # actual exit and reaps it; kill(pid, 0) would also accept a zombie as alive.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
    with tempfile.TemporaryDirectory(prefix="vizi-rust-host-loss-") as temporary:
        directory = Path(temporary)
        subprocess.run([str(binary), "--manifest", str(directory)], check=True)
        manifest = json.loads((directory / "vizi-plugin.json").read_text())
        # The launcher execs the plugin in its own session, matching the host's
        # Unix process-tree rule. Killing the emulator cannot signal this group.
        manifest["entry"]["command"] = [sys.executable, str(Path(__file__).resolve()), "--plugin", str(binary)]
        (directory / "vizi-plugin.json").write_text(json.dumps(manifest))
        plugin_pid = None
        host_directory = None
        with (directory / "stderr.log").open("w+") as log:
            emulator = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--host", str(directory)],
                stdout=subprocess.PIPE, stderr=log, bufsize=0,
            )
            try:
                info = json.loads(line(emulator))
                plugin_pid = info["pid"]
                host_directory = Path(info["directory"])
                assert line(emulator).strip() == b"ready"
                assert os.getsid(plugin_pid) == plugin_pid
                started = time.monotonic()
                emulator.kill()  # only the emulator PID; no process-group kill
                assert emulator.wait(timeout=5) == -signal.SIGKILL
                while time.monotonic() - started < 3:
                    pid, status = os.waitpid(plugin_pid, os.WNOHANG)
                    if pid:
                        plugin_pid = None
                        assert os.waitstatus_to_exitcode(status) == 0, status
                        print(f"Plugin exited on host loss in {time.monotonic() - started:.3f}s (mapped grant, no Shutdown).")
                        break
                    time.sleep(0.005)
                else:
                    raise AssertionError("plugin remained alive three seconds after host death")
            finally:
                if emulator.poll() is None:
                    emulator.kill()
                    emulator.wait()
                if plugin_pid is not None:
                    try:
                        os.kill(plugin_pid, signal.SIGKILL)
                        os.waitpid(plugin_pid, 0)
                    except ProcessLookupError:
                        pass
                    except ChildProcessError:
                        pass  # startup failure was reaped by the emulator
                emulator.stdout.close()
                if host_directory is not None:
                    shutil.rmtree(host_directory, ignore_errors=True)
                log.seek(0)
                sys.stderr.write(log.read())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--plugin":
        os.setsid()
        os.execv(sys.argv[2], [sys.argv[2]])
    elif len(sys.argv) > 1 and sys.argv[1] == "--host":
        emulate(Path(sys.argv[2]))
    else:
        main()
