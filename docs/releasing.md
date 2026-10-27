# Releasing a plugin

What a plugin release must be for vizi to install it from the catalog,
unattended, on a person's machine. The catalog is the list of plugins vizi
offers in Plugin Manager; a release is accepted into it version by version.

## 1. One archive per version

One zip per version, attached to the plugin's GitHub release and named
`<plugin-id>-<version>.zip`. It unpacks to a single folder `<plugin-id>/`
holding `vizi-plugin.json`, the code, and either:

- a setup script (clause 3), or
- a complete, self-contained build for every platform the release claims
  (clause 3b).

`version` is a [PEP 440](https://peps.python.org/pep-0440/) version, and the
catalog orders versions that way.

## 2. The manifest

`vizi-plugin.json` declares `engines.vizi`, the protocol the plugin needs
(see [the manifest reference](manifest.md#enginesvizi--compatibility-gating)),
and every permission it uses. The catalog entry for the release states the
minimum vizi version, which is the first vizi release that speaks the
`engines.vizi` minor: `"1.0"` is vizi v1.0.0. A plugin that needs a later
vizi release than that says so in `min_vizi` (a PEP 440 version such as
`"1.0.1"`). The manifest's `sdk_version`, which the SDK writes, names the SDK
release the plugin was built on, and the plugin's doctor prints it too
(clause 3f).

## 3. Setup

vizi provides the interpreter and the tool; the plugin provides a script.
[Reproducible environments](environments.md) explains each point with
examples.

- **(a)** The release has a `.python-version` file at its root holding a
  CPython version as digits and dots (`3.11` or `3.11.13`). vizi installs that
  minor version for the plugin and may choose the patch; do not depend on a
  patch. Paths, ranges and names are refused. A minor newer than the person's
  vizi can provide fails with a message asking them to update vizi, so set the
  catalog's minimum vizi version to match.
- **(b)** `scripts/bootstrap.py` is a standard-library Python script. vizi
  runs it on that interpreter, with the plugin folder as the working
  directory and no arguments. Called bare, it picks its own default profile
  and builds the environment the manifest's `entry.command` launches (`.venv`
  at the plugin root) from hash-locked requirement files for the current
  platform. A missing lock for the platform is a failure, not a fallback.
- **(b2)** On Linux and macOS, a venv made with the standard library must
  symlink its interpreter: `venv.create(path, symlinks=True, ...)`, or
  `python -m venv`, whose POSIX default is symlinks. Alternatively make it
  with `uv venv`, which (d) puts on `PATH`. A venv made by copying the
  interpreter (the `venv.create` default) does not start on macOS. Copies are
  fine on Windows. Every venv therefore points into vizi's shared interpreter
  directory, which vizi keeps for as long as any installed plugin uses it.
- **(c)** The bootstrap is idempotent. On failure it exits non-zero, and the
  last line it prints is the one-line cause shown to the person. It downloads
  no model or data (those are manifest assets) and writes nothing outside the
  plugin folder.
- **(d)** vizi puts a pinned `uv` first on `PATH` for the bootstrap and for
  every later launch of the plugin, with its cache and Python directories
  already set in the environment. A plugin may use it to build further
  environments lazily, at any time. Do not store its path, which changes
  between launches and vizi versions. Do not unset or override the `UV_*`
  variables vizi sets. Do not fetch your own `uv` or your own Python. vizi's
  release notes state the `uv` version, and a change of it is announced as a
  compatibility event.
- **(e)** vizi removes these from the environment before running a bootstrap
  or a plugin: `VIRTUAL_ENV`, `CONDA_PREFIX`, `CONDA_DEFAULT_ENV`, inherited
  `UV_*` (except `UV_PYTHON_INSTALL_MIRROR`), `PIP_*`, and the loader and
  `PYTHON*` variables. Do not rely on any of them.
- **(f)** A doctor (`scripts/doctor.py`) prints the SDK version and ends with
  a single ready / not-ready line.

A `bootstrap.sh` or `bootstrap.ps1` may ship as a developer convenience
outside vizi; vizi never runs them.

## 3b. No setup

A release may omit `scripts/bootstrap.py` if `entry.command` runs as unpacked
on every platform it claims, for example a frozen executable. vizi then
installs it by unpacking only. Such a release states its platforms in its
catalog entry, from `linux-x86_64`, `macos-arm64` and `windows-x86_64`.

## 4. Dependencies

Dependencies come from PyPI, or from the release's own assets by URL and
hash. Nothing comes from a private repository.

## 5. Acceptance pins the archive

Acceptance records the version, and the archive's SHA-256 and size. Replacing
the archive after acceptance makes that version uninstallable: ship a new
version instead.

## 6. Submitting a release

Email focolabdev@gmail.com naming the repository, the version and the
archive. Each later version is reviewed and accepted separately.
