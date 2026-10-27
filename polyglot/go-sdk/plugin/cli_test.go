package plugin

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"strings"
	"testing"
	"time"
)

func TestCLIRealEntry(t *testing.T) {
	// Build a real executable: a go test executable does not carry the ordinary
	// build settings that Install checks. All children inherit a scratch scan root.
	// Relative install arguments resolve against the child's physical working
	// directory. Use that same spelling for all fixture paths and assertions
	// (macOS commonly aliases /var to /private/var).
	scratch, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	source := filepath.Join(scratch, "main.go")
	code := `package main
import("context"; "os"; "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin")
func main() {
 info := plugin.Info{ID:"org.example.cli", Name:"CLI test", Version:"1.2.3"}
 d := plugin.NewDefinition(info)
 d.Resource("support.txt", []byte("new support"))
 a, err := d.Build()
 if err != nil { os.Exit(plugin.DeclarationExitCode(info, os.Args[1:], err, plugin.RunOptions{})) }
 os.Exit(a.RunCLI(context.Background(), os.Args[1:], plugin.RunOptions{}))
}`
	if err := os.WriteFile(source, []byte(code), 0600); err != nil {
		t.Fatal(err)
	}
	build := func(cgo, tag string, flags ...string) string {
		t.Helper()
		binary := filepath.Join(scratch, "cli"+tag+".exe")
		ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
		defer cancel()
		buildArgs := append([]string{"build", "-o", binary}, flags...)
		buildArgs = append(buildArgs, source)
		cmd := exec.CommandContext(ctx, "go", buildArgs...)
		cmd.Env = append(os.Environ(), "CGO_ENABLED="+cgo)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("build: %v\n%s", err, out)
		}
		return binary
	}
	pure := build("0", "pure")
	root := filepath.Join(scratch, "plugins with spaces")
	workingDir := filepath.Join(scratch, "unrelated working directory")
	if err := os.Mkdir(workingDir, 0755); err != nil {
		t.Fatal(err)
	}
	run := func(binary string, want int, args ...string) (string, string) {
		t.Helper()
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		cmd := exec.CommandContext(ctx, binary, args...)
		cmd.Dir = workingDir
		cmd.Env = append(os.Environ(), "VIZI_PLUGINS_DIR="+root)
		var out, stderr bytes.Buffer
		cmd.Stdout, cmd.Stderr = &out, &stderr
		err := cmd.Run()
		got := 0
		if err != nil {
			var exit *exec.ExitError
			if !errors.As(err, &exit) {
				t.Fatal(err)
			}
			got = exit.ExitCode()
		}
		if got != want {
			t.Fatalf("%v exit=%d want=%d\n%s%s", args, got, want, &out, &stderr)
		}
		if want != 0 {
			if out.Len() != 0 {
				t.Fatalf("failure wrote stdout: %s", &out)
			}
			for _, text := range []string{"FAILED: org.example.cli 1.2.3", "Why:", "\n\nTo fix:\n"} {
				if !strings.Contains(stderr.String(), text) {
					t.Fatalf("missing %q: %s", text, &stderr)
				}
			}
			if strings.Contains(stderr.String(), ".vizi-write-") {
				t.Fatalf("staging filename leaked: %s", &stderr)
			}
		} else if stderr.Len() != 0 {
			t.Fatalf("success wrote stderr: %s", &stderr)
		}
		return out.String(), stderr.String()
	}
	for _, tc := range []struct {
		args        []string
		reason, fix string
	}{
		{[]string{"--install", "some dir"}, "misplaced argument some dir", "--install=DIR"},
		{[]string{"--install", ""}, "--install followed by an empty argument: the folder given is empty", "--install=DIR"},
		{[]string{"--manifest", ""}, "--manifest followed by an empty argument: the folder given is empty", "--manifest=DIR"},
		{[]string{"--install="}, "--install=: the folder given is empty", "--install=DIR"},
		{[]string{"--install", "--wat"}, "unknown argument --wat", "--help"},
		{[]string{"--help", "--wat"}, "unknown argument --wat", "--help"},
		{[]string{"--install", "--manifest"}, "misplaced option --manifest", "--help"},
		{[]string{"--force"}, "misplaced option --force", "--install"},
		{[]string{"--manifest", "some dir"}, "misplaced argument some dir", "--manifest=DIR"},
		{[]string{"--manifest="}, "--manifest=: the folder given is empty", "--manifest=DIR"},
	} {
		_, got := run(pure, 1, tc.args...)
		if !strings.Contains(got, tc.reason) || !strings.Contains(got, tc.fix) {
			t.Fatal(got)
		}
		if strings.Contains(got, root) || strings.Contains(got, "destination unavailable") || strings.Contains(got, "Why: check command line ") {
			t.Fatal("usage invented a destination:", got)
		}
		if strings.Contains(got, "for example '--install='") || strings.Contains(got, "for example '--manifest='") {
			t.Fatal("empty fix:", got)
		}
	}
	if _, err := os.Stat(root); !os.IsNotExist(err) {
		t.Fatalf("usage wrote install root: %v", err)
	}
	dir := filepath.Join(root, "org.example.cli")
	if err := os.MkdirAll(dir, 0755); err != nil {
		t.Fatal(err)
	}
	lock := filepath.Join(dir, ".vizi-install-lock")
	if err := os.WriteFile(lock, []byte("held"), 0600); err != nil {
		t.Fatal(err)
	}
	_, got := run(pure, 1, "--install")
	if !strings.Contains(got, "another installer holds the lock") || !strings.Contains(got, removeCommand(lock)) {
		t.Fatal(got)
	}
	relativeRoot, err := filepath.Rel(workingDir, root)
	if err != nil {
		t.Fatal(err)
	}
	_, relativeReport := run(pure, 1, "--install="+relativeRoot)
	for _, report := range []string{got, relativeReport} {
		if !strings.Contains(report, "Why: create install lock "+lock+":") || !strings.Contains(report, "The existing lock remains at "+lock+".") || !strings.Contains(report, removeCommand(lock)) {
			t.Fatal(report)
		}
	}
	if b, err := os.ReadFile(lock); err != nil || string(b) != "held" {
		t.Fatal("other installer's lock changed", err)
	}
	if err := os.Remove(lock); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(dir, "user.txt")
	if err := os.WriteFile(marker, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	_, got = run(pure, 1, "--install")
	if !strings.Contains(got, "not created by this installer") || !strings.Contains(got, "--force") {
		t.Fatal(got)
	}
	out, _ := run(pure, 0, "--install", "--force")
	if !strings.Contains(out, "INSTALLED: org.example.cli 1.2.3 at "+dir) {
		t.Fatal(out)
	}
	manifestPath := filepath.Join(dir, "vizi-plugin.json")
	old, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	// Failing a late package write keeps the previously published manifest.
	if err := os.Remove(filepath.Join(dir, "support.txt")); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(dir, "support.txt"), 0755); err != nil {
		t.Fatal(err)
	}
	_, got = run(pure, 1, "--install")
	if !strings.Contains(got, "write package file "+filepath.Join(dir, "support.txt")) {
		t.Fatal(got)
	}
	if b, err := os.ReadFile(manifestPath); err != nil || !bytes.Equal(b, old) {
		t.Fatal("failed update changed manifest", err)
	}
	if _, err := os.Stat(lock); !os.IsNotExist(err) {
		t.Fatal("left our lock", err)
	}
	// Real permission errors on Unix; Windows ACL behavior is covered by native
	// execution of the other cases, rather than pretending chmod changes its ACL.
	if runtime.GOOS != "windows" {
		denied := filepath.Join(scratch, "denied")
		if err := os.Mkdir(denied, 0500); err != nil {
			t.Fatal(err)
		}
		defer os.Chmod(denied, 0700)
		probe, err := os.Create(filepath.Join(denied, "probe"))
		if err == nil {
			probe.Close()
			t.Log("permission cases skipped: process can write mode 0500 directories")
		} else {
			_, got = run(pure, 1, "--install="+filepath.Join(denied, "sub", "deeper"))
			if !strings.Contains(got, "permission denied") || strings.Contains(got, "another installer holds") || !strings.Contains(got, "Check permissions on directory "+denied+";") || !strings.Contains(got, "Nothing was written.") || strings.Contains(got, "--manifest") {
				t.Fatal(got)
			}
			_, got = run(pure, 1, "--manifest="+denied)
			if !strings.HasPrefix(got, "MANIFEST FAILED:") || !strings.Contains(got, denied) || strings.Contains(got, "--install") {
				t.Fatal(got)
			}
			// Failure to create a lock is not an existing lock.
			if err := os.Chmod(dir, 0500); err != nil {
				t.Fatal(err)
			}
			defer os.Chmod(dir, 0755)
			_, got = run(pure, 1, "--install")
			if !strings.Contains(got, "create install lock "+lock) || strings.Contains(got, "another installer holds") || !strings.Contains(got, "Nothing was written.") {
				t.Fatal(got)
			}
		}
	}
	// The runtime's cgo setting is a real build property, not an installer mock.
	cgo := build("1", "cgo")
	_, got = run(cgo, 1, "--install")
	for _, text := range []string{"CGO_ENABLED=1", "CGO_ENABLED=0 go run . --install", "PowerShell:", "cmd:", "Nothing was written."} {
		if !strings.Contains(got, text) {
			t.Fatal(got)
		}
	}

	if strings.Contains(got, "--install=DIR") || strings.Contains(got, "Why: check executable build ") {
		t.Fatal(got)
	}
	explicit := "--install=" + filepath.Join(scratch, "my plugins")
	_, got = run(cgo, 1, explicit, "--force")
	for _, text := range []string{"CGO_ENABLED=0 go run . '" + explicit + "' --force", `go run . "` + explicit + `" --force`} {
		if !strings.Contains(got, text) {
			t.Fatal(got)
		}
	}
	// A file is not a manifest directory; report that conflict and the matching flag.
	_, got = run(pure, 1, "--manifest="+marker)
	for _, text := range []string{"names a file", "--manifest=DIR needs a directory", "Nothing was written."} {
		if !strings.Contains(got, text) {
			t.Fatal(got)
		}
	}
	if strings.Contains(got, "--install") {
		t.Fatal(got)
	}
	for _, kind := range []string{"file", "symlink"} {
		badRoot := filepath.Join(scratch, "bad-"+kind)
		bin := filepath.Join(badRoot, "org.example.cli", "bin")
		if err := os.MkdirAll(filepath.Dir(bin), 0755); err != nil {
			t.Fatal(err)
		}
		if kind == "file" {
			if err := os.WriteFile(bin, []byte("conflict"), 0600); err != nil {
				t.Fatal(err)
			}
		} else if err := os.Symlink(t.TempDir(), bin); err != nil {
			t.Logf("symlink unavailable: %v", err)
			continue
		}
		_, got = run(pure, 1, "--install="+badRoot, "--force")
		if !strings.Contains(got, "Move or remove") || !strings.Contains(got, bin) || strings.Contains(got, "--manifest") {
			t.Fatal(got)
		}
	}
	noInfo := build("0", "noinfo", "-ldflags=-X runtime.modinfo=")
	_, got = run(noInfo, 1, "--install")
	if !strings.Contains(got, "build metadata is unavailable") || strings.Contains(got, "built with CGO_ENABLED=") {
		t.Fatal(got)
	}
	// Inject a failed home lookup using a compiler overlay. HOME is never unset,
	// and the lookup cannot fall back to the real user's directory on any OS.
	cwd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	cliPath := filepath.Join(cwd, "cli.go")
	cliSource, err := os.ReadFile(cliPath)
	if err != nil {
		t.Fatal(err)
	}
	replaced := strings.Replace(string(cliSource), "os.UserHomeDir()", `func() (string,error) { return "", errors.New("home directory lookup unavailable (test)") }()`, 1)
	// Ignore the scan-root lookup only in this overlay; the child still has a
	// scratch VIZI_PLUGINS_DIR and a real HOME, and the home lookup always fails.
	replaced = strings.Replace(replaced, `dir = os.Getenv("VIZI_PLUGINS_DIR")`, `dir = ""`, 1)
	overlaySource := filepath.Join(scratch, "cli.go")
	if err := os.WriteFile(overlaySource, []byte(replaced), 0600); err != nil {
		t.Fatal(err)
	}
	overlay, err := json.Marshal(map[string]any{"Replace": map[string]string{cliPath: overlaySource}})
	if err != nil {
		t.Fatal(err)
	}
	overlayPath := filepath.Join(scratch, "overlay.json")
	if err := os.WriteFile(overlayPath, overlay, 0600); err != nil {
		t.Fatal(err)
	}
	noHome := build("0", "nohome", "-overlay="+overlayPath)
	_, got = run(noHome, 1, "--install")
	if strings.Contains(got, "destination unavailable") || strings.Contains(strings.SplitN(got, "\n", 2)[0], " at ") {
		t.Fatal(got)
	}
	for _, text := range []string{"find the default install directory", "home directory lookup unavailable (test)", "--install=DIR", "VIZI_PLUGINS_DIR", "Nothing was written."} {
		if !strings.Contains(got, text) {
			t.Fatal(got)
		}
	}

	// A later mkdir refusal must retain the precise partial-write state.
	partialSource := strings.Replace(string(cliSource), "os.Mkdir(missing[i], 0755)", `func() error { if filepath.Base(missing[i]) == "stop" { return os.ErrPermission }; return os.Mkdir(missing[i],0755) }()`, 1)
	if err := os.WriteFile(overlaySource, []byte(partialSource), 0600); err != nil {
		t.Fatal(err)
	}
	partial := build("0", "partial", "-overlay="+overlayPath)
	partialRoot := filepath.Join(scratch, "partial")
	_, got = run(partial, 1, "--install="+filepath.Join(partialRoot, "stop", "after"))
	if !strings.Contains(got, "Directories were created at "+partialRoot) || strings.Contains(got, "Nothing was written.") {
		t.Fatal(got)
	}
	if _, err := os.Stat(partialRoot); err != nil {
		t.Fatal(err)
	}

	// A cleanup error must not silently report success, or claim the manifest
	// was untouched when it was already published. Fault only the lock removal.
	packagePath := filepath.Join(cwd, "package.go")
	packageSource, err := os.ReadFile(packagePath)
	if err != nil {
		t.Fatal(err)
	}
	replacedPackage := strings.Replace(string(packageSource), `root.Remove(".vizi-install-lock")`, `func() error { return os.ErrPermission }()`, 1)
	packageOverlay := filepath.Join(scratch, "package.go")
	if err := os.WriteFile(packageOverlay, []byte(replacedPackage), 0600); err != nil {
		t.Fatal(err)
	}
	overlay, err = json.Marshal(map[string]any{"Replace": map[string]string{packagePath: packageOverlay}})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(overlayPath, overlay, 0600); err != nil {
		t.Fatal(err)
	}
	cleanup := build("0", "cleanup", "-overlay="+overlayPath)
	cleanupRoot := filepath.Join(scratch, "cleanup")
	_, got = run(cleanup, 1, "--install="+cleanupRoot)
	cleanupLock := filepath.Join(cleanupRoot, "org.example.cli", ".vizi-install-lock")
	if !strings.Contains(got, "The package was installed at") || !strings.Contains(got, "Why: remove install lock "+cleanupLock+":") || !strings.Contains(got, "The lock remains at "+cleanupLock+".") || !strings.Contains(got, removeCommand(cleanupLock)) || strings.Contains(got, "manifest was not replaced") {
		t.Fatal(got)
	}
	if _, err := os.Stat(filepath.Join(cleanupRoot, "org.example.cli", "vizi-plugin.json")); err != nil {
		t.Fatal(err)
	}

}

