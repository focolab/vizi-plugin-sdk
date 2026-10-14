use super::{install, DeclaredPlugin, ManagementCommand, Result};
use std::{ffi::OsString, fmt, io::Write, path::Path, process::ExitCode};

/// A complete, printable CLI failure report. Display and Debug deliberately use
/// the same unquoted text. Prefer [`report_exit`] in main to print it to stderr.
pub struct ManagementError(String);
impl fmt::Display for ManagementError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}
impl fmt::Debug for ManagementError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(self, f)
    }
}
impl std::error::Error for ManagementError {}

/// Finish main with Display output on stderr and a nonzero exit on failure.
/// Avoid returning a boxed string error from main: Rust would Debug-quote it.
pub fn report_exit(result: std::result::Result<(), impl fmt::Display>) -> ExitCode {
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            let _ = writeln!(std::io::stderr().lock(), "{error}");
            ExitCode::FAILURE
        }
    }
}

impl ManagementCommand {
    /// Parse management arguments, build the declaration, and install or export
    /// it. Returns None after a successful management command (including help),
    /// or the plugin to serve when there are no arguments. The identity must
    /// match the builder; it is supplied first so even build failures name it.
    /// No host handles or application workers should be started before this call.
    /// Unexpected management panics are reported without a stack trace. A hook
    /// proxy delegates to the previous application hook outside this boundary.
    pub fn prepare(
        id: &str,
        version: &str,
        args: impl IntoIterator<Item = impl Into<OsString>>,
        build: impl FnOnce() -> Result<DeclaredPlugin>,
    ) -> std::result::Result<Option<DeclaredPlugin>, ManagementError> {
        Self::prepare_with_resources(id, version, args, build, |_| Ok(()))
    }

    /// Like prepare, with an application resource step after publication and
    /// before success is printed. A failure reports that publication occurred.
    /// The callback receives the absolute directory containing the manifest.
    pub fn prepare_with_resources(
        id: &str,
        version: &str,
        args: impl IntoIterator<Item = impl Into<OsString>>,
        build: impl FnOnce() -> Result<DeclaredPlugin>,
        resources: impl FnOnce(&Path) -> Result<()>,
    ) -> std::result::Result<Option<DeclaredPlugin>, ManagementError> {
        let args: Vec<OsString> = args.into_iter().map(Into::into).collect();
        let context = Attempt::new(id, version, &args);
        let guard = PanicGuard::enter(context.clone());
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            Self::prepare_inner(id, version, args, build, resources, &context)
        })) {
            Ok(result) => result,
            Err(payload) => Err(guard.failure(payload.as_ref())),
        }
    }

    fn prepare_inner(
        id: &str,
        version: &str,
        args: Vec<OsString>,
        build: impl FnOnce() -> Result<DeclaredPlugin>,
        resources: impl FnOnce(&Path) -> Result<()>,
        context: &Attempt,
    ) -> std::result::Result<Option<DeclaredPlugin>, ManagementError> {
        let command = Self::parse(args).map_err(|e| {
            let mut context = context.clone();
            if context.operation == "PLUGIN START" {
                context.operation = "COMMAND LINE";
            }
            context.failure(
                &e,
                "Nothing written.",
                "Correct the named argument; use --help for usage.",
            )
        })?;
        if matches!(command, Some(Self::Help)) {
            std::io::stdout()
                .lock()
                .write_all(Self::HELP.as_bytes())
                .map_err(|e| {
                    context.failure(
                        &format!("writing help to stdout: {}", crate::manifest::io_cause(&e)),
                        "Nothing written.",
                        "Check that stdout is open and writable.",
                    )
                })?;
            return Ok(None);
        }
        panic_phase("building the plugin declaration", false);
        let plugin = build().map_err(|e| {
            context.failure(&format!("building plugin declaration: {e}"), "Nothing written.", "Correct the plugin declaration, embedded icons or launch command, rebuild, and retry.")
        })?;
        if plugin.manifest().id() != id || plugin.manifest().version() != version {
            return Err(context.failure(
                "CLI identity differs from the built plugin's id or version",
                "Nothing written.",
                "Pass the same id and version to prepare and PluginBuilder, then rebuild.",
            ));
        }
        let (directory, success, launch) = match command {
            None => return Ok(Some(plugin)),
            Some(Self::Help) => unreachable!(),
            Some(Self::Install(options)) => {
                panic_phase("installing the plugin", true);
                let (directory, launch) = plugin.install_with_command(&options).map_err(|e| {
                    context.failure(&e, "", "Check the destination and retry --install.")
                })?;
                (directory, "INSTALLED", Some(launch))
            }
            Some(Self::Manifest(directory)) => {
                panic_phase("writing the manifest", true);
                let path = plugin.write_manifest(directory).map_err(|e| {
                    context.failure(&e, "", "Check the destination and retry --manifest.")
                })?;
                (path.parent().unwrap().to_owned(), "MANIFEST WRITTEN", None)
            }
        };
        let state = format!(
            "The manifest was published in {}; installation resources may be incomplete.",
            directory.display()
        );
        panic_phase("packaging application resources", true);
        resources(&directory).map_err(|e| {
            context.failure(
                &format!("packaging resources in {}: {e}", directory.display()),
                &state,
                "Check the resource path and permissions, then retry.",
            )
        })?;
        let launch = launch
            .map(|command| format!(" (launch executable: {})", one_line(&command)))
            .unwrap_or_default();
        writeln!(
            std::io::stdout().lock(),
            "{success}: {id} {version} in {}{launch}",
            directory.display()
        )
        .map_err(|e| {
            context.failure(
                &format!(
                    "writing success report to stdout: {}",
                    crate::manifest::io_cause(&e)
                ),
                &format!("Publication completed in {}.", directory.display()),
                "Check that stdout is open and writable; the files are already published.",
            )
        })?;
        Ok(None)
    }
}

