use super::{DeclaredPlugin, Result};
use crate::manifest::io_cause;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::OsString,
    fs::{self, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};

const MARKER: &str = ".vizi-rust-sdk";
const LOCK: &str = ".vizi-rust-install.lock";
const ICON_PREFIX: &str = ".vizi-rust-icons-";

/// Installs a manifest and, by default, a durable copy of the running executable.
#[derive(Clone, Debug)]
pub struct InstallOptions {
    /// Plugins root, not the individual plugin directory. Defaults to
    /// `VIZI_PLUGINS_DIR`, then the user's `.vizi/plugins` directory.
    pub directory: Option<PathBuf>,
    /// Permit writing into a nonempty directory not owned by this installer.
    /// Unrelated files are preserved; the manifest and ownership marker change.
    pub force: bool,
    /// Copy this executable under a content-derived name. Set false to retain
    /// the declared launch command for development or a custom launcher.
    pub copy_executable: bool,
}
impl Default for InstallOptions {
    fn default() -> Self {
        Self {
            directory: None,
            force: false,
            copy_executable: true,
        }
    }
}

/// Optional CLI parser. Applications with their own CLI can call `install` and
/// `write_manifest` directly. Paths supplied as separate arguments need not be
/// UTF-8. Pass arguments **after** the executable name.
#[derive(Clone, Debug)]
pub enum ManagementCommand {
    Manifest(PathBuf),
    Install(InstallOptions),
    Help,
}
impl ManagementCommand {
    pub const HELP: &'static str = "--manifest [DIR]  Write the generated manifest (default: current directory)\n\
        --install [DIR]   Install under DIR/<plugin-id> (default: VIZI_PLUGINS_DIR or ~/.vizi/plugins)\n\
        --force           With --install, replace an unmanaged manifest\n\
        --copy=false      With --install, retain the declared launch command (development)\n\
        --help            Show these options\n\
        Installation copies this executable by default; custom launchers require --copy=false.\n";

    pub fn parse(args: impl IntoIterator<Item = impl Into<OsString>>) -> Result<Option<Self>> {
        let mut args = args.into_iter().map(Into::into).peekable();
        let mut command = None;
        let mut force = false;
        let mut copy = None;
        while let Some(arg) = args.next() {
            if arg == "--copy=false" || arg == "--copy=true" {
                if copy.replace(arg == "--copy=true").is_some() {
                    return Err(format!("{} supplied twice", arg.to_string_lossy()));
                }
                continue;
            }
            if arg == "--force" {
                if force {
                    return Err("--force supplied twice".into());
                }
                force = true;
                continue;
            }
            let spelling = arg
                .to_str()
                .ok_or_else(|| unknown_argument(&arg.to_string_lossy()))?;
            let (flag, inline) = spelling
                .split_once('=')
                .map_or((spelling, None), |(f, v)| (f, Some(v)));
            if !(matches!(flag, "--manifest" | "--install") || flag == "--help" && inline.is_none())
            {
                return Err(unknown_argument(&arg.to_string_lossy()));
            }
            if command.is_some() {
                return Err(format!(
                    "misplaced argument {}; give only one of --manifest, --install or --help",
                    arg.to_string_lossy()
                ));
            }
            if flag == "--help" && inline.is_none() {
                command = Some(Self::Help);
                continue;
            }
            let directory = if let Some(value) = inline {
                Some(PathBuf::from(value))
            } else if args
                .peek()
                .is_some_and(|a| !a.as_encoded_bytes().starts_with(b"-"))
            {
                args.next().map(PathBuf::from)
            } else {
                None
            };
            if directory.as_ref().is_some_and(|p| p.as_os_str().is_empty()) {
                return Err(format!("{flag} directory must not be empty\nTo fix: Supply a nonempty directory with {flag}=DIR; use --help for usage."));
            }
            command = Some(if flag == "--manifest" {
                Self::Manifest(directory.unwrap_or_else(|| PathBuf::from(".")))
            } else {
                Self::Install(InstallOptions {
                    directory,
                    ..InstallOptions::default()
                })
            });
        }
        if let Some(copy) = copy {
            match &mut command {
                Some(Self::Install(options)) => options.copy_executable = copy,
                _ => return Err(format!("--copy={copy} applies only to --install")),
            }
        }
        if force {
            match &mut command {
                Some(Self::Install(options)) => options.force = true,
                _ => return Err("--force applies only to --install".into()),
            }
        }
        Ok(command)
    }
}

