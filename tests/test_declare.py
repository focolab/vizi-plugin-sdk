"""Declaring the interface in code, and ``--manifest`` / ``--install``.

A declared plugin's manifest is built from its decorators and checked by the
host's own parser before anything is written; ``--install`` writes it in place
-- this interpreter, this source -- with the Go SDK's package posture (marker,
lock, temp-and-rename, manifest last).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

import vizi_plugin_sdk
from vizi_plugin_sdk import declare, manifest, package, server, widgets
from vizi_plugin_sdk.declare import DeclarationError
from vizi_plugin_sdk.package import PackageError

#: The name this suite imports the SDK under -- ``vizi_plugin_sdk`` here, or
#: the name another tree gives the same package -- and the directory that
#: makes it importable.  A child process must load THIS copy: another
#: ``vizi_plugin_sdk`` may be importable beside it.
_SDK = vizi_plugin_sdk.__name__
_SDK_ROOT = str(Path(vizi_plugin_sdk.__file__).resolve().parents[len(_SDK.split("."))])

_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'


class _Declared(unittest.TestCase):
    """Each test declares into a fresh, empty declaration."""

    def setUp(self) -> None:
        fresh = declare._fresh()
        fresh.__enter__()
        self.addCleanup(fresh.__exit__, None, None, None)
        for registry in (
            server._REGISTERED,
            server._COMMANDS,
            server._EXPORTERS,
            server._TOOLS,
            server._PANEL_OPENED,
            server._CHUNK_SOURCES,
            server._STAGES,
            server._STAGE_PARAMS,
            server._SHAPERS,
        ):
            saved = dict(registry)
            self.addCleanup(lambda r=registry, s=saved: (r.clear(), r.update(s)))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.source = self.tmp / "src"
        self.source.mkdir()
        (self.source / "icons").mkdir()
        (self.source / "icons" / "a.svg").write_bytes(_SVG)
        patches = (
            mock.patch.object(package, "source_dir", return_value=self.source),
            mock.patch.object(package, "launch_command", return_value=["py", "plugin.py"]),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def declare_hello(self, **plugin_kwargs) -> None:
        declare.plugin(id="org.test.hello", name="Hello", version="1.2.3", **plugin_kwargs)

        @server.command("say", pane=declare.pane("icons/a.svg"))
        def say(ctx):
            return "hi"

        declare.panel(
            "main",
            title="Main",
            root=widgets.column("root", widgets.button("go", "Go", command=say)),
        )


class Building(_Declared):
    def test_an_undeclared_plugin_has_no_manifest_to_build(self):
        with self.assertRaisesRegex(DeclarationError, r"call plugin\("):
            declare.build(["py"])
        self.assertFalse(declare.declared())

    def test_a_plugin_may_declare_an_older_engines_floor_but_not_a_newer_one(self):
        """`engines.vizi` was always this SDK's newest minor, so
        a host one minor older refused a plugin that used nothing newer."""
        major, minor = manifest.HOST_API_MAJOR, manifest.HOST_API_MINOR
        declare.plugin(id="org.test.x", name="X", version="1", engines=f"{major}.0")
        self.assertEqual(declare.build(["py"])["engines"], {"vizi": f"{major}.0"})
        for wrong in (f"{major}.{minor + 1}", f"{major + 1}.0", "one", "1"):
            with self.subTest(engines=wrong):
                with self.assertRaises(declare.DeclarationError):
                    declare.plugin(id="org.test.x", name="X", version="1", engines=wrong)

    def test_the_manifest_states_the_sdk_release_and_min_vizi(self):
        """min_vizi is 1.0.0 unless the author says another, as the Go, Rust and
        Java SDKs write it: the first host that speaks protocol 1.0."""
        declare.plugin(id="org.test.x", name="X", version="1")
        data = declare.build(["py"])
        self.assertEqual(data["sdk_version"], vizi_plugin_sdk.__version__)
        self.assertEqual(data["min_vizi"], "1.0.0")
        self.assertEqual(manifest.parse_manifest(data).sdk_version, vizi_plugin_sdk.__version__)
        declare.plugin(id="org.test.x", name="X", version="1", min_vizi="0.9.7")
        self.assertEqual(declare.build(["py"])["min_vizi"], "0.9.7")

    def test_only_what_the_author_wrote_is_emitted(self):
        declare.plugin(id="org.test.x", name="X", version="1")

        @server.command("hidden", menu=None)
        def hidden(ctx):
            pass

        data = declare.build(["py", "x.py"])
        self.assertEqual(data["manifest_version"], 1)
        self.assertEqual(
            data["engines"], {"vizi": f"{manifest.HOST_API_MAJOR}.{manifest.HOST_API_MINOR}"}
        )
        self.assertEqual(data["entry"], {"command": ["py", "x.py"]})
        (cmd,) = data["contributions"]["commands"]
        self.assertEqual(cmd, {"id": "hidden", "title": "hidden", "menu": None})
        self.assertNotIn("permissions", data)
        self.assertNotIn("assets", data)
        parsed = manifest.parse_manifest(data)
        self.assertEqual(parsed.commands[0].menu, None)

    def test_a_slow_starting_plugin_declares_its_startup_time(self):
        """``entry.startup_s``: advisory, checked by the host's own parser."""
        declare.plugin(id="org.test.x", name="X", version="1", startup_s=90)
        data = declare.build(["py", "x.py"])
        self.assertEqual(data["entry"], {"command": ["py", "x.py"], "startup_s": 90})
        self.assertEqual(manifest.parse_manifest(data).startup_s, 90.0)
        declare.plugin(id="org.test.x", name="X", version="1", startup_s=0)
        with self.assertRaisesRegex(manifest.ManifestError, "startup_s"):
            manifest.parse_manifest(declare.build(["py", "x.py"]))

    def test_a_widget_binds_the_handler_itself_or_its_id(self):
        self.declare_hello()
        root = declare.build(["py"])["contributions"]["panels"][0]["root"]
        self.assertEqual(root["children"][0]["command"], "say")

        declare.panel("other", title="O", root=widgets.button("b", command="say"))
        panels = declare.build(["py"])["contributions"]["panels"]
        self.assertEqual(panels[1]["root"]["command"], "say")

    def test_binding_a_function_that_is_no_command_is_refused_by_name(self):
        declare.plugin(id="org.test.x", name="X", version="1")

        def not_a_command(ctx):
            pass

        declare.panel("p", title="P", root=widgets.button("b", command=not_a_command))
        with self.assertRaisesRegex(DeclarationError, "not_a_command"):
            declare.build(["py"])

    def test_a_panel_opened_handler_needs_a_declared_panel(self):
        declare.plugin(id="org.test.x", name="X", version="1")

        @server.panel_opened("ghost")
        def opened(ctx):
            pass

        with self.assertRaisesRegex(DeclarationError, "ghost"):
            declare.build(["py"])

    def test_a_chunk_source_declares_its_overlay_chunked(self):
        declare.plugin(id="org.test.x", name="X", version="1")
        declare.overlay("seg", name="Segmentation")

        @server.chunk_source("seg", time="static")
        def serve_seg(ctx, want):
            return None

        (overlay,) = declare.build(["py"])["contributions"]["overlays"]
        self.assertEqual(
            overlay, {"id": "seg", "name": "Segmentation", "chunked": True, "time": "static"}
        )

    def test_every_kind_round_trips_through_the_reference_parser(self):
        declare.plugin(
            id="org.test.all",
            name="All",
            version="1",
            permissions=["data_access"],
            subscriptions=["epochs"],
        )

        @server.reader("r", extensions="TIF", name="Reader")
        class R:
            pass

        @server.exporter("e", extension=".csv")
        class E:
            pass

        @server.command("c", title="C")
        def c(ctx):
            pass

        @server.tool(
            "t",
            name="Tool",
            views=["2d"],
            menu=None,
            wants_pick=False,
            triggers=[declare.trigger(key="Ctrl+J")],
            actions=[declare.action("undo", key="U", pane=declare.pane("icons/a.svg"))],
            pane=declare.pane("icons/a.svg", tooltip="Tool", hold="momentary"),
        )
        class T:
            pass

        declare.overlay("o", segment_mib=8)
        declare.asset(
            "model",
            filename="m.bin",
            size=3,
            sha256="ab" * 32,
            source={"kind": "manual"},
        )
        declare.panel(
            "p",
            title="P",
            menu_label="Panel",
            root=widgets.column(
                "root",
                widgets.label("l", "L"),
                widgets.slider("s", command=c, min=0, max=10, step=0.5, value=2, float=True),
                widgets.row(
                    "r",
                    widgets.dropdown("d", ["a", "b"], command=c, value="b"),
                    widgets.checkbox("x", "X", command=c, value=True),
                ),
                widgets.line_edit("le", command=c, placeholder="…"),
                widgets.progress("pr", indeterminate=True),
                widgets.image("img"),
            ),
        )
        data = package.manifest_data()
        parsed = manifest.parse_manifest(data, source_dir=self.source)
        self.assertEqual([r.extensions for r in parsed.readers], [(".tif",)])
        self.assertEqual(parsed.tools[0].views, ("2d",))
        self.assertIs(parsed.tools[0].wants_pick, False)
        self.assertEqual(parsed.permissions, ("data_access",))
        self.assertEqual(parsed.subscriptions, ("epochs",))
        self.assertEqual(declare.pane_icons(data), ["icons/a.svg"])

    def test_region_types_are_declared_by_their_shaper_or_on_their_own(self):
        """A shaped type is declared by its decorator; a type
        with no shaper -- whose commits are answered REJECTED -- by region_type()."""
        declare.plugin(id="org.test.s", name="S", version="1", permissions=["contribute_rois"])

        @server.region_shaper("tetra.volume", name="Keeps its volume")
        def keep(ctx, edit):
            return edit.positions

        declare.region_type("tetra.unshaped")
        data = declare.build(["py"])
        self.assertEqual(
            data["contributions"]["region_types"],
            [{"id": "tetra.volume", "name": "Keeps its volume"}, {"id": "tetra.unshaped"}],
        )
        parsed = manifest.parse_manifest(data)
        self.assertEqual([r.id for r in parsed.region_types], ["tetra.volume", "tetra.unshaped"])
        self.assertIn("tetra.volume", server._SHAPERS)
        self.assertNotIn("tetra.unshaped", server._SHAPERS)

    def test_a_rule_the_host_enforces_is_reported_before_anything_is_written(self):
        declare.plugin(id="org.test.x", name="X", version="1")

        @server.command("c")
        def c(ctx):
            pass

        declare.panel("p", title="P", root=widgets.slider("s", command=c, min=5, max=1))
        with self.assertRaisesRegex(manifest.ManifestError, "min < max"):
            package.install(self.tmp / "plugins")
        self.assertFalse((self.tmp / "plugins").exists())

    def test_a_missing_icon_is_the_parsers_diagnostic(self):
        declare.plugin(id="org.test.x", name="X", version="1")

        @server.command("c", pane=declare.pane("icons/missing.svg"))
        def c(ctx):
            pass

        with self.assertRaisesRegex(manifest.ManifestError, "not found in the plugin directory"):
            package.manifest_data()

    def test_an_icon_path_that_leaves_the_folder_on_any_os_is_refused(self):
        """On Windows a drive-relative ``C:x.svg`` or rooted
        ``\\x.svg`` is not ``is_absolute()``, and joining it to the plugin
        folder leaves the folder -- where ``--install`` then wrote."""
        for icon in ("C:a.svg", "\\a.svg", "icons\\..\\..\\a.svg", "/a.svg", "../a.svg"):
            with self.subTest(icon=icon):
                self.assertFalse(manifest.is_contained_relative(icon))
        self.assertTrue(manifest.is_contained_relative("icons/a.svg"))
        self.assertTrue(manifest.is_contained_relative("icons\\a.svg"))
        with self.assertRaisesRegex(package.PackageError, "not a path inside"):
            package._plain_parents(self.tmp, "C:a.svg")