#[derive(Clone)]
struct Attempt {
    identity: String,
    operation: &'static str,
    destination: String,
}
impl Attempt {
    fn new(id: &str, version: &str, args: &[OsString]) -> Self {
        // Recover the user's intended destination even when parsing fails later.
        // This pass never writes or interprets an unknown flag as a command.
        let mut operation = "PLUGIN START";
        let mut directory = None;
        for (index, arg) in args.iter().enumerate() {
            let spelling = arg.to_string_lossy();
            let (flag, inline) = spelling
                .split_once('=')
                .map_or((spelling.as_ref(), None), |(a, b)| (a, Some(b)));
            if matches!(flag, "--install" | "--manifest") {
                operation = if flag == "--install" {
                    "INSTALL"
                } else {
                    "MANIFEST"
                };
                directory = inline.map(std::path::PathBuf::from).or_else(|| {
                    args.get(index + 1)
                        .filter(|a| !a.as_encoded_bytes().starts_with(b"-"))
                        .map(std::path::PathBuf::from)
                });
                break;
            }
        }
        let empty_directory = directory.as_ref().is_some_and(|p| p.as_os_str().is_empty());
        let destination = match operation {
            "INSTALL" => directory
                .map(Ok)
                .unwrap_or_else(install::default_plugins_dir)
                .and_then(|p| install::absolute(install::Mode::Install, &p))
                .map(|p| p.join(id)),
            "MANIFEST" => install::absolute(
                install::Mode::Manifest,
                &directory.unwrap_or_else(|| ".".into()),
            ),
            _ => Ok(std::path::PathBuf::new()),
        };
        Self {
            identity: format!("{id} {version}"),
            operation,
            destination: if empty_directory {
                String::new()
            } else {
                destination
                    .map(|p| p.display().to_string())
                    .unwrap_or_else(|_| {
                        "(destination unavailable; specify an absolute directory)".into()
                    })
            },
        }
    }