fn unknown_argument(argument: &str) -> String {
    let flag = argument.split('=').next().unwrap_or(argument);
    let fix = if flag.starts_with("--runner") {
        "Remove the --runner argument: the Rust SDK copies a native binary, so there is no runner to choose.".into()
    } else if flag == "--copy" {
        "Use --copy=true or --copy=false with --install.".into()
    } else if let Some(option) = ["--install", "--manifest", "--force", "--copy", "--help"]
        .into_iter()
        .find(|option| one_edit_away(flag, option))
    {
        format!("Did you mean {option}? Replace {flag} with {option}; use --help for usage.")
    } else {
        "Accepted options: --install[=DIR], --manifest[=DIR], --force, --copy=true, --copy=false, --help.".into()
    };
    let argument = if argument.is_empty() { "''" } else { argument };
    format!("unknown argument {argument}\nTo fix: {fix}")
}

// One missing, extra, mistyped or transposed ASCII character is a useful hint;
// do not guess at more distant spellings or interpret them as commands.
fn one_edit_away(a: &str, b: &str) -> bool {
    if !a.is_ascii() || !b.is_ascii() || a.len().abs_diff(b.len()) > 1 {
        return false;
    }
    let (a, b) = (a.as_bytes(), b.as_bytes());
    let i = a
        .iter()
        .zip(b)
        .position(|(x, y)| x != y)
        .unwrap_or(a.len().min(b.len()));
    match a.len().cmp(&b.len()) {
        std::cmp::Ordering::Less => a[i..] == b[i + 1..],
        std::cmp::Ordering::Greater => a[i + 1..] == b[i..],
        std::cmp::Ordering::Equal => {
            i < a.len()
                && (a[i + 1..] == b[i + 1..]
                    || i + 1 < a.len()
                        && a[i] == b[i + 1]
                        && a[i + 1] == b[i]
                        && a[i + 2..] == b[i + 2..])
        }
    }
}

#[derive(Clone, Copy)]
pub(super) enum Mode {
    Install,
    Manifest,
}
impl Mode {
    fn flag(self) -> &'static str {
        match self {
            Self::Install => "--install=DIR",
            Self::Manifest => "--manifest=DIR",
        }
    }
}

impl DeclaredPlugin {
    /// Write `directory/vizi-plugin.json` and the embedded icons it needs. The
    /// manifest is replaced atomically after validation; other files survive.
    /// Returns an absolute manifest path preserving the supplied directory's
    /// spelling, without resolving directory aliases for display.
    /// Available with the default-enabled `install` feature.
    pub fn write_manifest(&self, directory: impl AsRef<Path>) -> Result<PathBuf> {
        let directory = absolute(Mode::Manifest, directory.as_ref())?;
        write_manifest(Mode::Manifest, self, &directory, None)
            .map_err(|e| unpublished(e, &directory))
            .map(|_| directory.join("vizi-plugin.json"))
    }

    /// Write a discoverable `<plugins-root>/<id>/vizi-plugin.json`. Existing
    /// installations of this ID can be updated; other nonempty directories
    /// require `force`. Available with the default-enabled `install` feature.
    /// Re-run after changing the declared interface or rebuilding
    /// the executable. This does not trigger a host rescan or restart a plugin.
    /// Returns an absolute plugin directory preserving the supplied root's
    /// spelling, without resolving directory aliases for display.
    pub fn install(&self, options: &InstallOptions) -> Result<PathBuf> {
        self.install_with_command(options)
            .map(|(directory, _)| directory)
    }

