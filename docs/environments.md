# Reproducible plugin environments

vizi launches the interpreter named by `entry.command`; it does not create or
solve a Python environment for the plugin. The portable convention below makes
that environment a reproducible plugin artifact while keeping it independent
from vizi's own Python installation.

This is a convention, not a new host service. A compiled plugin can ship one
executable and ignore it; Python plugins with scientific/ML stacks should
follow it.

## Repository contract

Keep these files in the plugin repository:

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

Only include lock targets the plugin actually supports. Add one lock per
claimed Python minor, platform/architecture, and compute profile; do not reuse
a Linux lock on Windows or a CPU lock for a GPU install.

The required invariants are:

1. `pyproject.toml` declares the tested Python range. The SDK supports Python
   3.11–3.13; a backend may narrow it, for example TensorFlow 2.15 plugins
   commonly select only 3.11. Do not claim an untested minor.
2. `.python-version` names the exact default interpreter used to generate the
   locks, including patch version.
3. `.venv/` is repository-local, ignored by version control, and is the
   interpreter used by `entry.command`.
4. Every transitive Python dependency is version-pinned in a target lock.
   Published wheels have hashes. A VCS dependency is pinned to a full commit,
   built into a wheel in CI, and that wheel is what the lock hashes; a branch
   name or a fresh-machine source build is not a lock.
5. CPU and GPU are separate profiles. Shared packages live in `base.in`; only
   framework/runtime differences belong in `cpu.in` or `gpu.in`.
6. Author-known immutable model/data files are manifest `assets`, with exact
   size and SHA-256. User-specific files are persistent runtime slots selected
   through `ctx.host.request_asset()`. Bootstrap never downloads either class
   through plugin code.

Treat the SDK's wheel like any other immutable input: build it from a pinned
commit of this repository, place it in a wheelhouse or `vendor/`, pin the
version whose protocol major matches the target host, and record the wheel
hash in every lock.

## Producing locks

Use any resolver that produces the contract above. One simple reference
workflow uses a pinned `pip-tools` version on each native target:

```bash
python -m piptools compile --generate-hashes --resolver=backtracking \
  --output-file requirements/locks/linux-x86_64-py311-cpu.txt \
  requirements/base.in requirements/cpu.in
```

Run the corresponding command on Windows x86_64 and macOS Apple Silicon for
their lockfiles. Generate the GPU lock on the target whose framework wheel
index and accelerator stack it represents. Keep custom package indexes in the
input file or resolver configuration so lock regeneration does not depend on
someone's shell settings.

If a package has no wheel for a claimed target, build and test the wheel in
that target's CI job first, publish it to the plugin's wheelhouse, and lock the
artifact hash. Requiring a compiler, Git checkout, or live dependency solver
during end-user bootstrap is not reproducible.

Commit the input files and generated locks together. Lock regeneration must be
an explicit dependency-update change, never an automatic bootstrap side
effect.

## Bootstrap contract

`scripts/bootstrap.py --profile cpu|gpu` should be the only supported setup
entry point. It must:

1. Detect OS, architecture, and Python minor; reject an unsupported tuple.
2. Verify the running interpreter is inside the declared Python range and,
   for a release build, matches the exact patch in `.python-version`.
3. Create `.venv` with `venv` if absent. If an existing `.venv` was built by
   another interpreter/profile/lock, stop with a clear error rather than
   silently mutating it.
4. Select exactly one checked-in lock and install it with the venv's Python,
   hash checking, and binary-only artifacts, equivalent to:

   ```bash
   .venv/bin/python -m pip install \
     --require-hashes --only-binary=:all: \
     -r requirements/locks/linux-x86_64-py311-cpu.txt
   ```

5. Record the selected target/profile plus the lockfile SHA-256 in
   `.venv/.vizi-environment.json` for the doctor.
6. Leave the manifest, assets, and user configuration untouched. In
   particular, it must not fall back to CPU after a requested GPU install.

