package plugin

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"strings"
)

// RunCLI is the terminal entry point for main: it writes Run's error to
// ErrorOutput without a timestamp and returns an exit code. It never exits the
// process itself, so callers can finish cleanup before passing the code to os.Exit.
func (a *Application) RunCLI(ctx context.Context, args []string, options RunOptions) int {
	if err := a.Run(ctx, args, options); err != nil {
		out := options.ErrorOutput
		if out == nil {
			out = os.Stderr
		}
		fmt.Fprintln(out, err)
		return 1
	}
	return 0
}

type packageFailure struct {
	cause                       error
	operation, path, fix, state string
}

func (e *packageFailure) Error() string {
	if e.path == "" {
		return e.operation + ": " + e.cause.Error()
	}
	return e.operation + " " + e.path + ": " + e.cause.Error()
}
func (e *packageFailure) Unwrap() error { return e.cause }

func packageIO(operation, path string, err error) error {
	var known *packageFailure
	if errors.As(err, &known) {
		return err
	}
	return &packageFailure{cause: err, operation: operation, path: path}
}
func usageFailure(reason, fix string) error {
	return &packageFailure{cause: errors.New(reason), operation: "check command line", fix: fix, state: "Nothing was written."}
}

func installDirectory(dir string) (string, error) {
	if dir == "" {
		dir = os.Getenv("VIZI_PLUGINS_DIR")
	}
	if dir == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", &packageFailure{cause: err, operation: "find the default install directory", fix: "Choose a directory with --install=DIR, or set VIZI_PLUGINS_DIR to your plugins directory.", state: "Nothing was written."}
		}
		dir = filepath.Join(home, ".vizi", "plugins")
	}
	full, err := filepath.Abs(dir)
	if err != nil {
		return "", packageIO("resolve install directory", dir, err)
	}
	return full, nil
}

func checkInstallBuild(info *debug.BuildInfo, ok bool) error {
	cause := "build metadata is unavailable; the installer cannot verify that native libraries are unnecessary"
	if ok && info != nil {
		cause = "build metadata does not record CGO_ENABLED; the installer cannot verify that native libraries are unnecessary"
		for _, setting := range info.Settings {
			if setting.Key == "CGO_ENABLED" {
				if setting.Value == "0" {
					return nil
				}
				cause = "this executable was built with CGO_ENABLED=" + setting.Value + "; the installer does not package native libraries"
			}
		}
	}
	return &packageFailure{cause: errors.New(cause), operation: "check executable build", state: "Nothing was written.", fix: buildFix([]string{"--install"})}
}

func cliDestination(args []string) (mode, dir string) {
	mode = "COMMAND"
	for i, arg := range args {
		for _, flag := range []string{"--install", "--manifest"} {
			if arg == flag || strings.HasPrefix(arg, flag+"=") {
				mode = strings.ToUpper(strings.TrimPrefix(flag, "--"))
				if _, value, ok := strings.Cut(arg, "="); ok {
					dir = value
				} else if i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
					dir = args[i+1]
				}
				return
			}
		}
	}
	return
}

// Strip filesystem wrappers only while rendering: error chains remain available
// to programmatic callers, and staging filenames never become terminal advice.
func plainCause(err error) string {
	switch e := err.(type) {
	case *packageFailure:
		return operationPath(e.operation, e.path) + ": " + plainCause(e.cause)
	case *fs.PathError:
		return plainCause(e.Err)
	case *os.LinkError:
		return plainCause(e.Err)
	case interface{ Unwrap() []error }:
		var parts []string
		for _, child := range e.Unwrap() {
			parts = append(parts, plainCause(child))
		}
		return strings.Join(parts, "; ")
	default:
		return strings.NewReplacer("\r", " ", "\n", " ").Replace(err.Error())
	}
}

type cliError struct {
	report string
	cause  error
}

func (e *cliError) Error() string { return e.report }
func (e *cliError) Unwrap() error { return e.cause }