    pub(super) fn install_with_command(
        &self,
        options: &InstallOptions,
    ) -> Result<(PathBuf, String)> {
        let mode = Mode::Install;
        let id = self.manifest.id();
        directory_name(id).map_err(|e| format!("{e}\nState: Nothing written."))?;
        let root = match &options.directory {
            Some(path) if path.as_os_str().is_empty() => {
                return Err("install directory must not be empty.\nState: Nothing written.\nTo fix: Supply a nonempty directory with --install=DIR.".into())
            }
            Some(path) => path.clone(),
            None => default_plugins_dir()?,
        };
        let directory = absolute(mode, &root)?.join(id);
        let mut published = false;
        let mut install_lock = None;
        let mut result = (|| {
            reject_symlink(mode, &directory)?;
            fs::create_dir_all(&directory)
                .map_err(|e| io_error(mode, "creating plugin directory", &directory, e))?;
            // Keep the caller's absolute spelling for reports and shell commands.
            // Canonicalization can add a Windows verbatim prefix or resolve a
            // macOS /var alias; neither should change the path in the report.
            let reported_lock_path = directory.join(LOCK);
            let directory = directory
                .canonicalize()
                .map_err(|e| io_error(mode, "resolving plugin directory", &directory, e))?;
            // Serialize cooperating installers, including the ownership check.
            let lock_path = directory.join(LOCK);
            let lock = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&lock_path)
            .map_err(|e| {
                if e.kind() == std::io::ErrorKind::AlreadyExists {
                    format!("locking {}: lock file already exists; another install may be in progress. The lock is left in place.\nTo fix: Wait for the other installer. If it exited, confirm no installer is running, then remove this lock file, and retry.{}", reported_lock_path.display(), lock_commands(&reported_lock_path, cfg!(windows)))
                } else {
                    io_error(mode, "creating install lock", &reported_lock_path, e)
                }
            })?;
            install_lock = Some(InstallLock {
                file: Some(lock),
                path: lock_path,
                reported_path: reported_lock_path,
            });
            let marker = directory.join(MARKER);
            reject_symlink(mode, &marker)?;
            let ownership = format!("vizi-rust-sdk\n{id}\n");
            let owned = match fs::read_to_string(&marker) {
                Ok(contents) => contents == ownership,
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::NotFound | std::io::ErrorKind::InvalidData
                    ) =>
                {
                    false
                }
                Err(e) => return Err(io_error(mode, "reading ownership marker", &marker, e)),
            };
            let nonempty = fs::read_dir(&directory)
                .map_err(|e| io_error(mode, "reading plugin directory", &directory, e))?
                .collect::<std::io::Result<Vec<_>>>()
                .map_err(|e| io_error(mode, "reading plugin directory entries", &directory, e))?
                .iter()
                .any(|e| e.file_name() != LOCK);
            if nonempty && !owned && !options.force {
                return Err(format!("{} is not an installation of '{id}' owned by this SDK.\nTo fix: Use --force with --install to replace its manifest, or choose another plugins root with --install=DIR", directory.display()));
            }
            let executable = if options.copy_executable {
                Some(copy_executable(self, &directory)?)
            } else {
                None
            };
            write_manifest(mode, self, &directory, executable.as_deref())?;
            published = true;
            atomic_write(mode, &marker, ownership.as_bytes())?;
            let command = executable.unwrap_or_else(|| {
                self.manifest.value()["entry"]["command"][0]
                    .as_str()
                    .unwrap()
                    .to_owned()
            });
            Ok(command)
        })();
        if let Some(lock) = install_lock {
            if let Err(cleanup) = lock.finish() {
                result = Err(match result {
                    Ok(_) => cleanup,
                    Err(error) => format!("{error}\n{cleanup}"),
                });
            }
        }
        result.map_err(|e| {
            if published {
                format!("{e}\nState: The new manifest was published in {}, but installation did not finish. Inspect the reported path before retrying.", directory.display())
            } else {
                unpublished(e, &directory)
            }
        }).map(|command| (directory, command))
    }
}