class Installing(_Declared):
    def setUp(self) -> None:
        super().setUp()
        self.plugins = self.tmp / "plugins"
        self.target = self.plugins / "org.test.hello"

    def test_install_writes_the_package_and_the_host_accepts_it(self):
        self.declare_hello()
        self.assertEqual(package.install(self.plugins), self.target)
        self.assertEqual((self.target / "icons" / "a.svg").read_bytes(), _SVG)
        self.assertTrue((self.target / package.MARKER).is_file())
        self.assertFalse((self.target / package.LOCK).exists())
        loaded = manifest.load_manifest(self.target)
        self.assertEqual(loaded.command, ("py", "plugin.py"))
        self.assertEqual(loaded.id, "org.test.hello")

    def test_reinstalling_is_idempotent(self):
        self.declare_hello()
        package.install(self.plugins)
        before = {p.name: p.stat().st_mtime_ns for p in self.target.rglob("*") if p.is_file()}
        package.install(self.plugins)
        after = {p.name: p.stat().st_mtime_ns for p in self.target.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "an identical file is left alone")

    def test_the_plugins_directory_defaults_to_the_hosts(self):
        self.declare_hello()
        with mock.patch.dict(os.environ, {package.ENV_PLUGINS_DIR: str(self.plugins)}):
            self.assertEqual(package.install(), self.target)

    def test_a_directory_this_installer_did_not_create_needs_force(self):
        self.declare_hello()
        self.target.mkdir(parents=True)
        (self.target / "notes.txt").write_text("mine")
        with self.assertRaisesRegex(PackageError, "this installer did not create it") as caught:
            package.install(self.plugins)
        self.assertIn("--force", caught.exception.fix)
        self.assertFalse((self.target / manifest.MANIFEST_FILENAME).exists())
        package.install(self.plugins, force=True)
        self.assertEqual((self.target / "notes.txt").read_text(), "mine")
        self.assertTrue((self.target / manifest.MANIFEST_FILENAME).is_file())

    def test_a_held_lock_refuses_and_is_not_taken_away(self):
        self.declare_hello()
        self.target.mkdir(parents=True)
        (self.target / package.LOCK).write_text("")
        with self.assertRaisesRegex(PackageError, "another install") as caught:
            package.install(self.plugins)
        self.assertEqual(caught.exception.path, str(self.target / package.LOCK))
        self.assertIn(str(self.target / package.LOCK), caught.exception.fix)
        self.assertTrue((self.target / package.LOCK).exists())

    def test_the_manifest_is_written_last(self):
        """A failure partway leaves the previous manifest naming a complete
        package, and releases the lock."""
        self.declare_hello()
        package.install(self.plugins)
        old = (self.target / manifest.MANIFEST_FILENAME).read_bytes()
        declare.plugin(id="org.test.hello", name="Hello", version="2.0")
        real = package._write_file

        def failing(root, relative, data):
            if relative == package.MARKER:
                raise OSError("disk full")
            real(root, relative, data)

        with mock.patch.object(package, "_write_file", failing):
            with self.assertRaisesRegex(OSError, "disk full"):
                package.install(self.plugins)
        self.assertEqual((self.target / manifest.MANIFEST_FILENAME).read_bytes(), old)
        self.assertFalse((self.target / package.LOCK).exists())

    def test_an_id_that_is_not_one_portable_directory_name_is_refused(self):
        # The id grammar refuses these before a directory is named.
        for bad in ("org/test", "a:b", "CON", "trailing.", ".."):
            with self.subTest(bad=bad):
                declare.plugin(id=bad, name="X", version="1")
                with self.assertRaisesRegex(manifest.ManifestError, "is not a plugin id"):
                    package.install(self.plugins)
        # So does a Windows device name as the first part, which is no directory there.
        for bad in ("con.example", "lpt1.tool"):
            with self.subTest(bad=bad):
                declare.plugin(id=bad, name="X", version="1")
                with self.assertRaisesRegex(manifest.ManifestError, "is a Windows device name"):
                    package.install(self.plugins)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_symlinked_package_directory_is_refused(self):
        self.declare_hello()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.plugins.mkdir()
        self.target.symlink_to(elsewhere)
        with self.assertRaisesRegex(PackageError, "symbolic link") as caught:
            package.install(self.plugins)
        self.assertEqual(caught.exception.path, str(self.target))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_an_ephemeral_interpreter_is_refused_unless_forced(self):
        self.declare_hello()
        with mock.patch.object(sys, "prefix", str(Path(tempfile.gettempdir()) / "env")):
            with self.assertRaisesRegex(PackageError, "cache or temporary directory"):
                package.install(self.plugins)
            self.assertEqual(package.install(self.plugins, force=True), self.target)

    def test_uv_and_pipx_caches_are_ephemeral(self):
        with mock.patch.dict(os.environ, {"UV_CACHE_DIR": str(self.tmp / "uvc")}):
            with mock.patch.object(sys, "prefix", str(self.tmp / "uvc" / "archive-v0" / "x")):
                self.assertIsNotNone(package.ephemeral_environment())
        with mock.patch.dict(os.environ, {"PIPX_HOME": str(self.tmp / "px")}):
            with mock.patch.object(sys, "prefix", str(self.tmp / "px" / ".cache" / "abc")):
                self.assertIsNotNone(package.ephemeral_environment())

    def test_a_lasting_venv_is_not_ephemeral(self):
        with mock.patch.object(package, "_ephemeral_roots", return_value=[self.tmp / "cache"]):
            with mock.patch.object(sys, "prefix", str(self.tmp / "project" / ".venv")):
                self.assertIsNone(package.ephemeral_environment())

    def test_pythonpath_is_warned_about(self):
        self.declare_hello()
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/somewhere"}):
            with mock.patch("sys.stderr") as err:
                package.install(self.plugins)
        written = "".join(call.args[0] for call in err.write.call_args_list)
        self.assertIn("PYTHONPATH", written)

    def test_manifest_mode_writes_icons_and_manifest_only(self):
        self.declare_hello()
        out = self.tmp / "out"
        self.assertEqual(package.write_manifest(out), out / manifest.MANIFEST_FILENAME)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["icons", "vizi-plugin.json"])
        manifest.load_manifest(out)

    def test_manifest_mode_into_the_source_leaves_its_icons_alone(self):
        self.declare_hello()
        package.write_manifest(self.source)
        self.assertEqual((self.source / "icons" / "a.svg").read_bytes(), _SVG)
        manifest.load_manifest(self.source)