func (a *Application) cliFailure(args []string, err error) error {
	mode, dir := cliDestination(args)
	var detail *packageFailure
	errors.As(err, &detail)
	usage := detail != nil && detail.operation == "check command line"
	target := ""
	// For usage errors, only an explicitly supplied folder is meaningful.
	if dir != "" || !usage {
		if mode == "INSTALL" {
			if root, resolveErr := installDirectory(dir); resolveErr == nil {
				target = filepath.Join(root, a.m.ID)
			}
		} else if mode == "MANIFEST" {
			if dir == "" {
				dir = "."
			}
			if full, resolveErr := filepath.Abs(dir); resolveErr == nil {
				target = filepath.Join(full, "vizi-plugin.json")
			}
		}
	}
	operation, location := "prepare "+strings.ToLower(mode), ""
	fix, state := "", ""
	cause := err
	if detail != nil {
		operation, location = detail.operation, detail.path
		fix, state, cause = detail.fix, detail.state, detail.cause
		if operation == "check executable build" {
			fix = buildFix(args)
		}
	} else {
		var pe *fs.PathError
		if errors.As(err, &pe) {
			operation, location = pe.Op, pe.Path
		}
	}
	if fix == "" {
		fix = filesystemFix(mode, operation, location, cause)
	}
	if state == "" {
		packageDir := target
		if mode == "MANIFEST" && target != "" {
			packageDir = filepath.Dir(target)
		}
		state = "The manifest was not replaced."
		if packageDir != "" {
			state += " Files or directories prepared before the failure may remain at " + packageDir + "."
		}
	}
	headline := strings.TrimSpace(mode + " FAILED: " + a.m.ID + " " + a.m.Version)
	if target != "" {
		headline += " at " + target
	}
	return &cliError{cause: err, report: fmt.Sprintf("%s\nWhy: %s: %s\n%s\n\nTo fix:\n%s", headline, operationPath(operation, location), plainCause(cause), state, fix)}
}

func operationPath(operation, path string) string {
	if path == "" {
		return operation
	}
	return operation + " " + path
}

func filesystemFix(mode, operation, path string, err error) string {
	flag := "--" + strings.ToLower(mode) + "=DIR"
	if mode != "INSTALL" && mode != "MANIFEST" {
		return "Check the cause above and use --help for usage."
	}
	if errors.Is(err, fs.ErrPermission) {
		dir := nearestDirectory(path)
		return "Check permissions on directory " + dir + "; access was denied. Retry with " + flag + " pointing to a directory you can write to."
	}
	if operation == "create directory" {
		if st, statErr := os.Stat(path); statErr == nil && !st.IsDir() {
			return "The path " + path + " names a file; " + flag + " needs a directory. Choose a directory, or move the file before retrying."
		}
	}
	if operation == "create package directory" || operation == "write package file" {
		if st, statErr := os.Lstat(path); statErr == nil && (st.Mode()&os.ModeSymlink != 0 || (operation == "create package directory" && !st.IsDir()) || (operation == "write package file" && !st.Mode().IsRegular())) {
			return "Move or remove the conflicting file, directory or symbolic link at " + path + ", then retry; or choose another destination with " + flag + "."
		}
	}
	return "Check the path and available disk space; retry with " + flag + " pointing to a writable directory. Use --help for usage."
}

// Walk past missing components (and existing files) to the actual directory
// whose permissions the user can change. Do not suggest chmod on a future path.
func nearestDirectory(path string) string {
	for {
		if st, err := os.Stat(path); err == nil && st.IsDir() {
			return path
		}
		parent := filepath.Dir(path)
		if parent == path {
			return path
		}
		path = parent
	}
}

func buildFix(args []string) string {
	flag := "--install"
	force := ""
	for _, arg := range args {
		if arg == "--install" || strings.HasPrefix(arg, "--install=") {
			flag = arg
		}
		if arg == "--force" {
			force = " --force"
		}
	}
	return "From the plugin source directory, rebuild and install:\n" +
		"  Linux/macOS (sh): CGO_ENABLED=0 go run . " + shellArgument(flag, "sh") + force + "\n" +
		"  PowerShell: $env:CGO_ENABLED=\"0\"; go run . " + shellArgument(flag, "powershell") + force + "\n" +
		"  cmd: set CGO_ENABLED=0\n       go run . " + shellArgument(flag, "cmd") + force
}