// Publish immutable generations, so a Windows process can keep its old binary
// open. Hash the bytes copied to the temporary file, not a second source read.
fn copy_executable(plugin: &DeclaredPlugin, directory: &Path) -> Result<String> {
    let mode = Mode::Install;
    let source = std::env::current_exe()
        .map_err(|e| format!("locating running executable: {}\nTo fix: Check that this executable is still available, or use --copy=false with an explicit launch command.", io_cause(&e)))?;
    let source = source
        .canonicalize()
        .map_err(|e| io_error(mode, "resolving running executable", &source, e))?;
    let command = plugin.manifest.value()["entry"]["command"][0]
        .as_str()
        .unwrap();
    if Path::new(command).canonicalize().ok().as_ref() != Some(&source) {
        return Err(format!("copying running executable {}: the declared launch command {command} names a different executable.\nTo fix: Rerun the same --install command with --copy=false for this custom launcher", source.display()));
    }
    let bin = directory.join("bin");
    reject_symlink(mode, &bin)?;
    fs::create_dir_all(&bin)
        .map_err(|e| io_error(mode, "creating executable directory", &bin, e))?;
    let mut input = fs::File::open(&source)
        .map_err(|e| io_error(mode, "opening running executable", &source, e))?;
    let mut output = tempfile::NamedTempFile::new_in(&bin)
        .map_err(|e| io_error(mode, "preparing executable copy in", &bin, e))?;
    let mut hash = Sha256::new();
    let mut buffer = [0; 65536];
    loop {
        let n = input
            .read(&mut buffer)
            .map_err(|e| io_error(mode, "reading running executable", &source, e))?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
        output
            .write_all(&buffer[..n])
            .map_err(|e| io_error(mode, "writing executable copy in", &bin, e))?;
    }
    output
        .as_file()
        .set_permissions(
            input
                .metadata()
                .map_err(|e| io_error(mode, "reading executable permissions", &source, e))?
                .permissions(),
        )
        .map_err(|e| io_error(mode, "setting executable permissions in", &bin, e))?;
    output
        .as_file()
        .sync_all()
        .map_err(|e| io_error(mode, "syncing executable copy in", &bin, e))?;
    let name = format!(
        "plugin-{:x}{}",
        hash.finalize(),
        std::env::consts::EXE_SUFFIX
    );
    let target = bin.join(&name);
    reject_symlink(mode, &target)?;
    if target.exists() {
        // Never overwrite a running copy, and do not trust just its filename.
        let mut existing = fs::File::open(&target)
            .map_err(|e| io_error(mode, "opening installed executable", &target, e))?;
        let mut hash = Sha256::new();
        loop {
            let n = existing
                .read(&mut buffer)
                .map_err(|e| io_error(mode, "checking installed executable", &target, e))?;
            if n == 0 {
                break;
            }
            hash.update(&buffer[..n]);
        }
        if name
            != format!(
                "plugin-{:x}{}",
                hash.finalize(),
                std::env::consts::EXE_SUFFIX
            )
        {
            return Err(format!(
                "installed executable {} has unexpected contents.\nTo fix: Stop hosts using this plugin, move that file aside, and rerun --install",
                target.display()
            ));
        }
    } else {
        output
            .persist_noclobber(&target)
            .map_err(|e| io_error(mode, "publishing executable", &target, e.error))?;
    }
    Ok(format!("./bin/{name}"))
}

pub(super) fn default_plugins_dir() -> Result<PathBuf> {
    if let Some(path) = std::env::var_os("VIZI_PLUGINS_DIR").filter(|v| !v.is_empty()) {
        return Ok(path.into());
    }
    std::env::home_dir()
        .map(|p| p.join(".vizi").join("plugins"))
        .ok_or_else(|| {
            "cannot locate home directory.\nState: Nothing written.\nTo fix: Supply --install=DIR or set VIZI_PLUGINS_DIR.".into()
        })
}