class LaunchCommand(unittest.TestCase):
    """``entry.command`` is what the host will run, from the plugin's own
    directory, with none of this shell's state."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def _main(self, **attrs):
        main = types.ModuleType("__main__")
        main.__spec__ = None
        for k, v in attrs.items():
            setattr(main, k, v)
        return mock.patch.dict(sys.modules, {"__main__": main})

    def test_a_script_is_run_by_its_absolute_path(self):
        """A bare name beside the working directory, not `os.path.relpath`:
        that raises when the two paths are on different Windows drives.
        The expected path comes from `getcwd()`, which on
        macOS names `/private/var` for a temp dir created as `/var`."""
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        expected = os.path.join(os.getcwd(), "plugin.py")
        with self._main(__file__="plugin.py"):
            self.assertEqual(package.launch_command(), [sys.executable, expected])

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_the_interpreter_is_not_resolved_through_a_symlink(self):
        """A venv's python is a symlink to the base interpreter; resolving it
        would lose the venv's site-packages."""
        link = self.tmp / "bin" / "python"
        link.parent.mkdir()
        link.symlink_to(sys.executable)
        with self._main(__file__=str(self.tmp / "p.py")):
            with mock.patch.object(sys, "executable", str(link)):
                self.assertEqual(package.launch_command()[0], str(link))

    def test_pythonw_is_replaced_by_python(self):
        (self.tmp / "python.exe").write_bytes(b"")
        with self._main(__file__=str(self.tmp / "p.py")):
            with mock.patch.object(sys, "executable", str(self.tmp / "pythonw.exe")):
                self.assertEqual(package.launch_command()[0], str(self.tmp / "python.exe"))

    def test_a_frozen_plugin_is_its_executable(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(package.launch_command(), [sys.executable])

    def test_a_module_is_run_with_dash_m_when_importable_elsewhere(self):
        spec = types.SimpleNamespace(name="json.__main__")
        with self._main(__spec__=spec, __file__="ignored"):
            self.assertEqual(package.launch_command(), [sys.executable, "-m", "json"])

    def test_a_module_importable_only_from_here_is_refused(self):
        spec = types.SimpleNamespace(name="no_such_module_823")
        with self._main(__spec__=spec, __file__="ignored"):
            with self.assertRaisesRegex(PackageError, "importable only from the current"):
                package.launch_command()

    def test_no_script_is_refused(self):
        with self._main():
            with self.assertRaisesRegex(PackageError, "python -c"):
                package.launch_command()

    def test_a_zipped_script_is_run_by_its_archive(self):
        # As the interpreter really sets it up: `__spec__.name` is
        # "__main__", which a stubbed __main__ without a spec never exercised.
        archive = self.tmp / "plugin.pyz"
        archive.write_bytes(b"PK")
        spec = types.SimpleNamespace(name="__main__")
        with self._main(__spec__=spec, __file__=str(archive / "__main__.py")):
            self.assertEqual(package.launch_command(), [sys.executable, str(archive)])

    def test_a_real_zipapp_names_its_archive(self):
        """The archive branch was unreachable from a
        real zipapp, whose `__main__` has a spec; `--install` refused with
        "importable only from the current directory"."""
        import json
        import subprocess
        import zipapp

        # Named from the module itself: the same package can live under
        # another name and at a different depth in another tree.
        parent, _, _ = package.__name__.rpartition(".")
        repo = Path(package.__file__).resolve().parents[package.__name__.count(".")]
        source = self.tmp / "app"
        source.mkdir()
        (source / "__main__.py").write_text(
            "import json\n"
            f"from {parent} import package\n"
            "print(json.dumps(package.launch_command()))\n"
        )
        archive = self.tmp / "plugin.pyz"
        zipapp.create_archive(source, archive)
        done = subprocess.run(
            [sys.executable, str(archive)],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": str(repo)},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        command = json.loads(done.stdout)
        self.assertEqual(os.path.realpath(command[-1]), os.path.realpath(archive))
        self.assertEqual(len(command), 2)


class CommandLine(_Declared):
    def test_an_undeclared_plugins_arguments_are_its_own(self):
        self.assertIsNone(package.dispatch([]))
        self.assertIsNone(package.dispatch(["--mode", "fast"]))

    def test_an_undeclared_plugin_asked_to_install_says_why_it_cannot(self):
        with mock.patch("sys.stderr") as err:
            self.assertEqual(package.dispatch(["--install"]), 2)
        written = "".join(call.args[0] for call in err.write.call_args_list)
        self.assertIn("plugin(", written)

    def test_a_declared_plugin_with_no_arguments_serves(self):
        self.declare_hello()
        self.assertIsNone(package.dispatch([]))

    def test_a_declared_plugin_owns_its_arguments(self):
        self.declare_hello()
        for argv in (["--force"], ["--mode", "fast"], ["--install", "--manifest"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr") as err:
                self.assertEqual(package.dispatch(argv), 2)
            written = "".join(call.args[0] for call in err.write.call_args_list)
            self.assertIn("FAILED: org.test.hello", written)

    def test_manifest_and_install_report_what_they_wrote(self):
        self.declare_hello()
        with mock.patch("sys.stdout") as out:
            self.assertEqual(package.dispatch([f"--manifest={self.tmp / 'm'}"]), 0)
            self.assertEqual(package.dispatch(["--install", str(self.tmp / "p")]), 0)
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertIn(str(self.tmp / "m" / manifest.MANIFEST_FILENAME), printed)
        self.assertIn(str(self.tmp / "p" / "org.test.hello"), printed)

    def test_a_failure_is_a_message_and_a_status_not_a_traceback(self):
        self.declare_hello()
        (self.tmp / "p" / "org.test.hello").mkdir(parents=True)
        (self.tmp / "p" / "org.test.hello" / "x").write_text("")
        with mock.patch("sys.stderr") as err:
            self.assertEqual(package.dispatch([f"--install={self.tmp / 'p'}"]), 1)
        written = "".join(call.args[0] for call in err.write.call_args_list)
        self.assertIn("--force", written)

    def test_a_stale_manifest_is_warned_about(self):
        self.declare_hello()
        package.write_manifest(self.tmp)
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        self.assertIsNone(package.stale_manifest_warning())
        declare.plugin(id="org.test.hello", name="Hello, renamed", version="1.2.3")
        self.assertIn("--install", package.stale_manifest_warning())


class RunningTheScript(unittest.TestCase):
    """The modes as an author runs them: a real interpreter on a real file."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = {**os.environ, "PYTHONPATH": _SDK_ROOT}
        self.env.pop(package.ENV_PLUGINS_DIR, None)

    def _script(self, name: str, body: str) -> Path:
        path = self.tmp / name
        path.write_text(textwrap.dedent(f"""\
                from {_SDK} import command, plugin, serve

                plugin(id="org.test.script", name="Script", version="0.1")

                @command("go")
                def go(ctx):
                    return "went"
                """) + textwrap.dedent(body))
        return path

    def _run(self, *argv, cwd=None):
        return subprocess.run(
            [sys.executable, *argv],
            cwd=cwd or self.tmp,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=90,  # under the suite's 120 s per-test cap, which ends the session
        )

    def test_install_names_this_interpreter_and_this_script(self):
        script = self._script("plugin.py", 'if __name__ == "__main__":\n    serve()\n')
        done = self._run(str(script), f"--install={self.tmp / 'plugins'}")
        self.assertEqual(done.returncode, 0, done.stderr)
        installed = self.tmp / "plugins" / "org.test.script"
        self.assertIn(f"INSTALLED: org.test.script 0.1 at {installed}", done.stdout)
        loaded = manifest.load_manifest(installed)
        self.assertEqual(loaded.command, (sys.executable, str(script)))
        self.assertIn("PYTHONPATH", done.stderr, "the host will not have it")

    def test_neither_mode_needs_grpc(self):
        script = self.tmp / "plugin.py"
        script.write_text(
            "import sys\nsys.modules['grpc'] = None\n"
            + self._script("unused.py", "").read_text()
            + 'if __name__ == "__main__":\n    serve()\n'
        )
        done = self._run(str(script), f"--manifest={self.tmp / 'out'}")
        self.assertEqual(done.returncode, 0, done.stderr)
        manifest.load_manifest(self.tmp / "out")

    def test_a_module_run_from_its_own_directory_only_is_refused(self):
        pkg = self.tmp / "localplugin"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "__main__.py").write_text(self._script("unused.py", "serve()\n").read_text())
        done = self._run("-m", "localplugin", "--install", str(self.tmp / "plugins"))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("importable only from the current directory", done.stderr)
        self.assertFalse((self.tmp / "plugins").exists())


class FailureReports(RunningTheScript):
    """What a person sees when ``--install`` fails, through the real entry: a
    headline naming the plugin and where it was going, why, and a fix set
    apart.  Installs go only into
    this test's temporary folder."""

    HEADLINE = "INSTALL FAILED: org.test.script 0.1"

    def setUp(self) -> None:
        super().setUp()
        self.default = self.tmp / "default-plugins"
        self.env[package.ENV_PLUGINS_DIR] = str(self.default)
        self.script = self._script("plugin.py", 'if __name__ == "__main__":\n    serve()\n')

    def _fails(self, *argv, status=1, env=None):
        done = subprocess.run(
            [sys.executable, str(self.script), *argv],
            cwd=self.tmp,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            timeout=90,  # under the suite's 120 s per-test cap, which ends the session
        )
        self.assertEqual(done.returncode, status, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        lines = done.stderr.splitlines()
        start = next(i for i, line in enumerate(lines) if " FAILED: " in line)
        report = "\n".join(lines[start:])
        headline, why = lines[start : start + 2]
        self.assertTrue(why.startswith("Why: "), report)
        fix = report.split("\nTo fix:\n", 1)[1]
        self.assertFalse(self.default.exists(), "nothing may land in the default folder")
        return headline, why, fix

    def test_a_misspelled_option_is_named_and_installs_nothing(self):
        headline, why, fix = self._fails("--instal", status=2)
        self.assertEqual(headline, "COMMAND FAILED: org.test.script 0.1")
        self.assertIn("--instal", why)
        self.assertIn("Write --instal as --install", fix)

    def test_an_empty_folder_is_refused_not_taken_as_the_default(self):
        for argv in (["--install="], ["--install", ""]):
            with self.subTest(argv=argv):
                headline, why, fix = self._fails(*argv, status=2)
                self.assertEqual(headline, self.HEADLINE)
                self.assertIn("the folder given to --install is empty", why)
                self.assertIn("--install=DIR", fix)

    def test_an_ephemeral_interpreter_is_refused_with_the_command_to_force_it(self):
        cache = os.path.dirname(os.path.realpath(sys.prefix))
        target = self.tmp / "plugins"
        headline, why, fix = self._fails(f"--install={target}", env={"UV_CACHE_DIR": cache})
        self.assertEqual(
            headline, f"{self.HEADLINE} was not installed at {target / 'org.test.script'}"
        )
        self.assertIn("cache or temporary directory", why)
        for shell in package._shells():  # quoted as each shell on this platform takes it
            self.assertIn(f"{package._quote(f'--install={target}', shell)} --force", fix)

    def test_a_folder_this_installer_did_not_create_says_how_to_force_it(self):
        mine = self.tmp / "plugins" / "org.test.script"
        mine.mkdir(parents=True)
        (mine / "notes.txt").write_text("mine")
        headline, why, fix = self._fails(f"--install={self.tmp / 'plugins'}")
        self.assertEqual(headline, f"{self.HEADLINE} was not installed at {mine}")
        self.assertIn(f"check the package folder {mine}", why)
        self.assertIn("--force", fix)

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "needs a folder this user cannot write")
    def test_an_unwritable_folder_names_the_operation_and_the_folder(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(locked.chmod, 0o755)
        headline, why, fix = self._fails(f"--install={locked}")
        self.assertEqual(
            headline, f"{self.HEADLINE} was not installed at {locked / 'org.test.script'}"
        )
        self.assertEqual(
            why, f"Why: create the package folder {locked / 'org.test.script'}: permission denied"
        )
        self.assertIn(f"Check the permissions of {locked}", fix)

    def test_a_held_lock_gives_its_full_path_and_the_command_to_remove_it(self):
        lock = self.tmp / "plugins" / "org.test.script" / package.LOCK
        lock.parent.mkdir(parents=True)
        lock.write_text("")
        _headline, why, fix = self._fails(f"--install={self.tmp / 'plugins'}")
        self.assertIn(f"take the install lock {lock}", why)
        for line in package._removal(lock):
            self.assertIn(line, fix)
        self.assertTrue(lock.exists(), "the lock is not taken away")

    def test_on_windows_a_fix_is_given_for_powershell_and_cmd_not_sh(self):
        """The fixes were POSIX-quoted -- `'C:\\…'` means
        nothing to cmd -- and a Windows lock path sat on an `rm` line."""
        from pathlib import PureWindowsPath

        argv = [r"C:\Py\python.exe", r"C:\Users\Jo Lee\p.py", r"--install=C:\x y", "--force"]
        self.assertEqual(
            package._command(argv, windows=True),
            [
                r"PowerShell:  & C:\Py\python.exe 'C:\Users\Jo Lee\p.py' "
                r"'--install=C:\x y' --force",
                r'cmd:         C:\Py\python.exe "C:\Users\Jo Lee\p.py" "--install=C:\x y" --force',
            ],
        )
        lock = PureWindowsPath(r"C:\Users\Jo's\p\.vizi-install-lock")
        self.assertEqual(
            package._removal(lock, windows=True),
            [
                r"PowerShell:  Remove-Item -LiteralPath 'C:\Users\Jo''s\p\.vizi-install-lock'",
                'cmd:         del "C:\\Users\\Jo\'s\\p\\.vizi-install-lock"',
            ],
        )
        self.assertEqual(
            package._removal(PurePosixPath("/a b/.l"), windows=False), ["rm '/a b/.l'"]
        )

    def test_a_declaration_refused_at_import_is_reported_not_a_traceback(self):
        self.script.write_text(
            self.script.read_text().replace('version="0.1")', 'version="0.1", engines="9.9")')
        )
        headline, why, fix = self._fails(f"--install={self.tmp / 'plugins'}")
        self.assertTrue(headline.startswith(self.HEADLINE), headline)
        self.assertIn("engines '9.9'", why)
        self.assertIn("plugin(...)", fix)
        traced = subprocess.run(
            [sys.executable, str(self.script), f"--install={self.tmp / 'plugins'}"],
            env={**self.env, package.ENV_TRACE: "1"},
            capture_output=True,
            text=True,
            timeout=90,  # under the suite's 120 s per-test cap, which ends the session
        )
        self.assertIn("Traceback", traced.stderr, f"{package.ENV_TRACE} shows it")

    def test_a_success_says_so_with_the_launch_command(self):
        done = self._run(str(self.script), f"--install={self.tmp / 'plugins'}")
        self.assertEqual(done.returncode, 0, done.stderr)
        first, launch = done.stdout.splitlines()[:2]
        self.assertEqual(
            first, f"INSTALLED: org.test.script 0.1 at {self.tmp / 'plugins' / 'org.test.script'}"
        )
        self.assertTrue(launch.startswith("Launch: "), launch)


if __name__ == "__main__":
    unittest.main()
