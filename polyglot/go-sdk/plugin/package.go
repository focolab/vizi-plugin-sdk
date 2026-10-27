package plugin

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"slices"
	"strings"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
)

type resourceFS map[string][]byte

func (r resourceFS) Open(name string) (fs.File, error) {
	b, ok := r[name]
	if !ok {
		return nil, &fs.PathError{Op: "open", Path: name, Err: fs.ErrNotExist}
	}
	return &resourceFile{Reader: *bytes.NewReader(b), name: name, size: int64(len(b))}, nil
}

type resourceFile struct {
	bytes.Reader
	name string
	size int64
}

func (f *resourceFile) Close() error               { return nil }
func (f *resourceFile) Stat() (fs.FileInfo, error) { return resourceInfo{f.name, f.size}, nil }

type resourceInfo struct {
	name string
	size int64
}

func (i resourceInfo) Name() string       { return path.Base(i.name) }
func (i resourceInfo) Size() int64        { return i.size }
func (i resourceInfo) Mode() fs.FileMode  { return 0644 }
func (i resourceInfo) ModTime() time.Time { return time.Time{} }
func (i resourceInfo) IsDir() bool        { return false }
func (i resourceInfo) Sys() any           { return nil }

// WriteManifest writes the declaration and packaged resources to dir. With no
// command it names the current executable by absolute path: use a durable go
// build output, since go run's executable may disappear. It never starts a host
// connection. Files are individually staged; the manifest is published last.
func (a *Application) WriteManifest(dir string, command ...string) (result string, writeErr error) {
	changed := false
	defer func() {
		if !changed {
			writeErr = nothingWritten(writeErr)
		}
	}()
	if len(command) == 0 {
		exe, err := os.Executable()
		if err != nil {
			return "", err
		}
		command = []string{exe}
	}
	b, err := a.Manifest(command...)
	if err != nil {
		return "", err
	}
	dir, err = filepath.Abs(dir)
	if err != nil {
		return "", err
	}
	if changed, err = makePackageDirectory(dir); err != nil {
		return "", err
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return "", packageIO("open directory", dir, err)
	}
	defer root.Close()
	changed = true // Resource and manifest writes may now change package files.
	if err = writeResources(root, dir, a.resources); err != nil {
		return "", err
	}
	if err = writePackageFile(root, dir, "vizi-plugin.json", b, 0644); err != nil {
		return "", err
	}
	return filepath.Join(dir, "vizi-plugin.json"), nil
}

// InstallOptions selects the host's scan directory. Empty Directory means
// VIZI_PLUGINS_DIR, otherwise ~/.vizi/plugins. Force allows writing to a nonempty
// plugin directory not marked by this installer; unrelated files are retained.
type InstallOptions struct {
	Directory string
	Force     bool
}

const installMarker = ".vizi-go-sdk"