fn directory_name(id: &str) -> Result<()> {
    let base = id.split('.').next().unwrap_or("").to_ascii_uppercase();
    let reserved = matches!(base.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || ["COM", "LPT"].iter().any(|p| {
            base.strip_prefix(p)
                .is_some_and(|n| n.len() == 1 && matches!(n.as_bytes()[0], b'1'..=b'9'))
        });
    if id.is_empty()
        || id.len() > 200
        || !id.as_bytes()[0].is_ascii_alphanumeric()
        || !id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
        || id.ends_with('.')
        || reserved
    {
        return Err(format!("installation needs a portable plugin ID; got {id}: start with an ASCII letter or digit, use letters/digits/._-, no trailing dot or Windows device name (at most 200 bytes).\nTo fix: Change the PluginBuilder id to a portable name, rebuild, and rerun --install"));
    }
    Ok(())
}

struct InstallLock {
    file: Option<fs::File>,
    path: PathBuf,
    reported_path: PathBuf,
}
impl InstallLock {
    fn finish(mut self) -> Result<()> {
        // Windows must close the handle before removal.
        drop(self.file.take());
        fs::remove_file(&self.path).map_err(|e| format!(
            "removing install lock {}: {}\nState: The install lock remains at {}.\nTo fix: Confirm no installer is running, restore directory permissions and remove this lock file before retrying.{}",
            self.reported_path.display(), io_cause(&e), self.reported_path.display(), lock_commands(&self.reported_path, cfg!(windows))))
    }
}
impl Drop for InstallLock {
    fn drop(&mut self) {
        // Best effort during a panic. Ordinary Result paths use finish so a
        // cleanup failure is reported together with the original failure.
        if self.file.is_some() {
            drop(self.file.take());
            let _ = fs::remove_file(&self.path);
        }
    }
}

fn reject_symlink(mode: Mode, path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(m) if m.file_type().is_symlink() => Err(format!("refusing symlink {}.\nTo fix: Move this symlink aside and retry, or choose another directory with {}", path.display(), mode.flag())),
        Ok(_) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(io_error(mode, "inspecting path", path, e)),
    }
}

fn atomic_write(mode: Mode, path: &Path, bytes: &[u8]) -> Result<()> {
    reject_symlink(mode, path)?;
    let mut file = tempfile::NamedTempFile::new_in(path.parent().unwrap())
        .map_err(|e| io_error(mode, "preparing publication of", path, e))?;
    file.write_all(bytes)
        .and_then(|()| file.as_file().sync_all())
        .map_err(|e| io_error(mode, "writing and syncing", path, e))?;
    file.persist(path)
        .map_err(|e| io_error(mode, "publishing", path, e.error))?;
    Ok(())
}

fn write_manifest(
    mode: Mode,
    plugin: &DeclaredPlugin,
    directory: &Path,
    executable: Option<&str>,
) -> Result<PathBuf> {
    fs::create_dir_all(directory)
        .map_err(|e| io_error(mode, "creating manifest directory", directory, e))?;
    let directory = directory
        .canonicalize()
        .map_err(|e| io_error(mode, "resolving manifest directory", directory, e))?;
    let path = directory.join("vizi-plugin.json");
    reject_symlink(mode, &path)?;
    let mut value = plugin.manifest.value().clone();
    if let Some(executable) = executable {
        value["entry"]["command"][0] = executable.into();
    }
    let mut icons = BTreeMap::new();
    visit_icons(&mut value, &mut |icon| {
        let portable = icon.replace('\\', "/");
        let bytes = plugin
            .icons
            .get(&portable)
            .ok_or_else(|| format!("preparing manifest {}: missing embedded icon '{portable}'\nTo fix: Supply the icon with PluginBuilder::icon, rebuild, and retry.", path.display()))?;
        icons.insert(portable.clone(), bytes.as_slice());
        *icon = portable;
        Ok(())
    })?;
    // Unchanged icons can retain their published paths even when metadata
    // changes. Only a different resource set needs another generation.
    let mut resources = None;
    if !icons.is_empty() {
        let name = match reusable_icons(&path, &directory, &icons) {
            Some(name) => name,
            None => {
                let fresh = tempfile::Builder::new()
                    .prefix(ICON_PREFIX)
                    .tempdir_in(&directory)
                    .map_err(|e| io_error(mode, "preparing icons in", &directory, e))?;
                for (icon, bytes) in &icons {
                    let target = fresh.path().join(icon);
                    fs::create_dir_all(target.parent().unwrap()).map_err(|e| {
                        io_error(
                            mode,
                            &format!("creating directories for icon {icon} in"),
                            &directory,
                            e,
                        )
                    })?;
                    fs::write(target, bytes).map_err(|e| {
                        io_error(mode, &format!("writing icon {icon} in"), &directory, e)
                    })?;
                }
                let name = fresh
                    .path()
                    .file_name()
                    .unwrap()
                    .to_str()
                    .unwrap()
                    .to_owned();
                resources = Some(fresh);
                name
            }
        };
        visit_icons(&mut value, &mut |icon| {
            *icon = format!("{name}/{icon}");
            Ok(())
        })?;
    }
    let mut text = serde_json::to_string_pretty(&value).map_err(|e| {
        format!(
            "encoding manifest for {}: {e}\nTo fix: Correct the plugin declaration and rebuild.",
            path.display()
        )
    })?;
    text.push('\n');
    // The same file reader used by a plugin accepts the finished layout before
    // an existing discoverable manifest is replaced.
    let icon_name = |icon: &str| {
        // Name the original resource, not its private staging generation.
        resources
            .as_ref()
            .and_then(|r| {
                icon.strip_prefix(&format!(
                    "{}/",
                    r.path().file_name().unwrap().to_string_lossy()
                ))
            })
            .unwrap_or(icon)
            .to_owned()
    };
    crate::manifest::Manifest::parse_with_icons(&text, &|icon| {
        crate::manifest::load_icon(&directory, icon).map_err(|e| format!(
            "reading packaged icon {} for {}: {e}\nTo fix: Check the packaged icon and read permissions in {}; rebuild if needed, and retry.",
            icon_name(icon), path.display(), directory.display()))
    }).map_err(|e| {
        let fix = if e.contains("\nTo fix:") { "" } else {
            "\nTo fix: Correct the plugin declaration or embedded icons, rebuild, and retry."
        };
        format!("validating manifest for {}: {e}{fix}", path.display())
    })?;
    atomic_write(mode, &path, text.as_bytes())?;
    if let Some(resources) = resources {
        let _ = resources.keep();
    }
    Ok(path)
}