On Windows, use `.venv\Scripts\python.exe`; the script should construct paths
with Python rather than shell conditionals. A bootstrap may use uv internally
as long as `--locked`/frozen behavior and the same target/profile invariants
hold.

Fresh-machine commands are therefore:

```bash
python3.11 scripts/bootstrap.py --profile cpu
.venv/bin/python scripts/doctor.py --profile cpu
```

or on Windows:

```powershell
py -3.11 scripts\bootstrap.py --profile cpu
.venv\Scripts\python.exe scripts\doctor.py --profile cpu
```

The manifest should launch the repository-local interpreter, for example
`[".venv/bin/python", "plugin.py"]`. Relative paths are resolved from the
plugin directory and keep a checkout or supported symlink installation
relocatable.

## Doctor contract

`scripts/doctor.py --profile cpu|gpu` is read-only and exits nonzero if the
environment is not ready. It should print concise pass/fail lines for:

- interpreter path/version and the bootstrap marker's target/profile;
- selected lockfile digest and installed package versions;
- SDK import, plugin import/registration, and the manifest's `engines.vizi`
  major matching the target host (`1.0`);
- imports and a tiny CPU inference for every enabled backend;
- each declared asset's metadata and a candidate file's size/SHA-256;
- for a GPU profile, framework device discovery and one trivial device
  operation, including framework/runtime/driver versions.

A standalone doctor cannot see the host-only declared-asset mapping or runtime
slot store. Also expose the same asset/device checks through a plugin command
or startup health path when those are required: inside a vizi-launched process,
`asset_path(id)` confirms a manifest-declared file, and
`ctx.host.request_asset(slot)` resolves a user-selected file. A
doctor-reported *GPU unavailable* status or failed device operation is a
failed GPU doctor, not permission to switch profiles.

Never print credentials, tokens, signed URLs, or the full plugin environment.
vizi redacts common secret-looking environment values from retained process
output, but the doctor should avoid emitting them in the first place.

## Provisioning immutable assets

After bootstrap and the standalone doctor:

1. Start vizi and Rescan in Plugin Manager.
2. Expand the plugin's asset rows.
3. Use **Download** for a declared credential-free HTTPS source or **Import…**
   for a manual source.
4. Confirm each row says *verified*; a size or digest mismatch is rejected
   without replacing a previously verified file.
5. Launch the plugin and run its in-host doctor/health command when provided.

The host publishes verified files atomically under an opaque per-plugin root.
Updating a declared model means changing its declaration (normally the digest
and size), reprovisioning it, and reviewing that change like a dependency
update.

## Importing user-specific runtime assets

When a model or configuration is chosen by the user and cannot have an
author-known digest, declare `data_access` and request a named slot from a
command:

```python
path = ctx.host.request_asset("fine-tuned-weights")
if path is None:
    return
```

The host opens a native single-file picker, copies the choice under the
plugin's asset root, and records its import-time size and SHA-256. The slot is
reused across sessions. A plugin-provided “Choose different…” control passes
`force_reprompt=True`; cancellation preserves the previous import. Use
multiple slots and assemble a directory plugin-side when a framework expects
several model files. There is no directory chooser.

## CI matrix

CI should cover every claimed lock target:

1. Use the exact Python patch for that lock and run bootstrap in frozen mode.
2. Re-run lock generation and fail on a diff.
3. Run doctor, plugin imports, SDK registration tests, and a small synthetic
   backend inference.
4. Build native/VCS dependencies into target wheels before the lock job; test
   the same hashed wheel end users install.
5. For GPU locks, at least install and import on ordinary CI. Run the device
   operation on a matching GPU runner when available; otherwise record the
   required driver/framework combination and keep a named manual hardware
   gate before release.
6. Validate asset size/digest using licensed CI fixtures or metadata-only
   checks. Do not commit restricted model payloads merely to make CI green.

The release artifact should include the manifest, plugin code, bootstrap and
doctor, supported lockfiles, and access to every locked wheel. A fresh machine
then needs only the declared Python, the repository/artifact, and explicit
Plugin Manager asset provisioning; no dependency choice is left to the user.