    fn failure(&self, reason: &str, state: &str, fix: &str) -> ManagementError {
        let mut causes = Vec::new();
        let mut states = Vec::new();
        let mut fixes = Vec::new();
        for line in reason.lines() {
            if let Some(line) = line.strip_prefix("To fix: ") {
                fixes.push(line);
            } else if let Some(line) = line.strip_prefix("State: ") {
                states.push(line);
            } else {
                causes.push(line);
            }
        }
        if !state.is_empty() {
            states.push(state);
        }
        if fixes.is_empty() {
            fixes.push(fix);
        }
        let target = if self.destination.is_empty() {
            String::new()
        } else {
            format!(" into {}", one_line(&self.destination))
        };
        let detail = if self.operation == "COMMAND LINE" {
            "; the command line was not understood"
        } else {
            ""
        };
        let mut report = format!(
            "{} FAILED: {}{}{detail}\nWhy: {}\n",
            self.operation,
            one_line(&self.identity),
            target,
            one_line(&causes.join(" "))
        );
        for state in states {
            report.push_str(&format!("State: {}\n", one_line(state)));
        }
        report.push_str("\nTo fix:\n");
        for fix in fixes {
            report.push_str(&format!("  {}\n", one_line(fix)));
        }
        ManagementError(report.trim_end().to_owned())
    }
}

fn one_line(value: &str) -> String {
    value
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect()
}

