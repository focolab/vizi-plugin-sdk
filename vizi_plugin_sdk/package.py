"""``--manifest`` and ``--install`` for a declared plugin.

A plugin that declares its interface in code (:mod:`.declare`) answers these
from its own ``serve()``::

    python my_plugin.py --install          # into vizi's plugins directory
    python my_plugin.py --manifest=out/    # just write the manifest

**Installing is in place.**  Python has no single artifact to copy, so the
manifest names the environment the plugin was run in and the plugin's own
source: ``[sys.executable, "-m", <module>]`` for a module importable from that
environment, else ``[sys.executable, <absolute script path>]``.  Move or delete
either and the plugin stops launching -- re-run ``--install``.  A different
layout (a private venv, a relocated copy) starts from ``--manifest`` and is
arranged by hand.

``sys.executable`` is used as it is: resolving a venv's ``python`` symlink
names the base interpreter and loses the venv's ``site-packages``.

The package is written the way the Go SDK's is: a marker says the directory is
this installer's, ``--force`` is needed to write into one that is not, a lock
file keeps two installs apart, every file goes through a temporary and a
rename, and the manifest is written last -- so an install that fails partway
leaves the previous manifest naming a complete package.

Standard library only: neither mode needs grpc or numpy.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import errno
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

from . import declare as _declare
from . import manifest as _manifest

#: The directory the host scans unless ``$VIZI_PLUGINS_DIR`` says otherwise
#: (`manifest_schema.md` §1).
ENV_PLUGINS_DIR = "VIZI_PLUGINS_DIR"
MARKER = ".vizi-python-sdk"
LOCK = ".vizi-install-lock"
#: Set to anything to print the traceback under a failure report.
ENV_TRACE = "VIZI_SDK_TRACE"

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{stem}{n}" for stem in ("com", "lpt") for n in range(1, 10)
}


class PackageError(Exception):
    """``--manifest`` / ``--install`` cannot proceed.  The message is the cause;
    ``operation`` and ``path`` say what was being done where, ``fix`` the steps
    that would get past it, and ``state`` what the failure left behind."""

    def __init__(
        self,
        cause: str,
        *,
        operation: str = "",
        path: "str | os.PathLike | None" = None,
        fix: str = "",
        state: str = "",
    ) -> None:
        super().__init__(cause)
        self.operation = operation
        self.path = str(path) if path is not None else ""
        self.fix = fix
        self.state = state


class UsageError(PackageError):
    """The command line itself is wrong: exit status 2, nothing written."""

    def __init__(self, cause: str, *, fix: str = "") -> None:
        super().__init__(
            cause,
            operation="read the command line",
            fix=fix or "Run with --help for the options.",
            state="Nothing was written.",
        )


@contextlib.contextmanager
def _doing(operation: str, path: "str | os.PathLike", *, state: str = ""):
    """An ``OSError`` inside becomes a :class:`PackageError` naming
    ``operation`` and the path it failed on, in plain words."""
    try:
        yield
    except PackageError:
        raise
    except OSError as exc:
        raise PackageError(
            _plain_os_cause(exc), operation=operation, path=exc.filename or path, state=state
        ) from exc


def _plain_os_cause(exc: OSError) -> str:
    """``permission denied``, not ``[Errno 13] Permission denied: '/x'``."""
    text = exc.strerror or str(exc) or type(exc).__name__
    return text[:1].lower() + text[1:] if text[1:2].islower() else text


# ---------------------------------------------------------------------------
# What the host will run
# ---------------------------------------------------------------------------


def _interpreter() -> str:
    exe = sys.executable
    if not exe:
        raise PackageError(
            "this interpreter does not know its own executable (sys.executable is empty)",
            operation="find this Python interpreter",
            fix="Run the plugin with an ordinary python executable: "
            "python path/to/plugin.py --install",
            state="Nothing was written.",
        )
    head, tail = os.path.split(exe)
    # The host captures stderr (§2.2), which `pythonw` discards.
    if tail.lower() == "pythonw.exe" and os.path.isfile(os.path.join(head, "python.exe")):
        exe = os.path.join(head, "python.exe")
    return os.path.abspath(exe)


def _importable_elsewhere(python: str, module: str) -> bool:
    """Whether ``python -m module`` works from somewhere else -- the plugin
    directory the host launches in -- without this shell's ``PYTHONPATH``.
    Asked of the interpreter itself, because that is what the host will do."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    probe = (
        "import importlib.util, sys\n"
        "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is not None else 3)"
    )
    with tempfile.TemporaryDirectory() as elsewhere:
        try:
            done = subprocess.run(
                [python, "-c", probe, module],
                cwd=elsewhere,
                env=env,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return False
    return done.returncode == 0


def launch_command() -> list[str]:
    """``entry.command`` for this process's plugin, as the host must run it."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    python = _interpreter()
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    # A zipapp, or a pip console-script `.exe` on Windows, runs its archive's
    # `__main__.py` as the module `__main__`, which names no module to `-m`:
    # it is launched by its archive below.
    if spec is not None and spec.name and spec.name != "__main__":
        module = spec.name.removesuffix(".__main__")
        if _importable_elsewhere(python, module):
            return [python, "-m", module]
        raise PackageError(
            "it is importable only from the current directory, and vizi launches plugins "
            "from their own folder",
            operation=f"check that module {module} is importable",
            fix="Install it into this environment, from its project folder:\n"
            "  pip install -e .\n"
            "Or run the plugin's script by its path:\n"
            "  python path/to/plugin.py --install",
            state="Nothing was written.",
        )
    script = getattr(main, "__file__", None)
    if not script:
        raise PackageError(
            "there is no script to launch: this was run from `python -c` or an interactive "
            "session",
            operation="find the plugin's script",
            fix="Run the plugin by its path: python path/to/plugin.py --install",
            state="Nothing was written.",
        )
    script = os.path.abspath(script)
    container = os.path.dirname(script)
    if os.path.isfile(container):
        # A zipapp, or a Windows console-script launcher (a zip behind an .exe):
        # the archive is what the interpreter runs.
        script = container
    return [python, script]


def source_dir() -> Path:
    """Where the plugin's own files are: pane icons are named relative to it."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    script = getattr(sys.modules.get("__main__"), "__file__", None)
    return Path(script).resolve().parent if script else Path.cwd()


def _ephemeral_roots() -> list[Path]:
    home = Path.home()
    roots = [Path(tempfile.gettempdir())]
    local = os.environ.get("LOCALAPPDATA")
    xdg_cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    if os.environ.get("UV_CACHE_DIR"):
        roots.append(Path(os.environ["UV_CACHE_DIR"]))
    roots += [xdg_cache / "uv", home / "Library" / "Caches" / "uv"]
    if local:
        roots.append(Path(local) / "uv" / "cache")
    if os.environ.get("PIPX_HOME"):
        roots.append(Path(os.environ["PIPX_HOME"]) / ".cache")
    roots += [
        home / ".local" / "pipx" / ".cache",
        home / ".local" / "share" / "pipx" / ".cache",
        xdg_cache / "pipx",
        home / "Library" / "Caches" / "pipx",
    ]
    if local:
        roots.append(Path(local) / "pipx" / "pipx" / "Cache")
    return roots


def ephemeral_environment() -> "Path | None":
    """The cache or temporary directory this interpreter's environment lives
    under, if it does -- `uvx`, `pipx run`, `uv run --with` -- else ``None``.
    A manifest naming such an interpreter launches until the cache is pruned."""
    prefix = Path(os.path.realpath(sys.prefix))
    for root in _ephemeral_roots():
        resolved = Path(os.path.realpath(root))
        if prefix.is_relative_to(resolved):
            return resolved
    return None


def manifest_data() -> dict:
    """This plugin's manifest, validated by the host's own parser against the
    plugin's source directory (where its pane icons are)."""
    data = _declare.build(launch_command())
    try:
        as_json = json.loads(json.dumps(data))
    except (TypeError, ValueError) as exc:
        raise _declare.DeclarationError(f"the declaration is not JSON data: {exc}") from None
    _manifest.parse_manifest(as_json, source_dir=source_dir())
    return as_json


def _encode(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _plain_parents(root: Path, relative: str) -> Path:
    """``root / relative`` with every directory between made and checked to be
    a plain directory, so an existing symlink cannot redirect a write."""
    if not _manifest.is_contained_relative(relative):
        raise PackageError(
            f"{relative!r} is not a path inside the package",
            operation="place a package file",
            path=root,
            fix="Name the file by a relative path inside the plugin's folder.",
        )
    parts = Path(relative).parts
    here = root
    for part in parts[:-1]:
        here = here / part
        if here.is_symlink() or (here.exists() and not here.is_dir()):
            raise _not_plain(here, "a directory")
        with _doing("create directory", here):
            here.mkdir(exist_ok=True)
    return here / parts[-1]


def _not_plain(path: Path, wanted: str) -> PackageError:
    kind = "a symbolic link" if path.is_symlink() else "something else"
    return PackageError(
        f"it is {kind}, and {wanted} is needed there",
        operation="check",
        path=path,
        fix=f"Move or remove {path}, then run the command again; or choose another "
        "folder with --install=DIR.",
    )


def _write_file(root: Path, relative: str, data: bytes) -> None:
    """Write through a temporary and a rename, never truncating the file in
    place; an identical file is left alone."""
    dest = _plain_parents(root, relative)
    if dest.is_symlink() or (dest.exists() and not dest.is_file()):
        raise _not_plain(dest, "a regular file")
    with _doing("write", dest):
        if dest.exists() and dest.read_bytes() == data:
            return
        temp = dest.with_name(f".vizi-write-{secrets.token_hex(8)}")
        try:
            with open(temp, "xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, dest)
        finally:
            if temp.exists():
                temp.unlink()


def _write_package(target: Path, data: dict, source: Path, *, marker: bytes | None) -> None:
    for icon in _declare.pane_icons(data):
        src = source / icon
        dest = target / icon
        if dest.exists() and os.path.samefile(src, dest):
            continue
        with _doing("read icon", src):
            icon_bytes = src.read_bytes()
        _write_file(target, icon, icon_bytes)
    if marker is not None:
        _write_file(target, MARKER, marker)
    _write_file(target, _manifest.MANIFEST_FILENAME, _encode(data))


def write_manifest(directory: "str | os.PathLike | None" = None) -> Path:
    """Write the manifest, and the pane icons it names, into ``directory``
    (default: the working directory).  Returns the manifest's path."""
    target = Path(directory or ".").absolute()
    data = manifest_data()
    with _doing("create directory", target):
        target.mkdir(parents=True, exist_ok=True)
    _write_package(target, data, source_dir(), marker=None)
    return target / _manifest.MANIFEST_FILENAME


def plugins_dir() -> Path:
    return Path(os.environ.get(ENV_PLUGINS_DIR) or Path.home() / ".vizi" / "plugins")


def _check_portable_name(plugin_id: str) -> None:
    bad = set('<>:"/\\|?*')
    if (
        not plugin_id.strip()
        or plugin_id in (".", "..")
        or any(c in bad or ord(c) < 32 for c in plugin_id)
        or plugin_id[-1] in ". "
        or plugin_id.split(".")[0].lower() in _WINDOWS_RESERVED
    ):
        raise PackageError(
            f"the id {plugin_id!r} is not one directory name on every system, which "
            "--install needs",
            operation="name the package folder",
            fix="Change the id in plugin(id=...) to letters, digits, dots, dashes and "
            "underscores; or write the manifest with --manifest=DIR and place the "
            "package by hand.",
            state="Nothing was written.",
        )


def install(directory: "str | os.PathLike | None" = None, *, force: bool = False) -> Path:
    """Install this plugin into ``directory`` (default ``$VIZI_PLUGINS_DIR``
    or ``~/.vizi/plugins``), under a directory named for its id.  Returns
    that directory.  ``force`` allows an ephemeral interpreter and writing
    into a directory this installer did not create (its other files are kept).
    """
    ephemeral = ephemeral_environment()
    if ephemeral is not None and not force:
        raise PackageError(
            f"it is under {ephemeral}, a cache or temporary directory, and vizi could not "
            "launch the plugin once that is cleaned out",
            operation="check this interpreter's environment",
            path=sys.prefix,
            fix="Install from a lasting environment (a virtual environment, or your "
            "system Python), with that environment's python:\n"
            "  python path/to/plugin.py --install\n"
            "Or install from this one anyway, knowing it may stop launching:\n"
            + _indented(_rerun("--force")),
            state="Nothing was written.",
        )
    if os.environ.get("PYTHONPATH"):
        print(
            "warning: PYTHONPATH is set, and the host will not have it when it launches "
            "the plugin",
            file=sys.stderr,
        )
    data = manifest_data()
    plugin_id = data["id"]
    _check_portable_name(plugin_id)
    root = Path(directory).absolute() if directory else plugins_dir().absolute()
    with _doing("create the plugins folder", root, state="Nothing was written."):
        root.mkdir(parents=True, exist_ok=True)
    target = root / plugin_id
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise _not_plain(target, "a directory")
    with _doing("create the package folder", target, state="Nothing was written."):
        target.mkdir(exist_ok=True)
    lock = target / LOCK
    try:
        with _doing("take the install lock", lock, state="Nothing was written."):
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except PackageError as exc:
        if not isinstance(exc.__cause__, FileExistsError):
            raise
        raise PackageError(
            "the lock file is there: another install of this plugin is running, or an "
            "earlier one was interrupted",
            operation="take the install lock",
            path=lock,
            fix="If no other install is running, remove the lock and run the command again:\n"
            + _indented(_removal(lock)),
            state="Nothing was written, and the lock was left in place.",
        ) from None
    try:
        marker = f"vizi-python-sdk\n{plugin_id}\n".encode()
        with _doing("read the package folder", target):
            others = [p for p in target.iterdir() if p.name != LOCK]
            mine = (target / MARKER).is_file() and (target / MARKER).read_bytes() == marker
        if others and not mine and not force:
            raise PackageError(
                f"it has files in it and this installer did not create it (no {MARKER} " "marker)",
                operation="check the package folder",
                path=target,
                fix="To write into it anyway (its other files are kept):\n"
                + _indented(_rerun("--force"))
                + "\nOr install into another folder with --install=DIR.",
                state="Nothing was written.",
            )
        _write_package(target, data, source_dir(), marker=marker)
    finally:
        lock.unlink(missing_ok=True)
    _manifest.load_manifest(target)
    return target


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

_MODES = ("--install", "--manifest")
#: ``--install`` / ``--manifest`` with no value: the default folder.
_DEFAULT_FOLDER = object()


def _prog() -> str:
    return os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "plugin"


def _shells(windows: "bool | None" = None) -> "tuple[str, ...]":
    """The shells a fix gives its commands for: sh, or on Windows PowerShell
    and cmd -- where a path has backslashes and sh's quoting means nothing."""
    if windows is None:
        windows = os.name == "nt"
    return ("powershell", "cmd") if windows else ("sh",)


_PLAIN_ARG = re.compile(r"[A-Za-z0-9_@%+=:,./\\-]+")


def _quote(arg: str, shell: str) -> str:
    """``arg`` as ``shell`` must be given it."""
    if shell == "sh":
        return shlex.quote(arg)
    if _PLAIN_ARG.fullmatch(arg):
        return arg
    if shell == "powershell":
        return "'" + arg.replace("'", "''") + "'"
    return subprocess.list2cmdline([arg]) if " " in arg or "\t" in arg else f'"{arg}"'


_SHELL_LABEL = {"powershell": "PowerShell:  ", "cmd": "cmd:         "}


def _command(argv: "list[str]", windows: "bool | None" = None) -> "list[str]":
    """``argv`` as lines to type: one for sh, or one per Windows shell."""
    lines = []
    for shell in _shells(windows):
        words = " ".join(_quote(a, shell) for a in argv)
        if shell == "powershell":
            words = f"& {words}"  # a quoted program is a string to PowerShell without it
        lines.append(_SHELL_LABEL.get(shell, "") + words)
    return lines


def _removal(path: Path, windows: "bool | None" = None) -> "list[str]":
    """The command that removes the file ``path``, per shell."""
    verbs = {"sh": "rm", "powershell": "Remove-Item -LiteralPath", "cmd": "del"}
    return [
        _SHELL_LABEL.get(shell, "") + f"{verbs[shell]} {_quote(str(path), shell)}"
        for shell in _shells(windows)
    ]


def _indented(lines: "list[str]") -> str:
    return "\n".join(f"  {line}" for line in lines)


def _rerun(*extra: str) -> "list[str]":
    """This run's own command line with ``extra`` added, as a fix gives it."""
    argv = [a for a in sys.argv[1:] if a not in extra]
    script = sys.argv[0] if sys.argv and sys.argv[0] else "path/to/plugin.py"
    return _command([sys.executable or "python", script, *argv, *extra])


def _mode(argv: "list[str]") -> "str | None":
    for arg in argv:
        flag = arg.split("=", 1)[0]
        if flag in _MODES:
            return flag
    return None


def _folder(argv: "list[str]", flag: str) -> "str | None":
    """The folder given to ``flag`` in ``argv``, ``""`` for an empty one, or
    ``None`` for none."""
    for i, arg in enumerate(argv):
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
        if arg == flag:
            following = argv[i + 1] if i + 1 < len(argv) else None
            return following if following is not None and not following.startswith("-") else None
    return None


def _destination(mode: "str | None", argv: "list[str]") -> "Path | None":
    """Where ``mode`` was writing, as the report's headline names it."""
    if mode is None:
        return None
    folder = _folder(argv, mode)
    identity = _declare.requested_identity()
    if mode == "--manifest":
        return Path(folder or ".").absolute() / _manifest.MANIFEST_FILENAME
    root = Path(folder).absolute() if folder else plugins_dir().absolute()
    return root / identity[0] if identity is not None else root


def _fix_for(exc: PackageError, mode: "str | None") -> str:
    if exc.fix:
        return exc.fix
    flag = f"{mode}=DIR" if mode else "--install=DIR"
    cause = exc.__cause__
    if isinstance(cause, OSError) and cause.errno in (errno.EACCES, errno.EPERM):
        where = Path(exc.path) if exc.path else Path.cwd()
        while not where.is_dir() and where.parent != where:
            where = where.parent
        return (
            f"Check the permissions of {where}: writing there was refused.  Or choose a "
            f"folder you can write to with {flag}."
        )
    return (
        f"Check the path and the free disk space, then run the command again; or choose "
        f"another folder with {flag}."
    )


def failure_report(exc: BaseException, argv: "list[str]") -> str:
    """The text ``--install`` / ``--manifest`` print when ``exc`` stops them:
    a headline naming the plugin and where it was going, ``Why`` in plain
    words, what was left, and ``To fix``."""
    mode = _mode(argv)
    identity = _declare.requested_identity()
    if isinstance(exc, PackageError):
        failure = exc
    elif isinstance(exc, (_declare.DeclarationError, _manifest.ManifestError)):
        failure = PackageError(
            str(exc),
            operation="check the plugin's declaration",
            fix="Correct the declaration in the plugin's source -- plugin(...) and its "
            "decorators -- then run the command again.",
            state="Nothing was written.",
        )
    elif isinstance(exc, OSError):
        failure = PackageError(_plain_os_cause(exc), operation="write", path=exc.filename)
        failure.__cause__ = exc
    else:
        failure = PackageError(
            f"{type(exc).__name__}: {' '.join(str(exc).split())}",
            operation="run " + (mode or "the command").lstrip("-"),
            fix=f"This is a bug in the plugin or in the Python SDK.  Run again with "
            f"{ENV_TRACE}=1 for the traceback, and report it.",
        )
    word = (mode or "--command").lstrip("-").upper()
    headline = f"{word} FAILED:"
    headline += f" {identity[0]} {identity[1]}" if identity is not None else f" {_prog()}"
    destination = None if isinstance(failure, UsageError) else _destination(mode, argv)
    if destination is not None:
        headline += f" was not {'installed' if mode == '--install' else 'written'} at {destination}"
    where = f"{failure.operation} {failure.path}".strip() if failure.path else failure.operation
    why = f"Why: {where}: {failure}" if where else f"Why: {failure}"
    state = failure.state or (
        "The manifest was not replaced, so a previous install still names a complete package; "
        "files written before the failure may remain."
        if mode == "--install"
        else "The manifest was not replaced."
    )
    return f"{headline}\n{why}\n{state}\n\nTo fix:\n{_fix_for(failure, mode)}\n"


def _report(exc: BaseException, argv: "list[str]") -> None:
    sys.stderr.write(failure_report(exc, argv))
    if os.environ.get(ENV_TRACE):
        traceback.print_exception(exc)
    sys.stderr.flush()


def report_uncaught_failures(argv: "list[str] | None" = None) -> None:
    """For an ``--install`` / ``--manifest`` run, report an exception nothing
    caught -- a declaration refused as the plugin's module is imported, before
    ``serve()`` -- as a failure report instead of a traceback.  Called by
    :func:`plugin`; does nothing for a run serving a session."""
    argv = sys.argv[1:] if argv is None else argv
    if _mode(argv) is None or getattr(sys.excepthook, "_vizi_sdk", False):
        return
    previous = sys.excepthook

    def hook(kind, exc, tb):
        if issubclass(kind, (KeyboardInterrupt, SystemExit)):
            previous(kind, exc, tb)
            return
        _report(exc.with_traceback(tb), argv)

    hook._vizi_sdk = True  # type: ignore[attr-defined]
    sys.excepthook = hook


class _Parser(argparse.ArgumentParser):
    argv: "list[str]" = []

    def error(self, message: str):  # argparse's exit becomes the report
        known = [*_MODES, "--force", "--help"]
        typed = [a.split("=", 1)[0] for a in self.argv if a.startswith("--")]
        for word in typed:
            near = difflib.get_close_matches(word, known, n=1, cutoff=0.6)
            if word not in known and near:
                raise UsageError(
                    message,
                    fix=f"Write {word} as {near[0]}; options are not abbreviated.  "
                    "Run with --help for the options.",
                )
        raise UsageError(message)


def dispatch(argv: "list[str]") -> "int | None":
    """Handle ``argv`` for ``serve()``: ``None`` means serve a host session,
    an ``int`` is the exit status of a ``--manifest`` / ``--install`` run.

    Only a DECLARED plugin's arguments are read.  A hand-written manifest may
    pass a plugin arguments of its own, so an undeclared plugin's are left
    alone -- except the two this module would have answered, which say why
    they cannot be."""
    if not _declare.declared():
        if argv and argv[0].split("=", 1)[0] in _MODES:
            _report(
                UsageError(
                    f"{argv[0].split('=', 1)[0]} needs the plugin's interface declared in code",
                    fix="Declare it in the plugin's source, before serve():\n"
                    '  plugin(id="org.example.name", name="Name", version="1.0")',
                ),
                argv,
            )
            return 2
        return None
    if not argv:
        return None
    parser = _Parser(
        prog=_prog(),
        description="With no arguments, serve a session for vizi (which launches this).",
        allow_abbrev=False,
    )
    parser.argv = argv
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--install",
        nargs="?",
        const=_DEFAULT_FOLDER,
        metavar="DIR",
        help=f"install into vizi's plugins folder (default ${ENV_PLUGINS_DIR} "
        "or ~/.vizi/plugins), in place: the manifest runs this interpreter and this source",
    )
    modes.add_argument(
        "--manifest",
        nargs="?",
        const=_DEFAULT_FOLDER,
        metavar="DIR",
        help="write vizi-plugin.json and its icons to DIR (default: the working directory)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --install: allow an ephemeral interpreter, or a folder this "
        "installer did not create",
    )
    try:
        args = parser.parse_args(argv)
        if args.install is None and args.manifest is None:
            raise UsageError(
                "--force applies only to --install",
                fix="Add --install:\n" + _indented(_rerun("--install")),
            )
        if args.force and args.install is None:
            raise UsageError("--force applies only to --install", fix="Leave out --force.")
        flag, folder = (
            ("--install", args.install)
            if args.install is not None
            else ("--manifest", args.manifest)
        )
        if folder == "":
            default = plugins_dir().absolute() if flag == "--install" else Path.cwd()
            raise UsageError(
                f"the folder given to {flag} is empty (an unset variable, as in "
                f'{flag}="$DIR"?)',
                fix=f"Name the folder: {flag}=DIR\n"
                f"Or leave the value off to use the default, {default}: {flag}",
            )
        folder = None if folder is _DEFAULT_FOLDER else folder
        if flag == "--install":
            written = install(folder, force=args.force)
            launch = shlex.join(_manifest.load_manifest(written).command)
        else:
            written = write_manifest(folder)
    except UsageError as exc:
        _report(exc, argv)
        print(f"\nUsage: {parser.format_usage().removeprefix('usage: ')}", end="", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 -- every failure is reported, none is a traceback
        _report(exc, argv)
        return 1
    plugin_id, version = _declare.identity()
    if flag == "--install":
        print(f"INSTALLED: {plugin_id} {version} at {written}")
        print(f"Launch: {launch}")
    else:
        print(f"MANIFEST WRITTEN: {plugin_id} {version} at {written}")
    return 0


def stale_manifest_warning() -> "str | None":
    """For a declared plugin serving a session: a warning when the manifest in
    the working directory (the one the host read) no longer matches the code's
    declaration outside ``entry``, else ``None``."""
    try:
        on_disk = json.loads(Path(_manifest.MANIFEST_FILENAME).read_text(encoding="utf-8"))
        declared = _declare.build(["-"])
    except (OSError, ValueError, _declare.DeclarationError):
        return None
    if not isinstance(on_disk, dict):
        return None
    on_disk.pop("entry", None)
    declared.pop("entry", None)
    if on_disk == declared:
        return None
    return (
        f"warning: {_manifest.MANIFEST_FILENAME} here does not match this plugin's declared "
        "interface; the host is using the file.  Re-run --install to regenerate it."
    )