// Install copies this executable, resources and generated manifest together.
// This initial installer requires CGO_ENABLED=0 build metadata; it does not
// discover or package native libraries. Runtime data files must be Resource or
// Asset declarations. The binary is native to the OS/architecture running it.
// Executables get content-addressed paths, so updates never overwrite a running
// Windows executable. Old binaries are retained; stop plugins before removing
// old bin/ entries manually. Running sessions are not restarted by installation.
func (a *Application) Install(options InstallOptions) (result string, installErr error) {
	started := false
	defer func() {
		if !started {
			installErr = nothingWritten(installErr)
		}
	}()
	info, ok := debug.ReadBuildInfo()
	if err := checkInstallBuild(info, ok); err != nil {
		return "", err
	}
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	source := exe
	exe, err = filepath.EvalSymlinks(exe)
	if err != nil {
		return "", packageIO("resolve executable", source, err)
	}
	f, err := os.Open(exe)
	if err != nil {
		return "", packageIO("open executable", exe, err)
	}
	defer f.Close()
	stat, err := f.Stat()
	if err != nil {
		return "", packageIO("inspect executable", exe, err)
	}
	if !stat.Mode().IsRegular() {
		return "", packageIO("read executable", exe, errors.New("not a regular file"))
	}
	binary, err := io.ReadAll(f)
	if err != nil {
		return "", packageIO("read executable", exe, err)
	}
	started = true
	return a.installBinary(options, binary, runtime.GOOS)
}
func (a *Application) installBinary(options InstallOptions, binary []byte, goos string) (installed string, installErr error) {
	changed := false
	defer func() {
		if !changed {
			installErr = nothingWritten(installErr)
		}
	}()
	if !manifest.LocalPath(a.m.ID) || strings.Contains(a.m.ID, "/") {
		return "", &packageFailure{cause: errors.New("plugin ID is not one portable directory name"), operation: "check plugin ID for installation", state: "Nothing was written.", fix: "Use a plugin ID that is one portable directory name (no path separators or reserved names), then rebuild."}
	}
	dir, err := installDirectory(options.Directory)
	if err != nil {
		return "", err
	}
	if changed, err = makePackageDirectory(dir); err != nil {
		return "", err
	}
	parent, err := os.OpenRoot(dir)
	if err != nil {
		return "", packageIO("open install directory", dir, err)
	}
	defer parent.Close()
	created, err := packageParentsTracked(parent, dir, a.m.ID+"/file")
	changed = changed || created
	if err != nil {
		return "", err
	}
	packageDir := filepath.Join(dir, a.m.ID)
	root, err := parent.OpenRoot(a.m.ID)
	if err != nil {
		return "", packageIO("open plugin directory", filepath.Join(dir, a.m.ID), err)
	}
	defer root.Close()
	// Serialize cooperating installers to this package.
	lock, err := root.OpenFile(".vizi-install-lock", os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		if errors.Is(err, fs.ErrExist) {
			return "", &packageFailure{cause: fmt.Errorf("another installer holds the lock: %w", fs.ErrExist), state: "No package files were changed. The existing lock remains at " + filepath.Join(packageDir, ".vizi-install-lock") + ".", operation: "create install lock", path: filepath.Join(packageDir, ".vizi-install-lock"), fix: "Wait for the other installer to finish. If it was interrupted, remove this lock and retry:\n" + removeCommand(filepath.Join(packageDir, ".vizi-install-lock"))}
		}
		return "", packageIO("create install lock", filepath.Join(packageDir, ".vizi-install-lock"), err)
	}
	changed = true // The lock was created, even if later cleanup removes it.
	defer func() {
		if cleanupErr := root.Remove(".vizi-install-lock"); cleanupErr != nil {
			lockPath := filepath.Join(packageDir, ".vizi-install-lock")
			state := "The manifest was not replaced; files prepared before the failure may remain at " + packageDir + "."
			if installed != "" {
				state = "The package was installed at " + installed + "."
			}
			installErr = &packageFailure{cause: errors.Join(installErr, cleanupErr), operation: "remove install lock", path: lockPath, state: state + " The lock remains at " + lockPath + ".", fix: "Check directory permissions, then remove this installer's lock before retrying:\n" + removeCommand(lockPath)}
		}
	}()
	if err = lock.Close(); err != nil {
		return "", packageIO("close install lock", filepath.Join(packageDir, ".vizi-install-lock"), err)
	}
	folder, err := root.Open(".")
	if err != nil {
		return "", packageIO("open plugin directory", packageDir, err)
	}
	entries, err := folder.ReadDir(-1)
	folder.Close()
	if err != nil {
		return "", packageIO("read plugin directory", packageDir, err)
	}
	nonempty := false
	for _, e := range entries {
		if e.Name() != ".vizi-install-lock" {
			nonempty = true
		}
	}
	marker, markerErr := root.ReadFile(installMarker)
	expected := []byte("vizi-go-sdk\n" + a.m.ID + "\n")
	if nonempty && (!bytes.Equal(marker, expected) || markerErr != nil) && !options.Force {
		return "", &packageFailure{cause: errors.New("directory was not created by this installer"), state: "No package files were changed.", operation: "install into", path: filepath.Join(dir, a.m.ID), fix: "Choose a new directory with --install=DIR, or, after checking its contents, retry with --force."}
	}
	digest := sha256.Sum256(binary)
	name := "plugin"
	if goos == "windows" {
		name += ".exe"
	}
	binaryPath := "bin/" + hex.EncodeToString(digest[:]) + "/" + name
	b, err := a.Manifest("./" + binaryPath)
	if err != nil {
		return "", err
	}
	// On failure the old manifest continues to name the old complete executable.
	if err = writePackageFile(root, packageDir, binaryPath, binary, 0755); err != nil {
		return "", err
	}
	if err = writeResources(root, packageDir, a.resources); err != nil {
		return "", err
	}
	if err = writePackageFile(root, packageDir, installMarker, expected, 0644); err != nil {
		return "", err
	}
	if err = writePackageFile(root, packageDir, "vizi-plugin.json", b, 0644); err != nil {
		return "", err
	}
	return filepath.Join(dir, a.m.ID), nil
}
func writeResources(root *os.Root, dir string, resources map[string][]byte) error {
	names := make([]string, 0, len(resources))
	for n := range resources {
		names = append(names, n)
	}
	slices.Sort(names)
	for _, n := range names {
		if err := writePackageFile(root, dir, n, resources[n], 0644); err != nil {
			return err
		}
	}
	return nil
}