/// Only consider directories referenced by the currently published, valid
/// manifest. Compare all required bytes, not just names or file sizes, and do
/// not follow a resource path outside its generation. Never modify reused files.
fn reusable_icons(
    manifest: &Path,
    directory: &Path,
    icons: &BTreeMap<String, &[u8]>,
) -> Option<String> {
    let mut old = crate::manifest::Manifest::load(manifest)
        .ok()?
        .value()
        .clone();
    let mut names = BTreeSet::new();
    visit_icons(&mut old, &mut |icon| {
        let portable = icon.replace('\\', "/");
        if let Some((name, _)) = portable.split_once('/') {
            if name.starts_with(ICON_PREFIX) {
                names.insert(name.to_owned());
            }
        }
        Ok(())
    })
    .ok()?;
    names.into_iter().find(|name| {
        let root = directory.join(name);
        if !fs::symlink_metadata(&root).is_ok_and(|m| m.is_dir()) {
            return false;
        }
        let Ok(root) = root.canonicalize() else {
            return false;
        };
        icons.iter().all(|(name, expected)| {
            let Ok(path) = root.join(name).canonicalize() else {
                return false;
            };
            if !path.starts_with(&root) {
                return false;
            }
            let Ok(file) = fs::File::open(path) else {
                return false;
            };
            if !file
                .metadata()
                .is_ok_and(|m| m.is_file() && m.len() == expected.len() as u64)
            {
                return false;
            }
            let mut bytes = Vec::new();
            file.take(65537).read_to_end(&mut bytes).is_ok() && bytes == *expected
        })
    })
}

fn visit_icons(value: &mut Value, f: &mut impl FnMut(&mut String) -> Result<()>) -> Result<()> {
    fn pane(value: &mut Value, f: &mut impl FnMut(&mut String) -> Result<()>) -> Result<()> {
        if let Some(Value::String(icon)) = value.get_mut("pane").and_then(|p| p.get_mut("icon")) {
            f(icon)?;
        }
        Ok(())
    }
    for kind in ["commands", "tools"] {
        if let Some(entries) = value["contributions"]
            .get_mut(kind)
            .and_then(Value::as_array_mut)
        {
            for entry in entries {
                pane(entry, f)?;
                if let Some(actions) = entry.get_mut("actions").and_then(Value::as_array_mut) {
                    for action in actions {
                        pane(action, f)?;
                    }
                }
            }
        }
    }
    Ok(())
}