func shellArgument(arg, shell string) string {
	safe := "-_=./:"
	if shell != "sh" {
		safe += "\\"
	}
	if strings.IndexFunc(arg, func(r rune) bool {
		return !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || strings.ContainsRune(safe, r))
	}) < 0 {
		return arg
	}
	switch shell {
	case "powershell":
		return "'" + strings.ReplaceAll(arg, "'", "''") + "'"
	case "cmd":
		return `"` + strings.ReplaceAll(arg, `"`, `""`) + `"`
	default:
		return "'" + strings.ReplaceAll(arg, "'", "'\"'\"'") + "'"
	}
}

func quoteArgument(arg string) string {
	if runtime.GOOS == "windows" {
		return `"` + strings.ReplaceAll(arg, `"`, `""`) + `"`
	}
	return "'" + strings.ReplaceAll(arg, "'", "'\"'\"'") + "'"
}
func removeCommand(path string) string {
	return removeCommandForOS(path, runtime.GOOS)
}

func removeCommandForOS(path, goos string) string {
	if goos == "windows" {
		return "  PowerShell: Remove-Item -LiteralPath '" + strings.ReplaceAll(path, "'", "''") + "'\n  cmd: del " + `"` + path + `"`
	}
	return "  rm -- " + quoteArgument(path)
}

// DeclarationExitCode reports a declaration failure before an Application exists.
// Pass the same Info used by NewDefinition so install failures still name the
// plugin. A nil error returns zero. It does not start a session or write files.
func DeclarationExitCode(info Info, args []string, err error, options RunOptions) int {
	if err == nil {
		return 0
	}
	a := &Application{}
	a.m.ID, a.m.Version = info.ID, info.Version
	report := a.cliFailure(args, &packageFailure{cause: err, operation: "validate plugin declaration", state: "Nothing was written.", fix: "Correct the plugin declaration in the source and rebuild; use --help for command-line usage."})
	out := options.ErrorOutput
	if out == nil {
		out = os.Stderr
	}
	fmt.Fprintln(out, report)
	return 1
}

// Keep the original error chain and any more precise state supplied by the
// failing operation. This is only called while no write has succeeded.
func nothingWritten(err error) error {
	if err == nil {
		return nil
	}
	var detail *packageFailure
	if errors.As(err, &detail) {
		if detail.state != "" {
			return err
		}
		copy := *detail
		copy.state = "Nothing was written."
		return &copy
	}
	return &packageFailure{cause: err, operation: "prepare package", state: "Nothing was written."}
}

// Unlike MkdirAll, record whether any component was actually created. A failed
// first mkdir and a failure after creating parent directories need different
// reports. Existing symlinked root directories retain MkdirAll's behavior.
func makePackageDirectory(dir string) (created bool, err error) {
	firstCreated := ""
	defer func() {
		if created && err != nil {
			var detail *packageFailure
			if errors.As(err, &detail) {
				copy := *detail
				copy.state = "Directories were created at " + firstCreated + ". No package files were written."
				err = &copy
			}
		}
	}()
	var missing []string
	cursor := dir
	for {
		st, statErr := os.Stat(cursor)
		if statErr == nil {
			if !st.IsDir() {
				return false, packageIO("create directory", cursor, errors.New("path names a file, not a directory"))
			}
			break
		}
		if !errors.Is(statErr, fs.ErrNotExist) {
			return false, packageIO("create directory", cursor, statErr)
		}
		missing = append(missing, cursor)
		parent := filepath.Dir(cursor)
		if parent == cursor {
			return false, packageIO("create directory", cursor, statErr)
		}
		cursor = parent
	}
	for i := len(missing) - 1; i >= 0; i-- {
		if mkdirErr := os.Mkdir(missing[i], 0755); mkdirErr != nil {
			if errors.Is(mkdirErr, fs.ErrExist) {
				if st, statErr := os.Stat(missing[i]); statErr == nil && st.IsDir() {
					continue
				}
			}
			return created, packageIO("create directory", missing[i], mkdirErr)
		}
		created = true
		if firstCreated == "" {
			firstCreated = missing[i]
		}
	}
	return created, nil
}