// dir is the absolute directory used to open root. Use it for diagnostics:
// nested Root.Name values are relative on Go 1.25. Root confines all operations.
// Refuse symlinks as well, so a caller's existing
// alias cannot turn one resource into another resource or into the manifest.
func packageParents(root *os.Root, dir, name string) error {
	_, err := packageParentsTracked(root, dir, name)
	return err
}
func packageParentsTracked(root *os.Root, dir, name string) (created bool, err error) {
	parts := strings.Split(name, "/")
	for i := 1; i < len(parts); i++ {
		prefix := strings.Join(parts[:i], "/")
		if mkdirErr := root.Mkdir(prefix, 0755); mkdirErr == nil {
			created = true
		} else if !errors.Is(mkdirErr, fs.ErrExist) {
			return created, packageIO("create package directory", filepath.Join(dir, prefix), mkdirErr)
		}
		st, statErr := root.Lstat(prefix)
		if statErr != nil {
			return created, packageIO("inspect package directory", filepath.Join(dir, prefix), statErr)
		}
		if !st.IsDir() || st.Mode()&os.ModeSymlink != 0 {
			return created, packageIO("create package directory", filepath.Join(dir, prefix), errors.New("not a plain directory (files and symbolic links are refused)"))
		}
	}
	return created, nil
}
func writePackageFile(root *os.Root, dir, name string, b []byte, mode fs.FileMode) (err error) {
	defer func() {
		if err != nil {
			err = packageIO("write package file", filepath.Join(dir, name), err)
		}
	}()
	if err := packageParents(root, dir, name); err != nil {
		return err
	}
	if st, err := root.Lstat(name); err == nil {
		if !st.Mode().IsRegular() {
			return packageIO("write package file", filepath.Join(dir, name), errors.New("not a regular file (directories and symbolic links are refused)"))
		}
		old, err := root.ReadFile(name)
		if err != nil {
			return err
		}
		if bytes.Equal(old, b) {
			if runtime.GOOS != "windows" && st.Mode().Perm() != mode {
				f, err := root.OpenFile(name, os.O_RDONLY, 0)
				if err != nil {
					return err
				}
				err = f.Chmod(mode)
				return errors.Join(err, f.Close())
			}
			return nil
		}
	} else if !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	random := make([]byte, 16)
	if _, err := rand.Read(random); err != nil {
		return err
	}
	temp := path.Join(path.Dir(name), ".vizi-write-"+hex.EncodeToString(random))
	f, err := root.OpenFile(temp, os.O_CREATE|os.O_EXCL|os.O_WRONLY, mode)
	if err != nil {
		return err
	}
	defer func() {
		if cleanupErr := root.Remove(temp); cleanupErr != nil && !errors.Is(cleanupErr, fs.ErrNotExist) {
			err = &packageFailure{cause: errors.Join(err, cleanupErr), operation: "remove temporary file for", path: filepath.Join(dir, name), state: "A temporary file remains in " + filepath.Join(dir, path.Dir(name)) + ". Files already written were not rolled back.", fix: "Check directory permissions and available disk space before retrying."}
		}
	}()
	_, writeErr := f.Write(b)
	syncErr := f.Sync()
	closeErr := f.Close()
	if err = errors.Join(writeErr, syncErr, closeErr); err != nil {
		return err
	}
	// Rename has platform-specific atomicity guarantees. We never truncate the
	// existing destination and surface sharing/rename failures to the caller.
	return root.Rename(temp, name)
}