pub(super) fn absolute(mode: Mode, path: &Path) -> Result<PathBuf> {
    std::path::absolute(path).map_err(|e| io_error(mode, "resolving path", path, e))
}

fn io_error(mode: Mode, operation: &str, path: &Path, error: std::io::Error) -> String {
    use std::io::ErrorKind;
    // A failed recursive mkdir can name several nonexistent descendants. Report
    // the existing ancestor whose permissions (or file type) need attention.
    let ancestor = path
        .ancestors()
        .find(|p| fs::metadata(p).is_ok())
        .unwrap_or(path);
    let flag = mode.flag();
    let cause = if matches!(
        error.kind(),
        ErrorKind::AlreadyExists | ErrorKind::NotADirectory
    ) && ancestor.is_file()
    {
        format!(
            "{} names a file; {flag} needs a directory",
            ancestor.display()
        )
    } else {
        io_cause(&error)
    };
    let fix = match error.kind() {
        ErrorKind::PermissionDenied => format!("Check permissions on {}; choose a writable directory with {flag} if needed.", ancestor.display()),
        ErrorKind::NotADirectory | ErrorKind::IsADirectory | ErrorKind::AlreadyExists => format!("Check the file or directory blocking {} before moving it aside, or choose another directory with {flag}.", ancestor.display()),
        ErrorKind::NotFound => format!("Restore the missing file or parent directory at {}, then retry; use {flag} to choose another destination.", path.display()),
        ErrorKind::StorageFull => format!("Free space on the disk containing {}, then retry.", ancestor.display()),
        ErrorKind::ReadOnlyFilesystem => format!("Choose a writable filesystem with {flag}; {} is on a read-only filesystem.", ancestor.display()),
        _ => format!("Check storage availability and permissions at {}, then retry; use {flag} to choose another destination.", ancestor.display()),
    };
    format!("{operation} {}: {cause}\nTo fix: {fix}", path.display())
}

fn lock_commands(path: &Path, windows: bool) -> String {
    let path = path.to_string_lossy();
    if windows {
        format!("\nTo fix: In cmd:\nTo fix: del \"{path}\"\nTo fix: Or in PowerShell:\nTo fix: Remove-Item -LiteralPath '{}'", path.replace('\'', "''"))
    } else {
        format!("\nTo fix: rm '{}'", path.replace('\'', "'\\''"))
    }
}

fn unpublished(error: String, directory: &Path) -> String {
    // Describe observable leftovers without listing private staging filenames
    // or claiming that an already-broken installation is intact.
    let state = match fs::symlink_metadata(directory) {
        Ok(m) if m.file_type().is_symlink() => {
            format!("The symlink remains at {}.", directory.display())
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            format!("No directory exists at {}.", directory.display())
        }
        _ => match fs::read_dir(directory) {
            Ok(mut entries) => {
                if entries.next().is_none() {
                    format!("An empty directory remains at {}.", directory.display())
                } else {
                    format!(
                        "Directory contents remain at {}; they were not rolled back.",
                        directory.display()
                    )
                }
            }
            Err(_) => format!(
                "Could not inspect the contents at {}; check this path before retrying.",
                directory.display()
            ),
        },
    };
    format!("{error}\nState: No new manifest was published; any previous manifest is unchanged. {state}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lock_commands_quote_spaces_and_apostrophes_for_each_shell() {
        assert_eq!(
            lock_commands(Path::new("/tmp/owner's plugins/.lock"), false),
            "\nTo fix: rm '/tmp/owner'\\''s plugins/.lock'"
        );
        assert_eq!(lock_commands(Path::new(r"C:\owner's plugins\.lock"), true),
            "\nTo fix: In cmd:\nTo fix: del \"C:\\owner's plugins\\.lock\"\nTo fix: Or in PowerShell:\nTo fix: Remove-Item -LiteralPath 'C:\\owner''s plugins\\.lock'");
    }
}