// A permanent proxy preserves the application's previous hook on every thread
// outside this CLI boundary. A scoped thread-local report suppresses the default
// panic trace only while management runs; no process-wide hook is temporarily
// removed while another application thread could be panicking.
thread_local! {
    static PANIC_REPORT: std::cell::RefCell<Option<PanicReport>> = const { std::cell::RefCell::new(None) };
}
struct PanicReport {
    attempt: Attempt,
    phase: &'static str,
    may_have_written: bool,
}
impl PanicReport {
    fn failure(&self, payload: &(dyn std::any::Any + Send)) -> ManagementError {
        let cause = payload
            .downcast_ref::<String>()
            .map(String::as_str)
            .or_else(|| payload.downcast_ref::<&str>().copied())
            .unwrap_or("panic with a non-text payload");
        let state = if self.phase == "packaging application resources" {
            "The manifest was published; packaging stopped unexpectedly. Inspect the destination before retrying."
        } else if self.may_have_written {
            "The operation stopped unexpectedly. Files may have been published; inspect the destination before retrying."
        } else {
            "Nothing written."
        };
        self.attempt.failure(
            &format!("unexpected failure while {} for {} at {}: {cause}", self.phase, self.attempt.identity, self.attempt.destination),
            state,
            "Check the plugin's declaration or packaging code for this failure, rebuild, and retry; include this report when reporting a plugin bug.",
        )
    }
}
struct PanicGuard(Option<PanicReport>);
impl PanicGuard {
    fn enter(attempt: Attempt) -> Self {
        static HOOK: std::sync::Once = std::sync::Once::new();
        HOOK.call_once(|| {
            let previous = std::panic::take_hook();
            std::panic::set_hook(Box::new(move |info| {
                let handled = PANIC_REPORT.with(|slot| {
                    let Ok(slot) = slot.try_borrow() else {
                        return false;
                    };
                    let Some(report) = slot.as_ref() else {
                        return false;
                    };
                    // Unwinding is reported by catch_unwind below. With abort,
                    // the hook is the last opportunity to print the CLI report.
                    if cfg!(panic = "abort") {
                        let _ = writeln!(
                            std::io::stderr().lock(),
                            "{}",
                            report.failure(info.payload())
                        );
                    }
                    true
                });
                if !handled {
                    previous(info);
                }
            }));
        });
        Self(PANIC_REPORT.with(|slot| {
            slot.replace(Some(PanicReport {
                attempt,
                phase: "parsing management arguments",
                may_have_written: false,
            }))
        }))
    }
    fn failure(&self, payload: &(dyn std::any::Any + Send)) -> ManagementError {
        PANIC_REPORT.with(|slot| slot.borrow().as_ref().unwrap().failure(payload))
    }
}
impl Drop for PanicGuard {
    fn drop(&mut self) {
        PANIC_REPORT.with(|slot| {
            slot.replace(self.0.take());
        });
    }
}
fn panic_phase(phase: &'static str, may_have_written: bool) {
    PANIC_REPORT.with(|slot| {
        if let Some(report) = slot.borrow_mut().as_mut() {
            report.phase = phase;
            report.may_have_written = may_have_written;
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::declare::{CommandSpec, PaneButton, PluginBuilder};
    use std::{
        fs,
        path::PathBuf,
        process::{Command, Output},
    };

    const ID: &str = "org.example.management";
    const VERSION: &str = "1.2.3";

    // A subprocess entry using exactly the prepare/report_exit pair recommended
    // for main. The test harness is only the launcher; main's exit code and
    // stderr are observed by the parent, including failures before build.
    #[test]
    fn entry() {
        let Ok(case) = std::env::var("VIZI_RUST_MANAGEMENT_CASE") else {
            return;
        };
        let args: Vec<String> =
            serde_json::from_str(&std::env::var("VIZI_RUST_MANAGEMENT_ARGS").unwrap()).unwrap();
        if matches!(case.as_str(), "panic-other-thread" | "panic-after") {
            std::panic::set_hook(Box::new(|_| eprintln!("APPLICATION PANIC HOOK")));
        }
        let id = if case == "bad-id" { "CON" } else { ID };
        let result = ManagementCommand::prepare_with_resources(id, VERSION, args, || {
            assert!(case != "panic-build", "unexpected declaration failure");
            if case == "panic-other-thread" {
                let _ = std::thread::spawn(|| panic!("application worker panic")).join();
            }
            let mut builder = PluginBuilder::new(id, "Management test", VERSION);
            if case == "custom-command" { builder.launch_command(["custom-runner"]); }
            if case == "invalid-launch" { builder.launch_command([""]); }
            if matches!(case.as_str(), "missing-icon" | "revalidate") {
                let mut spec = CommandSpec::new("run", "Run");
                spec.pane = Some(PaneButton::new("icon.svg"));
                builder.command(spec, |_, _| unreachable!());
                if case == "revalidate" { builder.icon("icon.svg", b"<svg/>"); }
            }
            let mut plugin = builder.build()?;
            if case == "revalidate" { plugin.icons.insert("icon.svg".into(), b"not an SVG".to_vec()); }
            Ok(plugin)
        }, |directory| {
            assert!(case != "panic-resource", "unexpected packaging failure");
            if case == "resource-failure" {
                Err(format!("writing resource {}: permission denied\nTo fix: Choose a writable directory with --install=DIR.", directory.join("asset.bin").display()))
            } else { Ok(()) }
        }).map(|_| ());
        let code = report_exit(result);
        if case == "panic-after" {
            let _ = std::panic::catch_unwind(|| panic!("application panic after management"));
        }
        std::process::exit(if code == ExitCode::SUCCESS { 0 } else { 1 });
    }

    fn command(root: &Path, case: &str, args: &[String]) -> Command {
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "declare::management::tests::entry",
                "--nocapture",
            ])
            .env("VIZI_RUST_MANAGEMENT_CASE", case)
            .env(
                "VIZI_RUST_MANAGEMENT_ARGS",
                serde_json::to_string(args).unwrap(),
            )
            .env("VIZI_PLUGINS_DIR", root.join("default-plugins"))
            .current_dir(root);
        command
    }

    fn run(root: &Path, case: &str, args: &[String]) -> Output {
        command(root, case, args).output().unwrap()
    }

    fn failed(output: &Output, operation: &str, reason: &str, fix: &str) -> String {
        assert_eq!(output.status.code(), Some(1), "{output:?}");
        let error = String::from_utf8(output.stderr.clone()).unwrap();
        assert!(
            error.starts_with(&format!("{operation} FAILED: ")),
            "{error}"
        );
        assert!(error.contains(VERSION), "{error}");
        assert!(error.contains(reason), "{error}");
        assert!(error.contains("\n\nTo fix:\n  "), "{error}");
        assert!(
            error.split("To fix:").nth(1).unwrap().contains(fix),
            "{error}"
        );
        assert!(
            !error.contains("Error: \"")
                && !error.contains(".tmp")
                && !error.contains(".vizi-rust-icons-")
                && !error.contains("panicked at")
                && !error.contains("(os error ")
                && !error.contains("stack backtrace"),
            "{error}"
        );
        assert!(!String::from_utf8_lossy(&output.stdout).contains("INSTALLED:"));
        error
    }

    fn install_args(root: &Path) -> Vec<String> {
        vec![
            format!("--install={}", root.display()),
            "--copy=false".into(),
        ]
    }

    #[test]
    fn usage_errors_name_the_actual_argument_before_help_or_build() {
        let root = tempfile::Builder::new()
            .prefix("vizi-management-")
            .tempdir()
            .unwrap();
        for (flag, fix) in [
            ("--runner=jbang", "Remove the --runner argument"),
            ("--copy=maybe", "--copy=true or --copy=false"),
            ("-x", "Accepted options:"),
            (r"--runner=C:\Program Files\plugin.exe", "native binary"),
        ] {
            let mut args = install_args(root.path());
            args.push(flag.into());
            let error = failed(
                &run(root.path(), "invalid-launch", &args),
                "INSTALL",
                &format!("unknown argument {flag}"),
                fix,
            );
            assert!(error.contains(&root.path().join(ID).display().to_string()));
            assert!(error.contains("Nothing written"));
            assert!(!root.path().join(ID).exists());
        }
        failed(
            &run(
                root.path(),
                "normal",
                &["--manifest".into(), "--copy=false".into()],
            ),
            "MANIFEST",
            "--copy=false applies only",
            "--help",
        );
        failed(
            &run(
                root.path(),
                "normal",
                &["--install".into(), "--help".into()],
            ),
            "INSTALL",
            "misplaced argument --help",
            "--help",
        );
        for args in [
            vec!["--install=".into()],
            vec!["--install".into(), "".into()],
            vec!["--manifest=".into()],
            vec!["--manifest".into(), "".into()],
        ] {
            let error = failed(
                &run(root.path(), "normal", &args),
                if args[0].starts_with("--install") {
                    "INSTALL"
                } else {
                    "MANIFEST"
                },
                "directory must not be empty",
                "nonempty directory",
            );
            assert!(!error.lines().next().unwrap().contains(" into "));
        }
        for flag in ["--instal", "--isntall", "--installl", "--instxll"] {
            let error = failed(
                &run(root.path(), "invalid-launch", &[flag.into()]),
                "COMMAND LINE",
                &format!("unknown argument {flag}"),
                "Did you mean --install?",
            );
            assert!(error.contains("the command line was not understood"));
        }
        failed(
            &run(
                root.path(),
                "normal",
                &["--install".into(), "--force".into(), "".into()],
            ),
            "INSTALL",
            "unknown argument ''",
            "Accepted options:",
        );
        let help = run(root.path(), "invalid-launch", &["--help".into()]);
        assert!(help.status.success());
        assert!(help.stderr.is_empty());
    }

    #[test]
    fn build_and_portable_id_refusals_have_context_and_recovery() {
        let root = tempfile::Builder::new()
            .prefix("vizi-management-")
            .tempdir()
            .unwrap();
        for (case, reason) in [
            ("missing-icon", "icon.svg"),
            ("invalid-launch", "launch command"),
            ("bad-id", "got CON"),
            ("panic-build", "unexpected declaration failure"),
        ] {
            let error = failed(
                &run(root.path(), case, &install_args(root.path())),
                "INSTALL",
                reason,
                "rebuild",
            );
            assert!(error.contains(&root.path().display().to_string()));
            assert!(error.contains("Nothing written"));
        }
        let error = failed(
            &run(
                root.path(),
                "custom-command",
                &[format!("--install={}", root.path().display())],
            ),
            "INSTALL",
            "running executable",
            "--copy=false",
        );
        assert!(error.contains("An empty directory remains at"));
    }

    // Make supplied and canonical destinations differ even on Linux.
    fn aliased_plugins_root(root: &Path) -> PathBuf {
        let plugins = root.join("plugins");
        fs::create_dir(&plugins).unwrap();
        #[cfg(unix)]
        let plugins = {
            // Exercise the /var -> /private/var distinction on Linux too:
            // I/O may canonicalize, but diagnostics must keep this alias.
            let alias = root.join("plugins-alias");
            std::os::unix::fs::symlink(&plugins, &alias).unwrap();
            alias
        };
        plugins
    }

    #[test]
    fn locked_and_unmanaged_destinations_keep_existing_files() {
        let root = tempfile::Builder::new()
            .prefix("vizi-management-")
            .tempdir()
            .unwrap();
        let plugins = aliased_plugins_root(root.path());
        let dir = plugins.join(ID);
        fs::create_dir(&dir).unwrap();
        let manifest = dir.join("vizi-plugin.json");
        fs::write(&manifest, "original").unwrap();
        let args = install_args(&plugins);
        failed(
            &run(root.path(), "normal", &args),
            "INSTALL",
            "not an installation",
            "--force",
        );
        let lock = dir.join(".vizi-rust-install.lock");
        fs::write(&lock, "held").unwrap();
        let error = failed(
            &run(root.path(), "normal", &args),
            "INSTALL",
            "lock file already exists",
            &lock.display().to_string(),
        );
        assert!(error.contains("confirm no installer is running"));
        assert!(error
            .lines()
            .next()
            .unwrap()
            .ends_with(&dir.display().to_string()));
        assert!(error.contains(&format!("Why: locking {}:", lock.display())));
        assert!(error.contains(&format!("Directory contents remain at {};", dir.display())));
        assert!(!error.contains(r"\\?\"));
        let commands: Vec<_> = error.lines().map(str::trim).collect();
        if cfg!(windows) {
            assert!(commands.contains(&format!("del \"{}\"", lock.display()).as_str()));
            assert!(commands
                .contains(&format!("Remove-Item -LiteralPath '{}'", lock.display()).as_str()));
        } else {
            assert!(commands.contains(&format!("rm '{}'", lock.display()).as_str()));
        }
        assert_eq!(fs::read_to_string(&manifest).unwrap(), "original");
        assert_eq!(fs::read_to_string(&lock).unwrap(), "held");
    }

    #[test]
    fn io_failure_and_revalidation_name_the_destination_without_temp_names() {
        let root = tempfile::Builder::new()
            .prefix("vizi-management-")
            .tempdir()
            .unwrap();
        let blocked = root.path().join("file not directory");
        fs::write(&blocked, "keep").unwrap();
        let error = failed(
            &run(root.path(), "normal", &install_args(&blocked)),
            "INSTALL",
            "names a file; --install=DIR needs a directory",
            "--install=DIR",
        );
        assert!(error.contains(&blocked.display().to_string()));
        assert!(!error.contains("--manifest=DIR"));
        let error = failed(
            &run(
                root.path(),
                "normal",
                &[format!("--manifest={}", blocked.display())],
            ),
            "MANIFEST",
            "names a file; --manifest=DIR needs a directory",
            "--manifest=DIR",
        );
        assert!(!error.contains("--install=DIR"));
        assert_eq!(fs::read_to_string(&blocked).unwrap(), "keep");
        for flag in ["--install", "--manifest"] {
            let destination = root.path().join(flag.trim_start_matches('-'));
            let error = failed(
                &run(
                    root.path(),
                    "revalidate",
                    &[
                        format!("{flag}={}", destination.display()),
                        "--copy=false".into(),
                    ]
                    .into_iter()
                    .filter(|a| flag == "--install" || a != "--copy=false")
                    .collect::<Vec<_>>(),
                ),
                if flag == "--install" {
                    "INSTALL"
                } else {
                    "MANIFEST"
                },
                "validating manifest",
                "embedded icons",
            );
            assert!(error.contains("No new manifest was published"));
        }
    }

    #[test]
    fn publication_and_post_publication_failure_are_distinguished() {
        let root = tempfile::Builder::new()
            .prefix("vizi-management-")
            .tempdir()
            .unwrap();
        let plugins = aliased_plugins_root(root.path());
        let args = install_args(&plugins);
        let success = run(root.path(), "normal", &args);
        assert!(success.status.success(), "{success:?}");
        assert!(success.stderr.is_empty());
        assert!(String::from_utf8_lossy(&success.stdout).contains(&format!(
            "INSTALLED: {ID} {VERSION} in {}",
            plugins.join(ID).display()
        )));
        let error = failed(
            &run(root.path(), "resource-failure", &args),
            "INSTALL",
            "asset.bin",
            "--install=DIR",
        );
        assert!(error.contains(&format!(
            "The manifest was published in {};",
            plugins.join(ID).display()
        )));
        assert!(!error.contains("No new manifest"));
        let panic = failed(
            &run(root.path(), "panic-resource", &args),
            "INSTALL",
            "unexpected packaging failure",
            "rebuild",
        );
        assert!(panic.contains("The manifest was published; packaging stopped unexpectedly"));
        assert!(!panic.contains("panicked at") && !panic.contains("stack backtrace"));
        let manifest = plugins.join("export");
        let success = run(
            root.path(),
            "normal",
            &[format!("--manifest={}", manifest.display())],
        );
        assert!(success.status.success(), "{success:?}");
        assert!(String::from_utf8_lossy(&success.stdout).contains(&format!(
            "MANIFEST WRITTEN: {ID} {VERSION} in {}",
            manifest.display()
        )));
    }

    #[test]
    fn management_panic_reporting_preserves_application_hooks_elsewhere() {
        let root = tempfile::Builder::new()
            .prefix("vizi-management-")
            .tempdir()
            .unwrap();
        for case in ["panic-other-thread", "panic-after"] {
            let result = run(root.path(), case, &install_args(root.path()));
            assert!(result.status.success(), "{result:?}");
            assert_eq!(
                String::from_utf8(result.stderr).unwrap(),
                "APPLICATION PANIC HOOK\n"
            );
            assert!(String::from_utf8_lossy(&result.stdout).contains("INSTALLED:"));
        }
    }

    #[test]
    fn corrupt_executable_copy_is_named_and_never_overwritten() {
        let root = tempfile::Builder::new()
            .prefix("vizi-management-")
            .tempdir()
            .unwrap();
        let args = vec![format!("--install={}", root.path().display())];
        let success = run(root.path(), "normal", &args);
        assert!(success.status.success(), "{success:?}");
        let dir = root.path().canonicalize().unwrap().join(ID);
        let manifest = crate::manifest::Manifest::load(dir.join("vizi-plugin.json")).unwrap();
        let binary = dir.join(manifest.value()["entry"]["command"][0].as_str().unwrap());
        fs::write(&binary, "corrupt").unwrap();
        let error = failed(
            &run(root.path(), "normal", &args),
            "INSTALL",
            "unexpected contents",
            "move that file aside",
        );
        assert!(error.contains(
            &binary
                .components()
                .collect::<PathBuf>()
                .display()
                .to_string()
        ));
        assert_eq!(fs::read_to_string(binary).unwrap(), "corrupt");
    }

    #[cfg(unix)]
    #[test]
    fn symlink_and_unwritable_directory_fail_through_the_entry() {
        use std::os::unix::{
            fs::{symlink, PermissionsExt},
            process::CommandExt,
        };
        let root = tempfile::Builder::new()
            .prefix("vizi-management-")
            .tempdir()
            .unwrap();
        let outside = root.path().join("outside");
        fs::create_dir(&outside).unwrap();
        let target = root.path().join(ID);
        symlink(&outside, &target).unwrap();
        let error = failed(
            &run(root.path(), "normal", &install_args(root.path())),
            "INSTALL",
            "refusing symlink",
            "Move this symlink aside",
        );
        assert!(error.contains(&target.display().to_string()));
        assert_eq!(fs::read_dir(&outside).unwrap().count(), 0);
        fs::set_permissions(root.path(), fs::Permissions::from_mode(0o755)).unwrap();
        let denied = root.path().join("unwritable");
        fs::create_dir(&denied).unwrap();
        fs::set_permissions(&denied, fs::Permissions::from_mode(0o555)).unwrap();
        for flag in ["--install", "--manifest"] {
            let destination = denied.join("sub/deeper");
            let mut child = command(
                root.path(),
                "normal",
                &[format!("{flag}={}", destination.display())],
            );
            // Root bypasses mode bits. Drop privileges only in that child.
            if unsafe { libc::geteuid() } == 0 {
                child.uid(65534).gid(65534);
            }
            let output = child.output().unwrap();
            let error = failed(
                &output,
                if flag == "--install" {
                    "INSTALL"
                } else {
                    "MANIFEST"
                },
                "permission denied",
                &format!("Check permissions on {};", denied.display()),
            );
            let fix = error.split("To fix:").nth(1).unwrap();
            assert!(fix.contains(&format!("{flag}=DIR")));
            assert!(!fix.contains(if flag == "--install" {
                "--manifest"
            } else {
                "--install"
            }));
            assert!(!denied.join("sub").exists());
        }
        fs::set_permissions(&denied, fs::Permissions::from_mode(0o755)).unwrap();
    }
}