// RunOptions defers session construction until normal serving; manifest/install
// modes must not start application goroutines or demand the host environment.
type RunOptions struct {
	ErrorOutput io.Writer                      // nil means stderr; used by RunCLI
	Output      io.Writer                      // nil means stdout
	Runtime     func() (control.Plugin, error) // nil means the default runtime
}

// Run handles --manifest[=DIR], --install[=DIR], --force, and --help. No arguments
// serves normally. It returns errors to main; it never calls os.Exit. A bare
// --manifest writes in the current directory. Paths use the = form so a bare
// option cannot accidentally consume another flag.
func (a *Application) Run(ctx context.Context, args []string, options RunOptions) (runErr error) {
	defer func() {
		if runErr != nil && len(args) != 0 {
			runErr = a.cliFailure(args, runErr)
		}
	}()
	output := options.Output
	if output == nil {
		output = os.Stdout
	}
	mode, dir := "", ""
	force, help := false, false
	for i, arg := range args {
		switch {
		case arg == "--help":
			help = true
		case arg == "--force":
			force = true
		case arg == "--manifest" || strings.HasPrefix(arg, "--manifest=") || arg == "--install" || strings.HasPrefix(arg, "--install="):
			if mode != "" {
				return usageFailure("misplaced option "+arg+"; give exactly one of --manifest or --install", "Use --help to check the supported options.")
			}
			parts := strings.SplitN(arg, "=", 2)
			mode = parts[0]
			if len(parts) == 2 {
				if parts[1] == "" {
					return usageFailure(arg+": the folder given is empty", "Supply a directory with "+mode+"=DIR; use --help for usage.")
				}
				dir = parts[1]
			}
		default:
			if i > 0 && (args[i-1] == "--install" || args[i-1] == "--manifest") && !strings.HasPrefix(arg, "-") {
				if arg == "" {
					return usageFailure(args[i-1]+" followed by an empty argument: the folder given is empty", "Supply a non-empty directory with "+args[i-1]+"=DIR; use --help for usage.")
				}
				return usageFailure("misplaced argument "+arg+" after "+args[i-1], "Write "+args[i-1]+"=DIR (with =), for example "+quoteArgument(args[i-1]+"="+arg)+". Use --help for usage.")
			}
			return usageFailure("unknown argument "+arg, "Use --help to check the supported options.")
		}
	}
	if force && mode != "--install" {
		return usageFailure("misplaced option --force; it applies only to --install", "Use --install[=DIR] --force, or remove --force. Use --help for usage.")
	}
	if help {
		_, err := fmt.Fprintln(output, "--manifest[=DIR]  Write manifest and resources (default: current directory)\n--install[=DIR]   Copy this CGO_ENABLED=0 executable into vizi's plugins directory\n--force           Allow installation into an unmarked plugin directory\nNo arguments     Serve a host-launched plugin session")
		if err != nil {
			return &packageFailure{cause: err, operation: "write help", state: "Nothing was written to the package directory.", fix: "Check stdout or the output redirection, then retry --help."}
		}
		return nil
	}
	var result string
	var err error
	switch mode {
	case "--manifest":
		if dir == "" {
			dir = "."
		}
		result, err = a.WriteManifest(dir)
	case "--install":
		result, err = a.Install(InstallOptions{Directory: dir, Force: force})
	default:
		var base control.Plugin
		if options.Runtime != nil {
			base, err = options.Runtime()
			if err != nil {
				return err
			}
		}
		return a.ServeEnv(ctx, base)
	}
	if err != nil {
		return err
	}
	verb := "INSTALLED"
	if mode == "--manifest" {
		verb = "MANIFEST WRITTEN"
	}
	_, err = fmt.Fprintf(output, "%s: %s %s at %s\n", verb, a.m.ID, a.m.Version, result)
	if err != nil {
		return &packageFailure{cause: err, operation: "write success message for", path: result, state: "The package or manifest was written successfully at " + result + ".", fix: "Check stdout or the output redirection. The files are already written; inspect the destination before retrying."}
	}
	return nil
}
