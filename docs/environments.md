# Reproducible plugin environments

A Python plugin runs in an environment of its own, never in vizi's. vizi
provides the interpreter and the tool that builds the environment; the plugin
provides a setup script and the locks it installs. Installed from the catalog,
the plugin then needs nothing on the person's machine but vizi.

A plugin that ships a complete, self-contained build for every platform it
claims (a frozen executable, for example) needs none of this: see
[releasing](releasing.md), clause 3b.

## What vizi provides

When it installs or updates a plugin, and before every launch:

1. **The interpreter.** vizi reads `.python-version` at the plugin's root and
   installs that CPython minor version (for example `3.11`) for the plugin. It
   may choose the patch: do not depend on one. A minor newer than the running
   vizi can provide fails with a message asking the person to update vizi.
2. **The setup run.** vizi runs `scripts/bootstrap.py` on that interpreter,
   with the plugin folder as the working directory and **no arguments**.
3. **`uv` on `PATH`.** A pinned `uv` is first on `PATH` for the bootstrap and
   for every later launch of the plugin, with its cache and Python directories
   already set in the environment. A plugin may use it to build further
   environments lazily, at any time. Do not store its path, which changes
   between launches and vizi versions; do not unset or override the `UV_*`
   variables vizi sets; and do not fetch your own `uv` or your own Python.
   vizi's release notes state the `uv` version, and a change of it is
   announced as a compatibility event.
4. **A clean environment.** vizi removes `VIRTUAL_ENV`, `CONDA_PREFIX`,
   `CONDA_DEFAULT_ENV`, inherited `UV_*` and `PIP_*` variables, and the loader
   and `PYTHON*` variables before running a bootstrap or a plugin. Do not rely
   on any of them; [debugging](debugging.md#trust-environment-and-devices)
   has the full list.

## Repository layout

```text
vizi-plugin.json
pyproject.toml
.python-version
requirements/
  base.in
  cpu.in
  gpu.in
  locks/
    linux-x86_64-py311-cpu.txt
    linux-x86_64-py311-gpu.txt
    windows-x86_64-py311-cpu.txt
    macos-arm64-py311-cpu.txt
scripts/
  bootstrap.py
  doctor.py
```

Include only the lock targets the plugin supports: one lock per platform and
architecture, Python minor and compute profile. Do not reuse a Linux lock on
Windows or a CPU lock for a GPU install.

The invariants:

1. **`.python-version`** holds a CPython version as digits and dots: `3.11`
   or `3.11.13`. vizi uses the minor. Paths, ranges and names are refused.
2. **`pyproject.toml`** declares the tested Python range. The SDK supports
   Python 3.11–3.13; a backend may narrow it (TensorFlow 2.15 plugins
   commonly select only 3.11). Do not claim an untested minor.
3. **`.venv/`** is at the plugin root, ignored by version control, and is the
   interpreter `entry.command` launches.
4. **Every transitive dependency is pinned** in a target lock, with hashes.
   A VCS dependency is pinned to a full commit, built into a wheel in CI, and
   that wheel is what the lock hashes; a branch name or a source build on the
   person's machine is not a lock.
5. **CPU and GPU are separate profiles.** Shared packages live in `base.in`;
   only framework and runtime differences belong in `cpu.in` or `gpu.in`.
6. **Model and data files are not dependencies.** Author-known immutable
   files are manifest `assets`, with exact size and SHA-256. User-specific
   files are runtime slots selected through `ctx.host.request_asset()`. The
   bootstrap downloads neither.

Pin the SDK like any other input: by version and hash, from PyPI or from a
wheel built from a pinned commit of this repository.

## Producing locks

Use any resolver that produces the invariants above. One reference workflow
uses a pinned `pip-tools` on each native target:

```bash
python -m piptools compile --generate-hashes --resolver=backtracking \
  --output-file requirements/locks/linux-x86_64-py311-cpu.txt \
  requirements/base.in requirements/cpu.in
```

Run the corresponding command on Windows x86_64 and macOS Apple Silicon for
their locks, and generate a GPU lock on the target whose framework wheel index
and accelerator stack it represents. Keep custom package indexes in the input
files or resolver configuration, not in someone's shell.

If a package has no wheel for a claimed target, build and test the wheel in
that target's CI job, publish it with the release (see
[releasing](releasing.md), clause 4), and lock its hash. A compiler, a Git
checkout or a live dependency solver on the person's machine is not
reproducible.

Commit the inputs and the locks together. Regenerating a lock is an explicit
dependency update, never a side effect of the bootstrap.

## The bootstrap

`scripts/bootstrap.py` is a standard-library Python script. vizi runs it
**bare**: no arguments, the plugin folder as the working directory, on the
interpreter `.python-version` names. It must:

1. Pick its own default profile when called with no arguments. Accepting a
   `--profile cpu|gpu` flag for developers is fine; vizi never passes one.