func TestCLIUnavailableBuildMetadata(t *testing.T) {
	for _, tc := range []struct {
		info *debug.BuildInfo
		ok   bool
		text string
	}{
		{nil, false, "build metadata is unavailable"},
		{&debug.BuildInfo{}, true, "does not record CGO_ENABLED"},
	} {
		err := checkInstallBuild(tc.info, tc.ok)
		a := buildDefinition(t, definition())
		report := a.cliFailure([]string{"--install=" + t.TempDir()}, err).Error()
		if !strings.Contains(report, tc.text) || strings.Contains(report, "built with CGO_ENABLED=") || !strings.Contains(report, "\nTo fix:\n") {
			t.Fatal(report)
		}
	}
}

func TestCLIOutputFailureAfterPublish(t *testing.T) {
	a := buildDefinition(t, definition())
	var stderr bytes.Buffer
	dir := t.TempDir()
	code := a.RunCLI(context.Background(), []string{"--manifest=" + dir}, RunOptions{Output: brokenCLIWriter{}, ErrorOutput: &stderr})
	if code != 1 || !strings.Contains(stderr.String(), "MANIFEST FAILED:") || !strings.Contains(stderr.String(), "was written") {
		t.Fatal(code, stderr.String())
	}
	if _, err := os.Stat(filepath.Join(dir, "vizi-plugin.json")); err != nil {
		t.Fatal(err)
	}
}

type brokenCLIWriter struct{}

func (brokenCLIWriter) Write([]byte) (int, error) { return 0, errors.New("output is closed") }

func TestWindowsLockAdvice(t *testing.T) {
	path := `C:\Users\O'Brien\My plugins\.vizi-install-lock`
	got := removeCommandForOS(path, "windows")
	for _, text := range []string{`cmd: del "C:\Users\O'Brien\My plugins\.vizi-install-lock"`, `PowerShell: Remove-Item -LiteralPath 'C:\Users\O''Brien\My plugins\.vizi-install-lock'`} {
		if !strings.Contains(got, text) {
			t.Fatal(got)
		}
	}
	if strings.Contains(got, "rm --") {
		t.Fatal(got)
	}
}