2. Detect the platform, architecture and Python minor, and select exactly one
   checked-in lock for them. **A missing lock for the platform is a failure,
   not a fallback.**
3. Build `.venv` at the plugin root, the environment `entry.command`
   launches, and install the lock into it with hash checking and binary-only
   artifacts, equivalent to:

   ```bash
   uv pip install --python .venv --require-hashes --only-binary :all: \
     -r requirements/locks/linux-x86_64-py311-cpu.txt
   ```

4. **On Linux and macOS, make the venv with symlinks**:
   `venv.create(path, symlinks=True, with_pip=...)`, `python -m venv` (whose
   POSIX default is symlinks), or `uv venv`. A venv made by copying the
   interpreter (`venv.create`'s own default) does not start on macOS. Copies
   are fine on Windows.
5. Be idempotent: a second run on a built environment changes nothing. If
   `.venv` exists but was built from another interpreter, profile or lock,
   rebuild it or stop with a clear error; never mutate it silently.
6. On failure, exit non-zero, with the one-line cause shown to the person as
   the **last line it prints**.
7. Write nothing outside the plugin folder, and download no model or data
   file.
8. Record the selected target, profile and the lock's SHA-256 in
   `.venv/.vizi-environment.json` for the doctor.

Construct paths with Python, not shell conditionals: on Windows the venv's
interpreter is `.venv\Scripts\python.exe`. The manifest launches the
plugin-local interpreter, for example `[".venv/bin/python", "plugin.py"]`;
relative paths resolve from the plugin folder.

A developer runs the same script by hand, with whatever Python 3.11+ and `uv`
are at hand:

```bash
python3 scripts/bootstrap.py
.venv/bin/python scripts/doctor.py
```

`bootstrap.sh` or `bootstrap.ps1` wrappers are a convenience for developers
outside vizi; vizi never runs them.

## The doctor

`scripts/doctor.py` is read-only, prints the SDK version, and **ends with a
single ready / not-ready line**; it exits non-zero when not ready. Before that
line it prints concise pass/fail lines for:

- the interpreter's path and version, and the bootstrap record's target and
  profile;
- the selected lock's digest and the installed package versions;
- importing the SDK and the plugin, and the manifest's `engines.vizi` major
  matching the host's (`1`);
- imports and a tiny CPU computation for every enabled backend;
- each declared asset's metadata, and a candidate file's size and SHA-256;
- for a GPU profile, device discovery and one trivial device operation,
  with framework, runtime and driver versions.

A standalone doctor cannot see the host's asset store or runtime slots.
Expose the same checks through a plugin command when they matter: inside a
vizi-launched process, `asset_path(id)` confirms a declared file and
`ctx.host.request_asset(slot)` resolves a user-selected one. A GPU doctor that
finds no device has failed; it does not switch profiles.

Never print credentials, tokens, signed URLs or the whole environment. vizi
redacts secret-looking environment values from the output it keeps, but the
doctor should not emit them in the first place.

## Provisioning immutable assets

After the bootstrap:

1. Rescan in Plugin Manager.
2. Expand the plugin's asset rows.
3. Use **Download** for a declared credential-free HTTPS source, or
   **Import…** for a manual one.
4. Confirm each row says *verified*. A size or digest mismatch is rejected
   without replacing a previously verified file.
5. Launch the plugin and run its in-host health command, if it has one.

vizi publishes verified files atomically under a per-plugin root it owns.
Updating a declared model means changing its declaration (normally the digest
and size) and provisioning it again.

## Importing user-specific runtime assets

When a model or configuration is chosen by the person and cannot have an
author-known digest, declare `data_access` and request a named slot from a
command:

```python
path = ctx.host.request_asset("fine-tuned-weights")
if path is None:
    return
```

vizi opens a native single-file picker, copies the choice under the plugin's
asset root, and records its size and SHA-256 at import. The slot is reused
across sessions. A plugin's own "Choose different…" control passes
`force_reprompt=True`; canceling keeps the previous import. Use several slots
and assemble a directory plugin-side when a framework expects several files;
there is no directory chooser.

## CI

Cover every claimed lock target:

1. Run the bootstrap bare, on the Python minor `.python-version` names.
2. Regenerate the locks and fail on a difference.
3. Run the doctor, the plugin's imports, its registration tests and a small
   synthetic backend computation.
4. Build native and VCS dependencies into target wheels before the lock job,
   and test the same hashed wheel the person installs.
5. For GPU locks, at least install and import on ordinary CI. Run the device
   operation on a matching GPU runner when one is available; otherwise record
   the required driver and framework combination and keep a named manual
   check before release.
6. Check asset sizes and digests with licensed fixtures or metadata only. Do
   not commit restricted model payloads to make CI pass.

The release archive holds the manifest, the plugin code, `.python-version`,
the bootstrap and the doctor, the supported locks, and access to every locked
wheel ([releasing](releasing.md)). A fresh machine then needs only vizi; no
dependency choice is left to the person.
