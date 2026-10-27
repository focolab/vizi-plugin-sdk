# The plugin manifest — `vizi-plugin.json`, schema version 1

**Status: normative for protocol `1.0`.** This document says what a host
accepts as a plugin manifest and what it does with each field. It transcribes
the reference parser, `vizi_plugin_sdk/manifest.py` — the Python SDK's module,
which the host imports and scans with; the two are kept in step by the
**fixtures at the end of this document**, which test suites execute against
that parser and against each SDK's own.
A rule stated here without a fixture is a rule nobody has checked; a rule the
parser enforces and this document omits is a rule an author cannot know.

The manifest is the fourth document of the specification beside the three
channels. It is not a wire format — it never crosses a socket — but it is the
one document a plugin author writes (by hand, or through an SDK that derives it
from code), the one a host reads before any process exists, and the source of
several structural bounds the channels rely on (§6 below). An SDK can serve a
correctly launched plugin; it cannot certify a manifest it invented, and this
is what it certifies against — the Python SDK by running the reference parser
itself, the others by their own validators tested against §8's fixtures.

---

## 1. The file, and how a host reads it

- **One file, `vizi-plugin.json`, at the plugin directory's root.** The host
  scans a plugins directory (`~/.vizi/plugins/`, or `$VIZI_PLUGINS_DIR`) for
  directories containing one, at startup and on demand.
- **An id is one plugin.** Every per-plugin store is keyed by it, so a second
  directory declaring an id already loaded is not loaded: in directory-name
  order the first wins, and Plugin Manager lists the second as not loadable,
  naming the first.
- **An id is lower-case reverse-DNS.** It matches
  `^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$` and is 3 to 64 characters long: a
  first part of a letter then letters and digits, and at least one more part,
  each a letter then letters, digits or `_`, the parts joined by single dots.
  Anything else is
  `id '…' is not a plugin id: lower-case reverse-DNS, 3 to 64 characters, matching ^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$`.
  The grammar is closed so that the folder a plugin installs to is its id,
  unchanged: two ids never name one folder, on a
  case-insensitive file system or any other, and an id is never a path.
  For the same reason the first part is not a Windows device name (`con`,
  `prn`, `aux`, `nul`, `com0`–`com9`, `lpt0`–`lpt9`, in any case), which
  Windows refuses as a folder name: such an id is
  `id '…' is not a plugin id: its first part '…' is a Windows device name`,
  and `console.example`, `com10.x` and `example.con` are ordinary ids.
- **`settings/` beside it belongs to the plugin's settings.** The host names
  it in `VIZI_PLUGIN_SETTINGS_DIR` at launch without creating it, and the SDK
  creates it on first use (`control_plane_protocol.md` §2.3); a plugin does not
  ship anything there,
  and whatever replaces a plugin's files on an update keeps it.
- **It is read as data.** No plugin code runs at scan time; the host launches a
  plugin lazily, when a surface it contributes is first used.
- **An invalid or incompatible manifest is logged and skipped at scan.** It
  never runs, never breaks discovery of other plugins, and never crashes the
  host.  Plugin Manager lists it as *not loadable*, with the reason, so a
  plugin does not vanish without saying why; a directory with no
  `vizi-plugin.json` is not a plugin and is not listed. Every rejection below is a diagnostic
  naming the manifest and the field, written to the host's log.
- **The file is UTF-8 JSON whose top level is an object.** Anything else is
  rejected before any field is read.
- **Unknown fields are ignored**, at the top level and inside every entry —
  the append-only posture of `control_plane_protocol.md` §3.2 applied to the
  document. The exceptions are stated where they occur (§7), because they are
  the places where ignoring would mislead the author.

---

## 2. Top level

| Field | Type | Required | Rule |
|---|---|---|---|
| `manifest_version` | integer | yes | **`1`**, the only version (§7) |
| `id` | string | yes | non-blank, lower-case reverse-DNS (§1): `org.lab.plugin`. Globally unique |
| `name` | string | yes | human-readable, for the Plugin Manager and menus |
| `version` | string | yes | the plugin's own version; it keys saved stage output (below) |
| `engines` | object | no | with `vizi` (§2.1); omitted, it reads as `{"vizi": "1.0"}` — the lowest floor |
| `min_vizi` | string | no | the oldest vizi release the plugin runs in, a PEP 440 version (§2.4) |
| `sdk_version` | string | no | the SDK release that built the plugin (§2.4) |
| `entry` | object | yes | with `command` (§2.2); optional `startup_s` (§2.3) |
| `contributions` | object | no | §3–§6; default empty |
| `assets` | list | no | §4 |
| `permissions` | list of strings | no | §5 |
| `subscriptions` | list of strings | no | §5.1 |

A missing required field is `missing required field 'x'`; a field of the wrong
type is `field 'x' must be <type>`, and a container of the wrong shape names
itself — `contributions.<kind> must be a list`, `assets must be a list`,
`binding must be an object`, `options must be a list of strings`. Those shape
rejections are not fixtured one by one below; every rule about a *value* is.

**`id` and `version` are what the handshake's identity string is composed
from.** The SDK reads both from the manifest in the launch directory and
announces `Ready; I am <id> <version>; …` (`control_plane_protocol.md` §3.1),
which the host writes to the plugin's log so a session names the build that
spoke. That is why both are required strings and not merely conventional.

**Change `version` whenever your output changes.** vizi saves what a stage
produced beside the file it filtered and reads it back, rather than running the
stage again, when the plugin `id`, the `version`, the stage and its parameters
all match (`processing_stages.md` §2). A rebuild that changes what a stage
computes but keeps the version shows the old build's output as though it were
the new one's; vizi trusts the version to say when a result is no longer the
same result.

**`version` is ordered as a PEP 440 version.** The parser accepts any
string, but the plugin catalog compares an installed version with
the one it lists by PEP 440's ordering, to say whether the listed one is
newer. A version that does not parse as PEP 440 can only equal another, so a
plugin installed at one is never offered an update.

### 2.1 `engines.vizi` — the compatibility gate

A string `"major.minor"` (`"1.0"`, `"1"`, `"1.x"`), read as a **minimum**: the
**floor**, the oldest host protocol the plugin's author tested against. A
leading `^`, `~` or `>=` is tolerated and immaterial.

**The grammar is exact.** At most one leading prefix, exactly `>=`, `^` or `~`;
then `MAJOR`, `MAJOR.MINOR` or `MAJOR.x`, where `MAJOR` and `MINOR` are each one
or more ASCII digits `0`–`9` (leading zeros allowed: `"01.0"` is `1.0`), and
`MAJOR` and `MAJOR.x` read as minor 0. Nothing is trimmed — no whitespace
anywhere — and there is no `+` sign, no underscore and no other digit
character. Each component is at most 4,294,967,295; a larger one is refused,
never wrapped or clamped. A value of that shape with a `-` on a component is
`engines.vizi '…' has a negative component`; anything else outside the grammar
is `engines.vizi '…' is not a version` — **including a third component
(`"1.0.3"`) and `*` in place of `x` (`"1.*"`)**. The protocol has no patch
level to require, and a host that quietly read either as `1.0` would list a
plugin whose own SDK then refuses its manifest at startup.

The gate is `control_plane_protocol.md` §3.2's, stated once: **the host runs the
plugin when the protocol majors are equal and the host's minor is at least the
declared minor.** A major breaks and a minor does not. **An incompatible
manifest is skipped at scan**, like an invalid one — a `0.x` or a `2.0`
manifest never runs in a `1.0` host; Plugin Manager lists it as not loadable,
naming the version it needs, and so does a warning in the host's log. (The launch
path checks the same rule again for a manifest handed to it directly, and
refuses with a diagnostic naming both versions.)

**`engines.vizi` is the floor, not the version the session runs at.** The
session's minor is negotiated at the handshake — the lower of the host's and
the plugin's (`control_plane_protocol.md` §3.1) — and is never below the
floor: a plugin whose session would be refuses it at the handshake. A plugin
that uses a feature newer than its floor asks its SDK whether the session has
it, so one plugin runs on every host from its floor up, rather than raising
its floor to the newest feature it can use.

**`engines` is read first**, before `manifest_version` and every other field,
so that a manifest written for a later 1.x is refused for that and not for the
first thing that 1.x added (a widget kind this host has never heard of). When
`engines.vizi` is a string, a malformed one is refused as above, and one whose
major differs or whose minor exceeds the host's is refused with exactly

    <name> <version> needs vizi protocol <engines>; this vizi speaks <major>.<minor>

— the plugin's `name` (its `id` when there is no name), its `version`, and
`engines.vizi` as written. The scan, an install and a launch say the same
sentence. An omitted `engines` reads as `{"vizi": "1.0"}`, the lowest floor (§2), so it is
never refused for its floor. Inside a present `engines`, a missing or non-string
`engines.vizi` is refused in the ordinary order (`missing required field 'vizi'`, `field 'vizi' must be str`). **Where
`engines` is and what it looks like are frozen**: `engines.vizi`, a string of
this section's form, at the top level of every manifest of every 1.x.

### 2.2 `entry.command` — the launch command

A **non-empty list of strings**; `entry.command must be a non-empty list of
strings` otherwise. The process is started with the plugin directory as its
working directory, so relative paths resolve against it. `command[0]` may be a
bare name on the host's `PATH`, a relative path, or an absolute path. Two
shapes are translated on Windows so one manifest launches everywhere: the
portable venv form (`.venv/bin/python` → `.venv\Scripts\python.exe`) and a
bare `python3` (which most Windows installs do not have) — the host prefers a
plugin-local `.venv`, then `python` on `PATH`, then the `py -3` launcher.
A `python` found under `WindowsApps` comes last: on a default Windows 11
install it is a stub that opens the Microsoft Store instead of running.

**What the command starts must be the process that serves the plugin, or reach
it without losing what the host handed over.** Two things travel from the
host to the spawned process and no further on their own:

- **On unix, the doorbell is an inherited file descriptor**
  (`doorbell_protocol.md` §3.1). A launcher that spawns the real plugin as a
  *child* with the runtime's default descriptor policy strands it: Java's
  `ProcessBuilder`, Go's `os/exec` and CPython's `subprocess` (`close_fds=True`)
  all pass nothing above descriptor 2, and setup fails on a descriptor number
  the environment names and the process does not hold — which the reference
  SDK reports as *"cannot adopt doorbell fd N: … the doorbell fd was not
  inherited: a launcher … closed non-stdio descriptors"*, and another SDK
  reports in its own words (this document does not speak for them). An
  `exec` that replaces the process
  keeps it (a `sh -c 'exec …'` wrapper works; a Python wrapper that
  `subprocess.run`s does not). This is the **host→plugin** direction; the
  opposite rule, `doorbell_protocol.md` §7.6's *"descriptors are never
  inheritable"*, is about a plugin's own children and is what the SDK
  enforces once it has adopted the descriptor — the two are not in tension,
  and the runtimes that table calls *"accidentally safe"* are the ones that
  strand a launcher here. Windows is unaffected: the plugin dials in, and a
  wrapper that propagates the environment is enough.
- **On every platform, the host captures the spawned process's stdout and
  stderr** — `doorbell_protocol.md` §3.1's setup failure *"exits with a
  diagnostic on stderr, which the host captures"* is a promise about the
  process the host started. A wrapper that does not forward its child's
  streams leaves the host with an empty capture and only *"did not bind within
  60s"*; Java's `ProcessBuilder` defaults every stream to a pipe nobody reads.

**The process inherits vizi's environment, less the loader, Python, virtualenv,
conda, uv and pip variables** that would make it load vizi's code instead of
its own; the host then sets its own uv and pip cache and config variables and,
when it has a uv, puts that uv first on `PATH`. The list is
`control_plane_protocol.md` §2.1's, and **beyond the variables that section
names, a plugin relies on no inherited variable**.

**stdin is unspecified**: it is whatever vizi's was, which may be a terminal,
closed, or not a valid descriptor at all. A plugin never reads from it; what
it is told arrives on the control plane.

**The host owns the whole tree the command starts, and ends all of it.** On
unix the command runs as the leader of a new session, so every process it
spawns shares its process group and teardown signals the group — SIGTERM,
`control_plane_protocol.md` §3.3's window, then SIGKILL for what remains. On
Windows the process is placed in a job object that its descendants inherit;
teardown terminates the job, and the kernel does the same if the host dies
first. A launcher in front of the plugin is therefore fine for process
lifetime: the real plugin goes down with it, on the launch timeout as much as
on a voluntary shutdown. What is NOT covered is a launcher that detaches the
real plugin into a session of its own (a daemonizer) — that process has left
the tree, and the host will not go looking for it. Because the plugin is no
longer in the terminal's session, a Ctrl+C in the terminal that launched vizi
reaches the host alone, which shuts its plugins down in §3.3's order; nothing
reaches a plugin from the terminal directly.

(The rules in this section — working directory, `PATH` resolution, the
Windows translation, the launcher chain, the process tree — are the launcher's,
not the parser's, so §8's fixtures do not reach them.)

### 2.3 `entry.startup_s` — how long the plugin expects to take to start

Optional. **A positive number of seconds**; anything else is
`entry.startup_s must be a positive number of seconds` (zero, a negative, a
string, `true`). It says how long the plugin expects to take from being
started to sending `Ready` — a cold JVM, a first-run dependency resolution, a
heavy import.

**It is advisory.** A host may honor it, cap it or ignore it, and nothing
about the plugin may depend on it being honored
(`control_plane_protocol.md` §9's launch deadline is host policy). vizi
honors it within 10 s to 300 s; without it a launch has 60 s. A plugin that
needs more than its host gives fails to start and can be started again; the
person can also cancel a start at any time, so a generous value costs only
the time to notice a plugin that hangs.

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["java", "-jar", "x.jar"], "startup_s": 120 } }
```

<!-- fixture: rejected "entry.startup_s must be a positive number of seconds" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"], "startup_s": 0 } }
```

<!-- fixture: rejected "entry.startup_s must be a positive number of seconds" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"], "startup_s": "60" } }
```

<!-- fixture: rejected "entry.startup_s must be a positive number of seconds" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"], "startup_s": true } }
```

### 2.4 `min_vizi` and `sdk_version` — what the release needs and what built it

Two facts about a release, both optional, beside the required fields and never
in place of one:

- **`min_vizi`** is the oldest vizi release the plugin runs in, for a plugin
  that needs a host fix newer than the first release speaking its
  `engines.vizi` minor. It is a PEP 440 version in its normalized public form
  without an epoch or a local label — release numbers, then optionally `aN`,
  `bN` or `rcN`, `.postN`, `.devN` (`"1.0.0"`, `"1.0rc1"`, `"1.2.post3"`).
  Anything else is `min_vizi '…' is not a PEP 440 version`; a value that is
  not a string is `min_vizi must be a string`. **Every protocol 1.0 host
  reads it.** A host older than `min_vizi` does not run the plugin: it is skipped at scan, and Plugin Manager lists it
  as not loadable with `<name> <version> needs vizi <min_vizi> or newer`, the
  catalog's sentence for the same fact. `engines.vizi` still gates:
  `min_vizi` narrows the hosts that run a plugin, never widens them. The
  Python SDK's `declare()` writes `"min_vizi": "1.0.0"` when the author sets
  none: 1.0.0 is the first vizi release that speaks protocol 1.0, so the
  default changes nothing about which hosts run the plugin, and a manifest
  without the field means the same.
- **`sdk_version`** is the SDK release that built the plugin, in the Python
  SDK's version form (`"1.0.0"`); each SDK writes its own release here
  whenever it writes a manifest, and an author does not set it. A value that
  is not a non-empty string is `sdk_version must be a non-empty string`. It
  gates nothing: it says which SDK wrote the manifest, for a person reading it.

---

## 3. Contributions

`contributions` is an object whose keys are the eight kinds below, each a list.
A kind that is absent is empty. **Ids are unique within a kind, not across
kinds** — a command and a panel may share an id; two panels may not. Every kind
rejects a duplicate with `duplicate <kind> id '…'`.

### 3.1 `readers` — "I open files with these extensions"

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | non-blank, unique among readers |
| `extensions` | list of strings | one of the two | each non-empty once its dots are stripped |
| `fallback_extensions` | list of strings | one of the two | each non-empty once its dots are stripped; none also in `extensions` |
| `name` | string | no | defaults to `id` |

A reader declares at least one suffix across the two lists. Suffixes are
normalized to lower case with one leading dot (`"TIF"`, `".tif"` and `"tif"`
are all `.tif`), and a reader **is a candidate for a path whose name ends with a
declared suffix**, case-insensitively, **and is longer than it**: a file named
only `.tif` is no `.tif` candidate, because it has no name before the suffix.
A suffix may be compound: `.ome.tif` is its own suffix, and a file named
`x.ome.tif` matches a reader declaring it as well as one declaring `.tif`.

The checks run in this order, **`extensions` completely and then
`fallback_extensions`** (so a fault in `extensions` is reported even when
`fallback_extensions` is not a list): the value is a list
(`extensions must be a list`, `fallback_extensions must be a list`); every
entry is a string that is neither empty nor only ASCII whitespace (space, tab,
CR, LF, VT, FF) once its leading dots are stripped — so `"."` and `" "` are
refused in either list (`each extension must be a non-empty string`,
`each fallback extension must be a non-empty string`); the two lists together
declare at least one suffix (`extensions must be non-empty`); and no normalized
suffix is in both (`a suffix is in both extensions and fallback_extensions`,
followed by the sorted list of those suffixes). The quoted texts are the ones
§8's fixtures hold every validator to; the words around them are the
reference's and need not be copied. The host launches the plugin when the user opens a
path with it and routes `OpenReader` by `reader_id`
(`control_plane_protocol.md` §8.2) — which is why the id is unique: two readers
with one id are unaddressable.

**Which candidate opens a file is the user's, never an accident of scan order.**
Nothing reads the file to decide: a candidate is a declaration, and a
reader that declared a suffix it cannot read after all fails its open. When
several readers are candidates, the host opens the one the user made the
default for that file type; asks when the defaults leave a tie; and always
shows which reader opened a file. `fallback_extensions` is for a reader that
*can* read a suffix but should not be picked for it unasked — a generic HDF5
reader beside an NWB one: it is offered and can be made the default, but is
never chosen automatically over a reader that declared the suffix in
`extensions`.

**Among outright declarations, the longer matched suffix wins unasked.**
A reader declaring `.zarr.zip` is picked over one declaring `.zip`, and one
declaring `.ome.tif` over one declaring `.tif`: the longer suffix names the
format, the shorter only its container.  Equal suffixes still tie and are
asked; a default the user made still overrides; and the rule never promotes a
fallback, however long its suffix.  So declare the most specific suffix your
format has -- and know that declaring a bare container suffix (`.zip`)
outright takes every file of that container no reader names more precisely.

### 3.2 `commands` — an addressable behavior

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | non-blank, unique among commands |
| `title` | string | yes | the menu / palette caption |
| `description` | string | no | default empty |
| `default_keybinding` | string or null | no | non-blank if present; **a request** — the host resolves it and the plugin loses any conflict with a core binding |
| `menu` | `"tools"` or null | no | default `"tools"`; `null` registers the command without listing it |
| `pane` | object | no | §3.6.3, a momentary button |

A command is what panel widgets fire (§3.3), what the Tools menu invokes, and
— with a `pane` button — what the control pane fires once on click.  Any
`menu` other than `"tools"` or `null` is `menu '…' not allowed`.  A command
takes keys and a pane button only: **mouse chords belong to tools**, because a
chord carries a position, and that is what a `"fire"` tool is (§3.6).

### 3.3 `panels` — a declarative side panel the host builds

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | non-blank, unique among panels |
| `title` | string | yes | the dock title |
| `menu_label` | string | no | non-blank, at most 128 characters |
| `root` | widget object | yes | the tree, §3.3.1 |
| `view_targets` | list | no | set-view: distinct names from `t`, `z`, `select_region`, `frame_region`, `look_at`, `orbit_point`; required when any widget is `moves_view` |
| `follow_targets` | list | no | Follow (§8.15): what a Follow started from this panel may drive, distinct names from the same list |

"Plugins propose, host disposes": the host builds real widgets from the tree
and owns them; the plugin patches state through the control plane
(`PanelPatch`) and pushes images through the panel segment (`P`).

#### 3.3.1 The widget tree

Every node is an object with `kind` and `id`. **Widget ids are unique within
one panel** (`duplicate widget id`), not across panels. The tree is bounded
against an untrusted manifest: **depth at most 8** (`panel tree deeper than 8`)
and **at most 256 widgets per panel, root included** (`panel has more than 256
widgets`). Panels themselves are not capped — see §6 for why.

| `kind` | Fields it reads | Rule |
|---|---|---|
| `label` | `text`, `text_format` (`"plain"`) | `text_format` `"plain"` or `"rich"`; no other kind takes it |
| `button` | `text`, `command` | interactive |
| `slider` | `command`, `min` (0), `max` (1), `step` (1), `value`, `float` (false) | interactive; `min < max`, `step > 0`, `value` numeric if present |
| `dropdown` | `command`, `options`, `value` | interactive; `options` a non-empty list of strings; `value` in `options` if present |
| `checkbox` | `command`, `text`, `value` | interactive |
| `line_edit` | `command`, `placeholder`, `value` | interactive |
| `image` | `command`, `moves_view` | the target of `push_image`; with a `command`, a click reports the pixel |
| `progress` | `value` (0), `indeterminate` (false) | **not** interactive; `value` numeric and within `[0, 1]` if present |
| `row`, `column` | `children` | a **non-empty** list of widgets |
| `channel_picker` | `command`, `multiple` (false), `value` | interactive, host pickers below; `value` a channel index, or with `multiple` a list of them |
| `color_picker` | `command`, `value` | interactive; `value` `"#RRGGBB"` if present |
| `colormap_picker` | `command`, `value` | interactive; `value` a colormap name if present |
| `roi_picker` | `command`, `value`, `follows_selection` (false), `filter` (`"all"`) | interactive; `value` a region id if present; `filter` `"all"` or `"mine"` |
| `list` | `row`, `max_rows` (256), `visible_rows` (8), `selectable` (false), `command` | rows supplied at runtime, below; `row` a non-empty list of leaf widgets; `command` only, and then required, when `selectable` |
| `table` | `columns`, `selectable` (false), `multiple` (false), `command`, `visible_rows` (8), `sortable` (false) | typed rows supplied at runtime, below; `columns` 1 to 32; `command` only, and then required, when `selectable`; `multiple` only when `selectable` |

**Interactive kinds must bind a declared command** — `command` names an id in
this manifest's `commands`, or the widget is rejected (`… widget '…' must bind
a declared command`). A widget binding nothing would be a control the host
could render and nothing could hear.

**What each interactive kind sends**, in `InvokeCommand.args`, and when:

| `kind` | `args` | Fires |
|---|---|---|
| `button` | `{}` | on each click |
| `checkbox` | `{"value": b}` | on every toggle the person makes |
| `line_edit` | `{"value": s}` | when the person presses Enter or the field loses focus |
| `dropdown` | `{"index": i, "value": s}` | each time the person picks an option, even the one already shown |
| `slider` without `moves_view` | `{"value": f}`, or `i` when not `float` | on release, and on any change made without dragging (keys, a click on the groove); never while dragging |
| `image` with a `command` | `{"x", "y", "button", "modifiers", "image"}` | on press (below) |
| a `selectable` `table` | `{"rows": [key, …]}`, a list even of one | on each change to the selection the person makes |
| the pickers, a `selectable` `list` | below | below |

**A slider carries its value, never a position.** A slider's readout shows its
value in the plugin's units. An integer slider moves by its declared step,
which is a whole number (`slider '…' step … is not a whole number`). A float
slider moves by its declared step when one is given (a declared step is
remembered as such, so a declared `1.0` is not the default), with the readout
formatted to that step's precision; with no step it has 1000 positions over
its range and a three-decimal readout. Every move is one step — an arrow key,
PageUp or PageDown, and a drag, which lands on a step — and **no move passes
`max`**: when the range is not a whole number of steps the last move stops at
`max`. The event carries the value, never a position.

A command fired from a menu, a key or a pane button carries empty `args`.
**`cause` (set-view, below) travels in its own field, never in `args`.**

**A patch never fires a widget's command.** `set_value`, `set_options` and
`set_rows` change what a widget shows and send nothing back; only a person's
interaction fires the command (`control_plane_protocol.md` §8.4). So an event
is always a person's, and a plugin that sets a value never hears it back.

**A label shows its text as written** (`text_format` `"plain"`, the default)
— its declared `text` and every later `set_value` alike — so markup in text a
plugin relays from data, such as a region's name, is shown, not interpreted.
A label that wants formatting declares `"text_format": "rich"`, and its text
is then read as Qt's rich-text subset; a link in it is styled but **never
opened**. No other kind takes the field (`does not take text_format`).

**The four pickers offer the host's choices, not the manifest's.** A
plugin that shows data about channels, colors or regions would otherwise ask a
person to type an index, a hex string or a region id, or rebuild a dropdown
from polled state; these are the host's own lists, re-read each time the
picker opens, so a newly opened dataset, a new region or a newly saved colormap
is simply there. Each fires its command with `{"value": …}`:

- `channel_picker` — the open dataset's channels, each shown by the name the
  person sees everywhere else in the host: the file's name for it, else
  "Channel N", 1-based. The value is the 0-based index, or with
  `multiple: true` (a checkable list) the indices chosen, as `Value.ints`.
- `color_picker` — a swatch opening the color dialog vizi uses for its own
  colors: opaque, `"#RRGGBB"`.
- `colormap_picker` — every colormap vizi renders, built-in first and then the
  user's own, each drawn as a swatch; the value is the name, and
  `GetColormap{name}` (`control_plane_protocol.md` §8.4) returns the table vizi
  renders it with, so an image the plugin draws matches.
- `roi_picker` — the open dataset's regions by name; the value is the region
  id, with `"name"` beside it. **Choosing one does not change vizi's own
  selection**: a plugin hears that selection through `ViewState` and
  `ViewChanged`, and moving it is a view change, which is not a panel's to make.

**A `list` shows rows the plugin supplies at runtime, each built from its
`row` template.** A panel's tree is otherwise fixed, and a plugin showing
one row per region, per channel or per result would have to guess a count. The
template is **one row of leaf widgets** — any leaf kind, images and the pickers
included, but no `list`, `row` or `column` — whose ids share the panel's id
space. The plugin sets the rows with `set_rows` and addresses one row's cell by
its key (`control_plane_protocol.md` §8.4); an event from a cell carries
`args["row"]`. `max_rows` (1 to 1024, default 256) bounds the rows, and with
them the host's queue sizing, which counts each template widget once per
possible row. **A list realizes at most 4096 cells, `max_rows` × the row's
widgets**: each row is built whole on the host's UI thread, and the
per-panel widget cap counts a row's template only once; `visible_rows` (1 to 1024, default 8) is the height before the
list scrolls. Column headings are an ordinary `row` of labels declared above
it. With `selectable`, one row can be selected and the list's own `command`
fires with `{"row": key}`.

**A `table` shows rows of typed cells under its declared `columns`** — data,
not widgets: the host draws the rows the person can see, so a table holds up
to **100,000 rows of at most 32 columns** where a `list` stops at 1024. Each
column has an `id` (distinct), a `header` (its id when absent) and a `type`,
`"string"` (the default), `"number"` or `"bool"`; a number column may declare
`precision`, the decimals shown (0 to 15, default 3), and a `unit`, shown in
its header. Numbers are right-aligned. A row has a key (non-empty, distinct, at
most 128 characters) and its cells in column order; a missing cell is blank.
The plugin states the rows whole with `set_table` or changes them by key with
`patch_table` (`control_plane_protocol.md` §8.4); a statement or patch that
would take the table past 100,000 rows is refused whole, and one `PanelPatch`
carrying a table op is at most 1 MiB, so a large table goes as a whole
statement followed by patches — which the reference SDK does by itself.
`visible_rows` (1 to 1024, default 8) is the least height: the reference host
lets a table grow into the panel's spare height when the panel has no `image`,
and keeps it at `visible_rows`, scrolling, beside one. The table is read-only.
With `sortable`, a click on a header sorts the rows on the host's side —
numerically for a number column, blanks last in either direction — with no
round trip, and the
plugin's own order is unchanged underneath. With `selectable`, the person
selects rows and the table's `command` fires with `{"rows": [key, …]}`; with
`multiple` too, several. A `set_value` (`Value.s`, one key or `""`, or
`Value.strings` for several) sets the selection and never fires the command.

**Set-view: a plugin may move the person's view, but only when the person
asked it to** (`control_plane_protocol.md` §8.15). A panel declares what
it may move in `view_targets`, and marks the widgets whose use may move it
`moves_view: true`. That is allowed on any interactive widget, a selectable
`list` or `table`, and an `image` with a `command`. Each interaction with such a widget
carries a host-minted `cause`, which the plugin's `ViewRequest` names. A
`moves_view` widget in a panel with no `view_targets` is rejected. A tool
declares its own `view_targets`, and only its FIRE interactions are causes.

**The person allows it per plugin, in a control the host owns.** A plugin
that declares `view_targets` or `follow_targets` anywhere gets one: in the
reference host, "Control view" on its row in the Plugin Manager, off
until the person turns it on and remembered after. It is not part of the
plugin's widget tree, has no id, cannot be patched, and nothing about it is
sent to the plugin. It governs the plugin's panels' widgets and its tools
alike, and **a panel must be open and showing while the view moves**, so a
person can see who is moving it. A tool with `view_targets` in a plugin with
no panel that declares them is rejected.

**Follow** (§8.15) is a panel's `follow_targets`: what a Follow the person
starts from that panel may drive. A panel with `follow_targets` gets a first
row the host draws, which holds the Follow button while the plugin offers
one; the person's choice above is its master, so the button is disabled
while the plugin is not allowed to move the view.

**A clickable `image`** binds a `command`. A click fires it, on press, with
`{"x", "y"}`, the pixel in the image's own coordinates (0,0 at top left), plus
`"button"` (`"left"`, `"middle"`, `"right"` or `"other"`), `"modifiers"` (for
example `"shift+ctrl"`, or `""`) and `"image"`: the offset of the `ImageRef` the
widget was showing, so the plugin knows which image the pixel belongs to even
if it has pushed a new one since.

**How an image is shown is the host's; where a click lands is not.** A host may
show an `image` scaled to fit, and decides how to treat the screen's
device-pixel ratio. **A click's `x` and `y` are always in the pushed image's own
pixels**, whatever the display scale, so a plugin never converts. The reference
host gives an `image` the space its panel has — its width, and the height the
panel's other rows leave, so the image grows with the dock while the rows
around it keep theirs — hinting at the image's own size so the panel first lays
it out whole, and shows it centered and scaled to fit
with its aspect kept: smoothly when it is larger than that space, by a whole
factor with nearest neighbours when smaller. It tells the plugin how large
each `image` widget is drawn (`WidgetGeometry`, `control_plane_protocol.md`
§8.4), and an image whose size equals `width_px × dpr` by `height_px × dpr`,
each rounded half to even, is drawn unscaled, one image pixel per screen pixel
— so a plot redrawn at that size is crisp. A click outside the picture fires nothing. Likewise a `label`'s wrapping and eliding are the host's.

**A `roi_picker` may follow vizi's selection.** With `follows_selection`, it
shows the region the person has selected wherever they selected it. Choosing
a region in it selects that region in vizi, as choosing it in the Regions
panel does: the choice is the person's, made in a control vizi draws, so no
permission is involved. `filter: "mine"` lists only the regions this plugin
contributed.

`multiple` belongs to `channel_picker` and a selectable `table`. A picker's shown value is patched
with `set_value` like any interactive widget (`Value.ints` for a multi-select
channel picker). The host keeps nothing a picker chose: what the choice means,
and whether it is saved, is the plugin's.

**`progress` is the one kind the host drives and the user cannot.** Work
that runs for minutes needs to say how far along it is, and the two other
shapes are both wrong for it: a `label` the plugin rewrites is a plugin
drawing a widget the host should draw, and a `slider` is interactive by the
table above, so it must bind a command and offers the person a control they
can drag, which a progress bar is not. A `progress` widget binds nothing and
is patched through `PatchOp.set_progress` (not `set_value`). `indeterminate`
is for work whose length is not known in advance and wins over `value`. A
patched `value` is clamped into `[0, 1]`, and NaN shows **empty**: it says
nothing about how far along the work is, and clamping it would draw a full bar.
The reference host shows the fraction to **1/1000**.

**Field types are checked, not coerced**: `min`, `max` and `step` must be
finite numbers (`… min must be numeric`; a boolean is not a number; a number
too large for a double is `… must be a finite number`), `float` and
`indeterminate` booleans, `text` and `placeholder` strings. A field a kind does not read is
still type-checked if present — `"min": "low"` on a label is rejected, not
ignored, because a value that would have crashed the parser is the one kind of
unknown the host cannot pass over.

**Unknown kinds are rejected**, not ignored (`unknown widget kind`): a manifest
written for a newer host would otherwise render with holes in it and no
diagnostic. This is one of §7's exceptions to "unknown is ignored".

### 3.4 `exporters` — "I write this artifact format"

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | non-blank, unique among exporters |
| `extension` | string | yes | a suffix by the reader rule (§3.1: not empty, nor only ASCII whitespace, once its leading dots are stripped); normalized like a reader's |
| `name` | string | no | defaults to `id` |

The entry appears in the Export Data dialog's format selector; `extension`
drives the save filter. Voxels reach the plugin through the export segment
(`X`, `shared_memory_protocol.md` §3).

### 3.5 `overlays` — "I draw into the views"

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | non-blank, unique among overlays |
| `name` | string | no | defaults to `id` |
| `segment_mib` | integer | no | default **32**; in **[1, 512]**; **refused on a chunked overlay** |
| `chunked` | bool | no | default `false`; the overlay carries one label volume per (level, t) the host asks for (`control_plane_protocol.md` §5.7) |
| `time` | string | no | `"per_t"` (default) or `"static"`; only with `chunked` |

Each overlay is granted **one `O` segment** with `subject` = its id
(`shared_memory_protocol.md` §3): a pulsed live lane beside a loaned arena of
`segment_mib` MiB. The default suits ~10⁵ f64 points. A push naming an overlay
the manifest did not declare is refused by the host.

A **chunked** overlay is a dense segmentation of a whole movie: its unit of
state is one label volume at (level, t), not a draw list, and the host names
the volumes it wants (`ChunksWanted`) rather than the plugin restating the
overlay whole. Its arena is **sized by the host from the open dataset** — one
level-0 volume plus one level-1 volume — and re-granted when the dataset
changes, so `segment_mib` does not apply and is refused. `time` says whether
one volume per level serves every timepoint (`"static"`, a fixed atlas) or the
host asks per t (`"per_t"`); it applies to nothing but a chunked overlay. A
chunked overlay takes `OverlayChunk` and `OverlayLabelTable` only — an
`OverlayUpdate` sent to one is refused — and never compiles
(`control_plane_protocol.md` §8.8).

**A plugin that declares an overlay must declare something that starts it.**
An overlay is drawn only while its plugin has a session, and the host starts
a session only for something the person does. What counts is what the host
always offers: a panel; a command or a tool that is in the Tools menu or has a
`pane` button; a reader, an exporter or a stage. A `default_keybinding` or a
tool's `triggers` do not count on their own — another binding can take them,
and the host then drops the plugin's. A manifest with overlays and none of
those is refused (`overlays are drawn only while the plugin runs, and nothing
declared here starts it; …`) rather than loaded as a plugin that never runs.
The host lists a command or tool whose only way in is a pane button in the
Tools menu, so that what this rule accepts can be started. A panel is the usual
answer: it is where the person finds the overlay's controls.

### 3.6 `tools` — a live interaction mode

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | non-blank, unique among tools |
| `name` | string | no | defaults to `id` |
| `views` | list | no | default `["2d", "3d"]`; non-empty, from those two |
| `menu` | `"tools"` or null | no | as for commands |
| `mode` | `"gesture"` (default) or `"fire"` | no | else `mode '…' not in […]` |
| `cancel_on_camera_move` | boolean | no | default `true` |
| `wants_pick` | boolean | no | default `true`: the host computes its surface pick for this tool's 3D presses (`doorbell_E_input.md` §3). `false` skips it and the tool gets the bare ray. A request about what the host computes, not a filter: a tool takes a record with or without a pick whichever it declared. Else `wants_pick must be a boolean` |
| `triggers` | list | no | §3.6.1 |
| `actions` | list | no | §3.6.2 |
| `pane` | object | no | §3.6.3 |
| `view_targets` | list | no | set-view: what a FIRE of this tool may move, from §3.3's names; the plugin must then have a panel that declares `view_targets`, whose move-the-view row governs the tool |

**The primary pointer means whatever the ENGAGED tool means.**  While a tool
is engaged it owns the primary button in its `views` and the host forwards
press / drag / release as voxel-space `E` records (`doorbell_E_input.md`); a
`"fire"` tool receives a `FIRE` per press instead.  One tool is engaged at a
time.  A tool never names the primary button: it is engaged — from the Tools
menu, the control pane, or a trigger — and then the button is its.

`binding` is refused by name (`binding is not a field`): it was replaced by
`triggers`, `actions` and `pane`, and ignoring it would give the author a tool
bound to nothing they chose and no way to learn why.

#### 3.6.1 `triggers` — what ENGAGES the tool

A list of **requests** (`control_plane_protocol.md` §8.10), never assignments:
the host owns the namespace and either honors each or reports why not — two
plugins asking for one trigger leaves neither bound; a trigger the host uses
is refused with the host keeping it.  A refused trigger is not a refused tool.

| Field | Type | Rule |
|---|---|---|
| `key` | string | a portable key name (`"Ctrl+J"`, `"Space"`); carries its own modifiers |
| `button` | string | `"right"` or `"middle"`, or `"left"` **with modifiers**; else `button '…' not in […]` |
| `modifiers` | list of strings (a single string is accepted) | from `"shift"`, `"ctrl"`, `"meta"`; qualifies `button` only |
| `hold` | `"toggle"` or `"momentary"` | keys default `"toggle"`; a mouse trigger is `"momentary"` and may not say otherwise |

**Exactly one of `key` or `button`** (`a trigger names a key or a button, not
both or neither`).  A `"toggle"` key is the sticky pane button by keyboard —
press once to engage, again to disengage.  A `"momentary"` trigger engages
the tool only while it is held and hands the pointer back to whatever was
engaged before; a mouse chord is down or up and nothing else, so it is always
momentary (`a mouse trigger is momentary`).

Three things a mouse trigger may not be, each refused at parse:

- **the bare left button** — it is the primary and means whatever the engaged
  tool means (`the bare left button is never a trigger`);
- **an Alt chord** — most window managers take Alt+drag before the
  application sees it (`alt is not a mouse modifier`).  Alt remains usable in
  a *key* name;
- **a chord the host keeps** — refused at resolution, not at parse, because
  the set depends on the tool's `views` (§8.10 prints it): in 3D the left and
  right buttons bare, with Shift and with Ctrl; in 2D both bare and with Ctrl.
  Free by construction: Meta chords, the middle button and its chords, and
  multi-modifier chords such as Shift+Ctrl+left.

**Names, never platform codes**: `"button": 4` is `Qt::MiddleButton` leaking
into a document a third party writes, which is `doorbell_E_input.md` §3's
rule applied to the manifest — the host stores the wire's neutral encoding.
A key trigger with `modifiers` is refused (`a key trigger carries its
modifiers in the key name`); the same trigger twice is `duplicate trigger`.

#### 3.6.2 `actions` — what the ENGAGED tool can be told, mid-gesture

The standard modality: the mouse is the target and a key is the fire — a node
placed on a polygon *while dragging*.  An action reaches the engaged tool as an
`E` record with `phase = FIRE` and `action` = the action's 1-based index in
this list, carrying the pointer's current sample; an open gesture is neither
closed nor disturbed (`doorbell_E_input.md` §1).

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | non-blank, unique among the tool's actions |
| `name` | string | no | defaults to `id` |
| `key` | string | no | a portable key name; live only while the tool is engaged |
| `pane` | object | no | §3.6.3, a momentary button that fires the action once |

**An action needs a key or a pane button** (`an action needs a key or a pane
button to be reachable`).  At most 64 per tool (`more than 64 actions`).  An
action key equal to one of the same tool's triggers, or to another of its
actions, is refused at parse (`is also one of this tool's triggers`, `is also
another action's`).  Because an action key is live only while its tool is
engaged, two plugins may both use `"N"`; the host refuses an action key only
where a *global* key would fire at the same press — one the host holds, or one
another plugin's trigger holds — and the announcement carries `""` for it.

#### 3.6.3 `pane` — a button on the control pane

Declared on a tool, an action (§3.6.2) or a command (§3.2).  The host builds
the pane; the plugin declares what its button looks like and, for a tool, how
it holds.

| Field | Type | Required | Rule |
|---|---|---|---|
| `icon` | string | yes | a path relative to the manifest directory, `.svg` or `.png`, at most 64 KiB; no `..`, never absolute |
| `tooltip` | string | no | at most 128 characters |
| `hold` | `"toggle"` or `"momentary"` | no | a **tool's** button: `"toggle"` (default) is sticky, tap on and tap off; `"momentary"` engages the tool for one gesture. An action's or command's button fires once on click and may not say otherwise |

**The icon is checked when the manifest is read** — the file exists, its
suffix is one the host renders, it is small, and its first bytes are what its
suffix claims (`not found in the plugin directory`, `is not one of`, `is N
bytes; the limit is`, `is not a PNG file`, `is not an SVG file`).  The host
renders it at registration, and a failure THERE refuses the plugin too.  A
pane with half its buttons blank is worse than no plugin: **a tool installs
whole or not at all** (a refused *trigger* is the one partial outcome, and it
is reported).

---

### 3.7 `stages` — "I edit a volume in place, in a chain"

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | non-blank, unique among stages |
| `name` | string | no | defaults to `id` |
| `params` | array | no | the stage's settings, a flat list (below) |

Each `params` entry:

| Field | Type | Required | Rule |
|---|---|---|---|
| `key` | string | yes | non-blank, unique within the stage |
| `name` | string | no | defaults to `key` |
| `type` | string | yes | `float`, `int`, `bool`, `choice` or `string` |
| `value` | per `type` | yes | the default; a number for `float` (finite), an integer for `int` (within signed 64-bit, `Value.i`'s range), a boolean for `bool`, one of `options` for `choice`, a string for `string` (at most `max_length` UTF-8 bytes) |
| `min`, `max` | per `type` | no | `float` and `int` only, numbers of the same kind (finite; within signed 64-bit for `int`); `min ≤ max`, and `value` within them |
| `options` | array | `choice` only | non-empty, distinct strings |
| `max_length` | integer | no, `string` only | 1 to 4096, the most UTF-8 bytes a value may hold; defaults to 4096 |

**A `string` is for a setting that is text**: a tool's own option syntax
(`--align=rigid --run=3`), a model name, a run label. It exists so that a
stage whose output depends on something the plugin computed can name that
something in a param, because saved output is keyed by the params.
The plugin validates the text in `PlanStage` and refuses a bad one
`UNSUPPORTED` with a sentence. A host compares it **exactly, byte for byte**:
it does not trim, fold case or reorder, so two spellings of one setting are
two identities. That is the safe direction, and it means a plugin that wants
saved output reused should document one spelling. The bound is in UTF-8 bytes
rather than characters so that every SDK counts it the same way; the 4096
ceiling keeps the saved-output key bounded. **A `string` is not private**:
the applied filter's parameters, strings included, reach every plugin that
declares `data_access` (`ViewState.filter`, `DataVolume.filter`) and are
written beside the file and into every export's provenance record — so a
string holds a setting, never a credential or a path the person would not
want recorded.

A field the entry's `type` does not take is rejected. The host sends a value
for every key, in both `PlanStage` and `StageArm`, as the `Value` kind the
type names (`processing_stages.md` §2).

A processing stage (`processing_stages.md`): the host negotiates what it
will produce (`PlanStage`), arms it at a position in a chain (`StageArm`), and
hands it volumes in a shared ring of `STAGED` slots (purpose `S`). A host that
does not drive a chain ignores the kind, as it ignores any it does not know, so
declaring one needs no change to the manifest.

### 3.8 `region_types` — "I compute the shape of regions of this type"

*A host that does not know the kind ignores it, as it ignores any it
does not know, and refuses the plugin's regions of these types at
`ContributeRois`, as it refuses every non-built-in type — so declaring one
needs no change to the manifest.*

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | the `Roi.type_name` this plugin shapes: starts with an ASCII letter or digit; then ASCII letters, digits, `.`, `_`, `-`; unique among region types; not `box`, `ellipsoid`, `cylinder` or `surfq6`, and not beginning `vizi.` |
| `name` | string | no | what a person reads — the Plugin Manager and the region's own row; defaults to `id` |

**Needs the `contribute_rois` permission** (§5): a type exists to be the type
of regions the plugin contributes, and is rejected without it.

A region whose `type_name` is one of these is **asked**: when the person drags
one of its handles, the host asks the plugin where every vertex goes, and the
plugin answers (`control_plane_protocol.md` §8.8, *Region types a plugin
shapes*). A region of any other type the plugin contributes is enacted by the
host from its data alone. The names are the plugin's own — two plugins may each
declare `tetra`, and they are two types.

## 4. `assets` — immutable files the host provisions

Top-level, not a contribution. An asset is a model or data file the plugin
needs, which the host obtains, verifies and exposes; the plugin requests it at
runtime (`control_plane_protocol.md` §8, `RequestAsset`).

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | starts with an ASCII letter or digit; then ASCII letters, digits, `.`, `_`, `-`; unique among assets |
| `name` | string | no | defaults to `id` |
| `filename` | string | yes | one plain file name: no path separators, not dot-leading |
| `size` | integer | yes | positive; the exact byte count |
| `sha256` | string | yes | exactly 64 hex characters (stored lower-case) |
| `source` | object | yes | `kind` is `"manual"` or `"https"` |
| `source.url` | string | with `https` only | an `https://` URL with a host and no credentials; **forbidden** with `manual` |

Every provisioned file is accepted only after its size and SHA-256 match the
declaration; a manifest cannot describe a file it does not pin.

---

## 5. `permissions` — host capabilities the plugin asks for

A list of strings (`permissions must be a list of strings`). The host enforces
a grant for each of the **known** set:

| Value | Since | Gates |
|---|---|---|
| `data_access` | 1.0 | active-dataset reads (`GetViewState`, `ReadDataVolume`), ROI read-back, and runtime asset requests |
| `contribute_rois` | 1.0 | native ROI contribution and label-volume compilation |

Declared is granted, and the grant is visible in the Plugin Manager; there is
no per-plugin consent prompt. **An unknown permission string is
accepted with a warning and grants nothing** — append-only, so a manifest
written for a newer host still loads on this one; the author sees the warning
in the host's log.

**A name is honored from the protocol minor that introduced it** — the
*Since* column. To a manifest whose `engines.vizi` minor is older, a later
name is an unknown one: warned about and ignored, granting nothing. A plugin
that uses a name a later 1.x added says so in `engines.vizi`, which is what
keeps a host that knows the name from honoring it for a plugin that did not
ask for that 1.x. The same holds for §5.1's names.

---

### 5.1 `subscriptions` — host pushes the session asks to be arranged

A list of names. An unknown name is **warned and ignored**, exactly as an
unknown permission is.

| Name | Since | What it arranges |
|---|---|---|
| `epochs` | 1.0 | `EpochChange` for a session that declares no tool (`control_plane_protocol.md` §8.7) — geometry, channels and time mapping; **not** the current `t` |
| `view` | 1.0 | `ViewChanged`: the dataset generation, `t`, `z`, the visible channels, the selected region and the applied filter, whenever one changes |
| `regions` | 1.0 | `RegionsChanged`: which regions were created, edited or deleted, by id, revision and origin |

**`view` and `regions` need the `data_access` permission.** They push what
`ViewState` and `GetRois` answer, which it gates; declared without it, each is
warned and ignored like an unknown name.

**A subscription is not a permission**, and the two lists are separate for a
reason rather than by accident. A permission says what a session may *do*; a
subscription says what it wants *sent*. `data_access` does not imply `epochs`,
because an arranged epoch is not free: §8.10 obliges the session to `EpochAck`,
with a 10 s deadline and escalation behind it, so a plugin that never acks
would be escalated for a push it never asked for.

**A tool session is on the chain whether or not it says so.** Its `E` records
are uninterpretable without an epoch, so a tool needs no declaration.

**Why a list rather than a field per push.** A name states what the session
wants sent, and the next arranged push is a name rather than another top-level
field and another row in §7.

**`subscriptions` needs no `manifest_version`.** §7 ignores unknown top-level
fields, so a host that does not know the field reads the manifest unchanged and
arranges nothing.

---

## 6. What a host derives from the manifest

Several fields are **structural**, not decorative — the channels size
themselves from them, which is why an SDK author needs to know they are read:

- **The coalescing-key capacity** of the session's inbound queue
  (`control_plane_protocol.md` §5.4) is one key per declared widget plus one
  per declared overlay, plus a fixed slack per **chunked** overlay (its label
  table and the chunk keys the host may name — the host names them, so the
  term is a constant rather than data-scaled), plus a fixed headroom. It is
  computed from the manifest
  at launch and handed to the transport, because no constant can stand in —
  widgets are capped per panel and panels are not capped, so a plugin declaring
  seventeen full panels crosses any fixed ceiling while declaring every key it
  uses.
- **The subject set** — the ids an `O`/`P` grant's `subject` may name
  (`shared_memory_protocol.md` §3, §8) is the manifest's overlay ids and panel
  ids, registered for the session at launch. A grant naming anything else is a
  host bug the host refuses before the capability goes out.
- **Lazy launch**: readers launch on a matching open, commands and tools on
  invocation, panels on open, exporters on an export, and a plugin with
  `region_types` when a region of one of its types is selected (§3.8). A
  plugin with no contribution is never launched.

---

## 7. Compatibility posture, stated field by field

The default is **ignore what you do not know**, and these are the places the
default does not hold, each because ignoring would mislead the author:

| Where | Unknown value | Treatment |
|---|---|---|
| top-level and per-entry fields | any name this document does not list | **ignored** |
| `manifest_version` | not `1` | **rejected** (`manifest_version N unsupported`) |
| widget `kind` | not in §3.3.1 | **rejected** — a panel with holes has no diagnostic |
| `menu` | not `"tools"` / null | **rejected** — a menu that does not exist |
| `views` | not `"2d"` / `"3d"` | **rejected** |
| `binding` | — | **rejected** by name (§3.6) |
| `permissions` | a string not in §5 | **warned and ignored** |
| `subscriptions` | a name not in §5.1 | **warned and ignored** |
| a widget field its `kind` does not take — `text_format`, `filter`, `columns` or `sortable` (§3.3.1 says which kind takes each) | any value, even the default | **rejected** — a field that means nothing on its kind is a mistake to name |
| a boolean widget field its `kind` does not take — `follows_selection`, `multiple`, `selectable`, `moves_view` | `true` | **rejected**; `false`, which asks for nothing, is accepted |
| `command` on an `image` | — | **must name a declared command**, and makes the image clickable (§3.3.1) |
| `overlays`, with nothing declared that starts the plugin (§3.5) | — | **rejected** — such a plugin could never run; a keybinding or trigger alone is not a start |
| a `label`'s text, with no `text_format` | — | **shown as written**, not as Qt rich text; `"text_format": "rich"` asks for rich text (§3.3.1) |

**`manifest_version` is 1, through every 1.x.** A 1.x addition raises
`engines.vizi` (§2.1), never `manifest_version`; `engines` is read first so
that a manifest written for a later 1.x is refused for that.

**A 1.x addition supplements a required field, never replaces it.** A
manifest a 1.0 host reads keeps every field §2 requires, in the place and form
§2 gives it, whatever a later 1.x adds beside it: `min_vizi` narrows what
`engines.vizi` admits and does not stand in for it.

**Every closed set and every cap is append-only within 1.x, and widened only
with an `engines` minor** — save the three marked *frozen*, which no 1.x
changes, because a host must read them before it knows which 1.x a manifest
was written for (`manifest_version`, `engines.vizi`) or because a folder is
named by them (`id`). A later 1.x may add a value to a set or raise a cap;
a manifest using it declares that minor in `engines.vizi`, so an older host
refuses it for its version (§2.1) and not for the value. No 1.x removes a value
or lowers a cap.

| Closed set or cap | 1.0 | Outside it |
|---|---|---|
| `manifest_version` (*frozen*) | `1` | `manifest_version N unsupported` |
| `id` (*frozen*) | §1's grammar, 3 to 64 characters | `id '…' is not a plugin id: …` |
| `engines.vizi` form (*frozen*) | `major.minor`, `major`, `major.x`; optional `^`, `~`, `>=` | `engines.vizi '…' is not a version` |
| `min_vizi` form | §2.4's PEP 440 subset | `min_vizi '…' is not a PEP 440 version` |
| widget `kind` | §3.3.1's sixteen | rejected (§3.3.1) |
| panel depth | 8 levels | rejected |
| widgets in a panel | 256 | rejected |
| a list's `max_rows` | 1024 (default 256) | rejected |
| a table's `columns` | 1 to 32 | rejected |
| a table's rows | 100,000 | refused whole at runtime (§3.3.1) |
| cells a panel's lists realize together | 4096 | rejected |
| `menu_label` | 128 characters | rejected |
| `text_format` | `plain`, `rich` | rejected |
| `roi_picker` `filter` | `all`, `mine` | rejected |
| `view_targets`, `follow_targets` | §3.3's names | rejected |
| `menu` | `"tools"`, null | rejected |
| `views` | `"2d"`, `"3d"` | rejected |
| tool `mode` | `gesture`, `fire` | rejected |
| `hold` | `toggle`, `momentary` | rejected |
| trigger `button` | `left`, `right`, `middle` | rejected |
| trigger `modifiers` | §3.6.1's set | rejected |
| actions per tool | 64 | rejected |
| pane `icon` | `.svg`, `.png`; 64 KiB | rejected |
| pane `tooltip` | 128 characters | rejected |
| overlay `time` | `per_t`, `static` | rejected |
| stage param `type` | §3.7's types | rejected |
| a string stage param | 4096 bytes | rejected |
| region type ids | not a built-in type, not `vizi.`-prefixed | rejected |
| asset `source.kind` | `manual`, `https` | rejected |
| `permissions` | §5's, each from its *Since* minor | warned and ignored |
| `subscriptions` | §5.1's, each from its *Since* minor | warned and ignored |

---

## 8. Fixtures

Each fenced JSON block below is a complete manifest and is preceded by a
marker naming the outcome the reference parser must produce:

- `<!-- fixture: accepted -->` — parses.
- `<!-- fixture: rejected "…" -->` — is refused, and the diagnostic contains the
  quoted text.

They are executed as written. Add a fixture whenever a rule above changes.

### 8.1 Accepted

The smallest manifest a host reads — no contributions, so never launched:

<!-- fixture: accepted -->
```json
{
  "manifest_version": 1,
  "id": "org.example.minimal",
  "name": "Minimal",
  "version": "0.0.1",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["python3", "plugin.py"] }
}
```

A selectable, sortable table of typed columns (§3.3.1):

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.stats", "name": "Stats", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "stats.pick", "title": "Pick" } ],
    "panels": [ { "id": "p", "title": "Stats", "root": { "kind": "table", "id": "stats", "selectable": true, "multiple": true,
      "command": "stats.pick", "sortable": true, "visible_rows": 12,
      "columns": [ { "id": "name", "header": "Region" },
                   { "id": "area", "header": "Area", "type": "number", "precision": 2, "unit": "µm²" },
                   { "id": "kept", "header": "Kept", "type": "bool" } ] } } ] } }
```

A reader, with extensions in every spelling the host normalizes:

<!-- fixture: accepted -->
```json
{
  "manifest_version": 1,
  "id": "org.example.reader",
  "name": "Reader",
  "version": "1.2.3",
  "engines": { "vizi": "^1.0" },
  "entry": { "command": [".venv/bin/python", "reader.py"] },
  "contributions": {
    "readers": [
      { "id": "raw", "name": "Raw frames", "extensions": [".raw", "RAW", "frames"] }
    ]
  }
}
```

Commands and a panel using every widget kind, each interactive widget bound
to a declared command:

<!-- fixture: accepted -->
```json
{
  "manifest_version": 1,
  "id": "org.example.panel",
  "name": "Panel",
  "version": "0.1.0",
  "engines": { "vizi": "1" },
  "entry": { "command": ["python3", "panel.py"] },
  "contributions": {
    "commands": [
      { "id": "run", "title": "Run", "description": "Run once", "default_keybinding": "Ctrl+Shift+R" },
      { "id": "set", "title": "Set a value", "menu": null }
    ],
    "panels": [
      {
        "id": "main",
        "title": "Controls",
        "menu_label": "Example controls",
        "root": {
          "kind": "column", "id": "root", "children": [
            { "kind": "label", "id": "hint", "text": "Adjust, then run" },
            { "kind": "row", "id": "r1", "children": [
              { "kind": "slider", "id": "gain", "command": "set", "min": 0, "max": 10, "step": 0.5, "value": 2.5, "float": true },
              { "kind": "checkbox", "id": "on", "command": "set", "text": "Enabled", "value": true }
            ]},
            { "kind": "dropdown", "id": "mode", "command": "set", "options": ["fast", "exact"], "value": "exact" },
            { "kind": "line_edit", "id": "note", "command": "set", "placeholder": "a note" },
            { "kind": "image", "id": "preview" },
            { "kind": "button", "id": "go", "command": "run", "text": "Run" }
          ]
        }
      }
    ]
  }
}
```

An exporter and an overlay with an explicit arena size:

<!-- fixture: accepted -->
```json
{
  "manifest_version": 1,
  "id": "org.example.draw_and_write",
  "name": "Draw and write",
  "version": "0.3.0",
  "engines": { "vizi": ">=1.0" },
  "entry": { "command": ["python3", "plugin.py"] },
  "contributions": {
    "exporters": [ { "id": "csv", "name": "Stats CSV", "extension": "csv" } ],
    "overlays": [ { "id": "tracks", "name": "Tracks", "segment_mib": 64 } ]
  }
}
```

A tool without a binding (the host chooses the gesture):

<!-- fixture: accepted -->
```json
{
  "manifest_version": 1,
  "id": "org.example.tool",
  "name": "Tool",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["python3", "tool.py"] },
  "contributions": {
    "tools": [ { "id": "seeds", "name": "Place seeds", "views": ["3d"], "menu": null } ]
  }
}
```

A version-1 manifest whose tool requests its triggers, declares an action and
asks for pane buttons, with every field present.  The fixture directory holds
`icons/tool.svg` and `icons/go.png`:

<!-- fixture: accepted -->
```json
{
  "manifest_version": 1,
  "id": "org.example.bound_tool",
  "name": "Bound tool",
  "version": "0.2.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["python3", "tool.py"] },
  "contributions": {
    "tools": [
      {
        "id": "polygon",
        "name": "Polygon",
        "views": ["2d"],
        "mode": "gesture",
        "cancel_on_camera_move": false,
        "pane": { "icon": "icons/tool.svg", "tooltip": "Draw a polygon", "hold": "toggle" },
        "triggers": [
          { "key": "P", "hold": "toggle" },
          { "key": "Space", "hold": "momentary" },
          { "button": "middle", "modifiers": ["ctrl", "shift"], "hold": "momentary" }
        ],
        "actions": [
          { "id": "add_node", "name": "Add node", "key": "N",
            "pane": { "icon": "icons/go.png", "tooltip": "Add a node here" } },
          { "id": "close", "name": "Close polygon", "key": "Return" }
        ]
      }
    ],
    "commands": [
      { "id": "reset", "title": "Reset", "default_keybinding": "Ctrl+Shift+R",
        "pane": { "icon": "icons/go.png", "tooltip": "Fit the view" } }
    ]
  }
}
```

A fire tool with a modified left chord, `modifiers` as a single string:

<!-- fixture: accepted -->
```json
{
  "manifest_version": 1,
  "id": "org.example.key_tool",
  "name": "Key tool",
  "version": "0.2.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["python3", "tool.py"] },
  "contributions": {
    "tools": [ { "id": "mark", "mode": "fire",
                 "triggers": [ { "button": "left", "modifiers": "meta" }, { "key": "M" } ] } ]
  }
}
```

Assets of both source kinds, and both known permissions:

<!-- fixture: accepted -->
```json
{
  "manifest_version": 1,
  "id": "org.example.model",
  "name": "Model",
  "version": "2.0.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["python3", "model.py"] },
  "assets": [
    {
      "id": "weights-v2",
      "name": "Segmentation weights",
      "filename": "weights.pt",
      "size": 104857600,
      "sha256": "0123456789ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef",
      "source": { "kind": "https", "url": "https://models.example.org/weights-v2.pt" }
    },
    {
      "id": "lut",
      "filename": "lut.bin",
      "size": 1,
      "sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
      "source": { "kind": "manual" }
    }
  ],
  "permissions": ["data_access", "contribute_rois"]
}
```

Unknown fields everywhere, and an unknown permission — all ignored (the
permission with a warning):

<!-- fixture: accepted -->
```json
{
  "manifest_version": 1,
  "id": "org.example.future",
  "name": "From a newer author",
  "version": "9.9.9",
  "engines": { "vizi": "1.0", "other_host": "3.1" },
  "entry": { "command": ["python3", "plugin.py"], "env": { "X": "1" } },
  "homepage": "https://example.org",
  "contributions": {
    "readers": [ { "id": "r", "extensions": [".x"], "priority": 5 } ],
    "commands": [ { "id": "c", "title": "C", "icon": "star" } ],
    "overlays": [ { "id": "o", "colour": "#ff0000" } ],
    "widgets": []
  },
  "permissions": ["data_access", "teleport"]
}
```

The same id used by a command and a panel — ids are per kind:

<!-- fixture: accepted -->
```json
{
  "manifest_version": 1,
  "id": "org.example.shared_ids",
  "name": "Shared ids",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["python3", "plugin.py"] },
  "contributions": {
    "commands": [ { "id": "main", "title": "Main" } ],
    "panels": [ { "id": "main", "title": "Main", "root": { "kind": "label", "id": "main", "text": "hi" } } ],
    "overlays": [ { "id": "main" } ],
    "tools": [ { "id": "main" } ]
  }
}
```

A plugin that shapes its own region type (§3.8), beside the command that
contributes the regions:

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.tetra", "name": "Tetrahedra", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3", "tetra.py"] },
  "permissions": ["contribute_rois"],
  "contributions": {
    "commands": [ { "id": "add", "title": "Add tetrahedra", "menu": "tools" } ],
    "region_types": [ { "id": "tetra.volume", "name": "Tetrahedron (keeps its volume)" }, { "id": "blob" } ] } }
```

A processing stage with one parameter of each type (§3.7):

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.morph", "name": "Morphology", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3", "morph.py"] },
  "contributions": { "stages": [
    { "id": "blur", "name": "Gaussian blur", "params": [
      { "key": "sigma", "name": "σ (voxels)", "type": "float", "value": 2.0, "min": 0.1, "max": 50 },
      { "key": "passes", "type": "int", "value": 1, "min": 1 },
      { "key": "clip", "type": "bool", "value": false },
      { "key": "edge", "type": "choice", "options": ["reflect", "constant"], "value": "reflect" },
      { "key": "label", "type": "string", "value": "run 1", "max_length": 64 } ] },
    { "id": "erode", "name": "Erosion" } ] } }
```

The release facts, and an id at its edges — three characters, and a later
part with `_` (§1, §2.4):

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "a.b", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "min_vizi": "1.0.0", "sdk_version": "1.0.0", "entry": { "command": ["python3"] } }
```

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example2.my_tool", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "min_vizi": "1.0rc2", "entry": { "command": ["python3"] } }
```

### 8.2 Rejected

Top level:

<!-- fixture: rejected "missing required field 'manifest_version'" -->
```json
{ "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "manifest_version 2 unsupported" -->
```json
{ "manifest_version": 2, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "field 'manifest_version' must be int" -->
```json
{ "manifest_version": "1", "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] } }
```

`manifest_version` 0 is refused for itself, not for a field it lacked:

<!-- fixture: rejected "manifest_version 0 unsupported" -->
```json
{ "manifest_version": 0, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "key": "T" } ] } ] } }
```

`engines` is read first (§2.1): a later minor is refused for that, not for the
widget kind it added:

<!-- fixture: rejected "Widgets 1.2.0 needs vizi protocol 1.1; this vizi speaks 1.0" -->
```json
{ "manifest_version": 1, "id": "org.example.widgets", "name": "Widgets", "version": "1.2.0", "engines": { "vizi": "1.1" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "sparkline", "id": "s" } } ] } }
```

<!-- fixture: rejected "Next 3 needs vizi protocol 2.0; this vizi speaks 1.0" -->
```json
{ "manifest_version": 1, "id": "org.example.next", "name": "Next", "version": "3", "engines": { "vizi": "2.0" }, "entry": { "command": ["python3"] } }
```

The id (§1):

<!-- fixture: rejected "id 'org.Example.tool' is not a plugin id" -->
```json
{ "manifest_version": 1, "id": "org.Example.tool", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "id 'exampletool' is not a plugin id" -->
```json
{ "manifest_version": 1, "id": "exampletool", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "id '1org.example' is not a plugin id" -->
```json
{ "manifest_version": 1, "id": "1org.example", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "id 'org..example' is not a plugin id" -->
```json
{ "manifest_version": 1, "id": "org..example", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "id 'org.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' is not a plugin id" -->
```json
{ "manifest_version": 1, "id": "org.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] } }
```

`min_vizi` and `sdk_version` (§2.4):

<!-- fixture: rejected "min_vizi 'v0.9' is not a PEP 440 version" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "min_vizi": "v0.9", "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "sdk_version must be a non-empty string" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "sdk_version": "", "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "id must be non-empty" -->
```json
{ "manifest_version": 1, "id": "   ", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "missing required field 'version'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "missing required field 'name'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "missing required field 'vizi'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": {}, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "engines.vizi 'one.zero' is not a version" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "one.zero" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "engines.vizi '1.0.3' is not a version" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0.3" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "engines.vizi '1.*' is not a version" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.*" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "engines.vizi '1.-1' has a negative component" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.-1" }, "entry": { "command": ["python3"] } }
```

<!-- fixture: rejected "entry.command must be a non-empty list of strings" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": [] } }
```

<!-- fixture: rejected "entry.command must be a non-empty list of strings" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3", 3] } }
```

<!-- fixture: rejected "contributions must be an object" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] }, "contributions": [] }
```

<!-- fixture: rejected "permissions must be a list of strings" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] }, "permissions": "data_access" }
```

Readers:

<!-- fixture: rejected "contributions.readers must be a list" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "readers": { "id": "r", "extensions": [".x"] } } }
```

<!-- fixture: rejected "extensions must be non-empty" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "readers": [ { "id": "r", "extensions": [] } ] } }
```

<!-- fixture: rejected "each extension must be a non-empty string" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "readers": [ { "id": "r", "extensions": [".x", ""] } ] } }
```

<!-- fixture: rejected "each fallback extension must be a non-empty string" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "readers": [ { "id": "r", "fallback_extensions": ["."] } ] } }
```

<!-- fixture: rejected "each extension must be a non-empty string" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "readers": [ { "id": "r", "extensions": ["."] } ] } }
```

<!-- fixture: rejected "each extension must be a non-empty string" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "readers": [ { "id": "r", "extensions": ["."], "fallback_extensions": ".h5" } ] } }
```

<!-- fixture: rejected "each fallback extension must be a non-empty string" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "readers": [ { "id": "r", "extensions": [".x"], "fallback_extensions": [". \t"] } ] } }
```

<!-- fixture: rejected "fallback_extensions must be a list" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "readers": [ { "id": "r", "extensions": [".x"], "fallback_extensions": ".h5" } ] } }
```

<!-- fixture: rejected "a suffix is in both extensions and fallback_extensions" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "readers": [ { "id": "r", "extensions": [".h5"], "fallback_extensions": ["H5"] } ] } }
```

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "readers": [ { "id": "r", "extensions": [".nwb"], "fallback_extensions": [".h5", ".hdf5"] } ] } }
```

<!-- fixture: rejected "duplicate reader id 'r'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "readers": [ { "id": "r", "extensions": [".x"] }, { "id": "r", "extensions": [".y"] } ] } }
```

Commands:

<!-- fixture: rejected "missing required field 'title'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "run" } ] } }
```

<!-- fixture: rejected "duplicate command id 'run'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "run", "title": "A" }, { "id": "run", "title": "B" } ] } }
```

<!-- fixture: rejected "default_keybinding must be a non-empty string" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "run", "title": "A", "default_keybinding": " " } ] } }
```

<!-- fixture: rejected "menu 'file' not allowed" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "run", "title": "A", "menu": "file" } ] } }
```

Panels and widgets:

<!-- fixture: rejected "duplicate panel id 'p'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [
    { "id": "p", "title": "P", "root": { "kind": "label", "id": "a" } },
    { "id": "p", "title": "Q", "root": { "kind": "label", "id": "a" } } ] } }
```

<!-- fixture: rejected "menu_label must be at most 128 characters" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P",
    "menu_label": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "root": { "kind": "label", "id": "a" } } ] } }
```

<!-- fixture: rejected "menu_label must be a non-empty string" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "menu_label": " ", "root": { "kind": "label", "id": "a" } } ] } }
```

<!-- fixture: rejected "widget id must be non-empty" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "label", "id": "" } } ] } }
```

<!-- fixture: rejected "unknown widget kind 'knob'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "knob", "id": "a" } } ] } }
```

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "label", "id": "a", "text": "<b>bold</b>", "text_format": "rich" } } ] } }
```

<!-- fixture: rejected "text_format must be one of ['plain', 'rich']" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "label", "id": "a", "text_format": "html" } } ] } }
```

<!-- fixture: rejected "does not take text_format; only a label does" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "c", "title": "C", "menu": null } ],
                     "panels": [ { "id": "p", "title": "P", "root": { "kind": "button", "id": "a", "command": "c", "text_format": "rich" } } ] } }
```

<!-- fixture: rejected "duplicate widget id 'a'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "row", "id": "a", "children": [ { "kind": "label", "id": "a" } ] } } ] } }
```

<!-- fixture: rejected "button widget 'go' must bind a declared command" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "run", "title": "Run" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "button", "id": "go", "command": "walk" } } ] } }
```

<!-- fixture: rejected "row 'r' has no children" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "row", "id": "r", "children": [] } } ] } }
```

<!-- fixture: rejected "slider 's' needs min < max" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "slider", "id": "s", "command": "set", "min": 5, "max": 5 } } ] } }
```

<!-- fixture: rejected "slider 's' needs step > 0" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "slider", "id": "s", "command": "set", "step": 0 } } ] } }
```

<!-- fixture: rejected "slider 's' min must be numeric" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "slider", "id": "s", "command": "set", "min": "low" } } ] } }
```

<!-- fixture: rejected "slider 's' float must be a boolean" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "slider", "id": "s", "command": "set", "float": "yes" } } ] } }
```

<!-- fixture: rejected "label 'l' text must be a string" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "label", "id": "l", "text": 5 } } ] } }
```

<!-- fixture: rejected "progress 'bar' value must be numeric" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "progress", "id": "bar", "value": "half" } } ] } }
```

<!-- fixture: rejected "progress 'bar' value must be within [0, 1]" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "progress", "id": "bar", "value": 1.5 } } ] } }
```

<!-- fixture: rejected "progress 'bar' indeterminate must be a boolean" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "progress", "id": "bar", "indeterminate": "yes" } } ] } }
```

<!-- fixture: rejected "slider 's' value must be numeric" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "slider", "id": "s", "command": "set", "value": "high" } } ] } }
```

<!-- fixture: rejected "dropdown 'd' needs non-empty options" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "dropdown", "id": "d", "command": "set" } } ] } }
```

<!-- fixture: rejected "dropdown 'd' default 'slow' not in options" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "dropdown", "id": "d", "command": "set", "options": ["fast"], "value": "slow" } } ] } }
```

A panel with each host picker:

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "column", "id": "c", "children": [
      { "kind": "channel_picker", "id": "ch", "command": "set", "multiple": true, "value": [0, 2] },
      { "kind": "color_picker", "id": "col", "command": "set", "value": "#FF8800" },
      { "kind": "colormap_picker", "id": "cm", "command": "set", "value": "viridis" },
      { "kind": "roi_picker", "id": "roi", "command": "set" } ] } } ] } }
```

<!-- fixture: rejected "color_picker 'col' value must be a #RRGGBB color" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "color_picker", "id": "col", "command": "set", "value": "orange" } } ] } }
```

<!-- fixture: rejected "channel_picker 'ch' value must be a channel index" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "channel_picker", "id": "ch", "command": "set", "value": [0, 1] } } ] } }
```

<!-- fixture: rejected "dropdown 'd' does not take multiple" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "dropdown", "id": "d", "command": "set", "options": ["a"], "multiple": true } } ] } }
```

`columns` and `sortable` belong to a `table` (§7):

<!-- fixture: rejected "label 'name' does not take columns or sortable" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": {
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "label", "id": "name", "text": "Cells", "sortable": true } } ] } }
```

A list whose rows carry a label, a checkbox, an image and a picker:

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "list", "id": "cells", "selectable": true, "command": "set", "max_rows": 64,
      "row": [ { "kind": "label", "id": "name" }, { "kind": "checkbox", "id": "on", "command": "set" },
               { "kind": "image", "id": "spark" }, { "kind": "channel_picker", "id": "ch", "command": "set" } ] } } ] } }
```

<!-- fixture: rejected "list 'cells' row[0] is a row; a row holds leaf widgets only" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "list", "id": "cells",
      "row": [ { "kind": "row", "id": "inner", "children": [ { "kind": "label", "id": "n" } ] } ] } } ] } }
```

<!-- fixture: rejected "list 'cells' has an empty row" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "list", "id": "cells", "row": [] } } ] } }
```

<!-- fixture: rejected "list 'cells' max_rows must be an integer in [1, 1024]" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "list", "id": "cells", "max_rows": 5000,
      "row": [ { "kind": "label", "id": "n" } ] } } ] } }
```

<!-- fixture: rejected "list 'cells' may realize at most 4096 cells (max_rows × row cells), not 1024 × 5" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "list", "id": "cells", "max_rows": 1024,
      "row": [ { "kind": "label", "id": "a" }, { "kind": "label", "id": "b" }, { "kind": "label", "id": "c" },
               { "kind": "label", "id": "d" }, { "kind": "label", "id": "e" } ] } } ] } }
```

<!-- fixture: rejected "panel 'p''s lists may realize at most 4096 cells together (each list's max_rows × row cells), not 5120" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "column", "id": "c", "children": [
    { "kind": "list", "id": "one", "max_rows": 512,
      "row": [ { "kind": "label", "id": "a1" }, { "kind": "label", "id": "b1" }, { "kind": "label", "id": "c1" },
                 { "kind": "label", "id": "d1" }, { "kind": "label", "id": "e1" } ] },
    { "kind": "list", "id": "two", "max_rows": 512,
      "row": [ { "kind": "label", "id": "a2" }, { "kind": "label", "id": "b2" }, { "kind": "label", "id": "c2" },
                 { "kind": "label", "id": "d2" }, { "kind": "label", "id": "e2" } ] } ] } } ] } }
```

A table's columns:

<!-- fixture: rejected "table 'stats' needs 1 to 32 columns, not 0" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "table", "id": "stats", "columns": [] } } ] } }
```

<!-- fixture: rejected "only a number column takes precision or unit" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "table", "id": "stats",
    "columns": [ { "id": "name", "unit": "µm" } ] } } ] } }
```

<!-- fixture: rejected "precision must be an integer in [0, 15]" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "table", "id": "stats",
    "columns": [ { "id": "area", "type": "number", "precision": 20 } ] } } ] } }
```

An integer slider's step is whole:

<!-- fixture: rejected "slider 'level' step 2.5 is not a whole number" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "slider", "id": "level", "command": "set", "min": 0, "max": 100, "step": 2.5 } } ] } }
```

<!-- fixture: rejected "list 'cells' binds a command only when selectable" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "list", "id": "cells", "command": "set",
      "row": [ { "kind": "label", "id": "n" } ] } } ] } }
```

<!-- fixture: rejected "list 'cells' selectable must be a boolean" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "list", "id": "cells", "selectable": "yes",
      "row": [ { "kind": "label", "id": "n" } ] } } ] } }
```

A panel whose trace image and frame slider may move the view, and a region
picker that follows vizi's selection:

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "view_targets": ["t", "select_region"], "root": { "kind": "column", "id": "c", "children": [
      { "kind": "image", "id": "trace", "command": "set", "moves_view": true },
      { "kind": "slider", "id": "frame", "command": "set", "min": 0, "max": 100, "moves_view": true },
      { "kind": "roi_picker", "id": "roi", "command": "set", "follows_selection": true, "filter": "mine" } ] } } ] } }
```

<!-- fixture: rejected "panel 'p' has moves_view widgets and declares no view_targets" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "button", "id": "go", "command": "set", "moves_view": true } } ] } }
```

<!-- fixture: rejected "view_targets must be distinct names from" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "view_targets": ["camera"], "root": { "kind": "label", "id": "l" } } ] } }
```

<!-- fixture: rejected "image widget 'trace' must bind a declared command" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "image", "id": "trace", "command": "nope" } } ] } }
```

<!-- fixture: rejected "roi_picker 'roi' filter must be one of" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "set", "title": "Set" } ],
    "panels": [ { "id": "p", "title": "P", "root": { "kind": "roi_picker", "id": "roi", "command": "set", "filter": "theirs" } } ] } }
```

A panel that offers a Follow over frames and regions:

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "follow_targets": ["t", "frame_region"], "root": { "kind": "label", "id": "l" } } ] } }
```

<!-- fixture: rejected "follow_targets must be distinct names from" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "follow_targets": ["t", "t"], "root": { "kind": "label", "id": "l" } } ] } }
```

<!-- fixture: rejected "tool 'pick' declares view_targets but no panel does" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "pick", "mode": "fire", "view_targets": ["t"] } ] } }
```

<!-- fixture: rejected "panel has more than 256 widgets" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "column", "id": "root", "children": [
    { "kind": "label", "id": "w0" }, { "kind": "label", "id": "w1" }, { "kind": "label", "id": "w2" }, { "kind": "label", "id": "w3" }, { "kind": "label", "id": "w4" }, { "kind": "label", "id": "w5" }, { "kind": "label", "id": "w6" }, { "kind": "label", "id": "w7" }, { "kind": "label", "id": "w8" }, { "kind": "label", "id": "w9" }, { "kind": "label", "id": "w10" }, { "kind": "label", "id": "w11" }, { "kind": "label", "id": "w12" }, { "kind": "label", "id": "w13" }, { "kind": "label", "id": "w14" }, { "kind": "label", "id": "w15" }, { "kind": "label", "id": "w16" }, { "kind": "label", "id": "w17" }, { "kind": "label", "id": "w18" }, { "kind": "label", "id": "w19" }, { "kind": "label", "id": "w20" }, { "kind": "label", "id": "w21" }, { "kind": "label", "id": "w22" }, { "kind": "label", "id": "w23" }, { "kind": "label", "id": "w24" }, { "kind": "label", "id": "w25" }, { "kind": "label", "id": "w26" }, { "kind": "label", "id": "w27" }, { "kind": "label", "id": "w28" }, { "kind": "label", "id": "w29" }, { "kind": "label", "id": "w30" }, { "kind": "label", "id": "w31" }, { "kind": "label", "id": "w32" }, { "kind": "label", "id": "w33" }, { "kind": "label", "id": "w34" }, { "kind": "label", "id": "w35" }, { "kind": "label", "id": "w36" }, { "kind": "label", "id": "w37" }, { "kind": "label", "id": "w38" }, { "kind": "label", "id": "w39" }, { "kind": "label", "id": "w40" }, { "kind": "label", "id": "w41" }, { "kind": "label", "id": "w42" }, { "kind": "label", "id": "w43" }, { "kind": "label", "id": "w44" }, { "kind": "label", "id": "w45" }, { "kind": "label", "id": "w46" }, { "kind": "label", "id": "w47" }, { "kind": "label", "id": "w48" }, { "kind": "label", "id": "w49" }, { "kind": "label", "id": "w50" }, { "kind": "label", "id": "w51" }, { "kind": "label", "id": "w52" }, { "kind": "label", "id": "w53" }, { "kind": "label", "id": "w54" }, { "kind": "label", "id": "w55" }, { "kind": "label", "id": "w56" }, { "kind": "label", "id": "w57" }, { "kind": "label", "id": "w58" }, { "kind": "label", "id": "w59" }, { "kind": "label", "id": "w60" }, { "kind": "label", "id": "w61" }, { "kind": "label", "id": "w62" }, { "kind": "label", "id": "w63" }, { "kind": "label", "id": "w64" }, { "kind": "label", "id": "w65" }, { "kind": "label", "id": "w66" }, { "kind": "label", "id": "w67" }, { "kind": "label", "id": "w68" }, { "kind": "label", "id": "w69" }, { "kind": "label", "id": "w70" }, { "kind": "label", "id": "w71" }, { "kind": "label", "id": "w72" }, { "kind": "label", "id": "w73" }, { "kind": "label", "id": "w74" }, { "kind": "label", "id": "w75" }, { "kind": "label", "id": "w76" }, { "kind": "label", "id": "w77" }, { "kind": "label", "id": "w78" }, { "kind": "label", "id": "w79" }, { "kind": "label", "id": "w80" }, { "kind": "label", "id": "w81" }, { "kind": "label", "id": "w82" }, { "kind": "label", "id": "w83" }, { "kind": "label", "id": "w84" }, { "kind": "label", "id": "w85" }, { "kind": "label", "id": "w86" }, { "kind": "label", "id": "w87" }, { "kind": "label", "id": "w88" }, { "kind": "label", "id": "w89" }, { "kind": "label", "id": "w90" }, { "kind": "label", "id": "w91" }, { "kind": "label", "id": "w92" }, { "kind": "label", "id": "w93" }, { "kind": "label", "id": "w94" }, { "kind": "label", "id": "w95" }, { "kind": "label", "id": "w96" }, { "kind": "label", "id": "w97" }, { "kind": "label", "id": "w98" }, { "kind": "label", "id": "w99" }, { "kind": "label", "id": "w100" }, { "kind": "label", "id": "w101" }, { "kind": "label", "id": "w102" }, { "kind": "label", "id": "w103" }, { "kind": "label", "id": "w104" }, { "kind": "label", "id": "w105" }, { "kind": "label", "id": "w106" }, { "kind": "label", "id": "w107" }, { "kind": "label", "id": "w108" }, { "kind": "label", "id": "w109" }, { "kind": "label", "id": "w110" }, { "kind": "label", "id": "w111" }, { "kind": "label", "id": "w112" }, { "kind": "label", "id": "w113" }, { "kind": "label", "id": "w114" }, { "kind": "label", "id": "w115" }, { "kind": "label", "id": "w116" }, { "kind": "label", "id": "w117" }, { "kind": "label", "id": "w118" }, { "kind": "label", "id": "w119" }, { "kind": "label", "id": "w120" }, { "kind": "label", "id": "w121" }, { "kind": "label", "id": "w122" }, { "kind": "label", "id": "w123" }, { "kind": "label", "id": "w124" }, { "kind": "label", "id": "w125" }, { "kind": "label", "id": "w126" }, { "kind": "label", "id": "w127" }, { "kind": "label", "id": "w128" }, { "kind": "label", "id": "w129" }, { "kind": "label", "id": "w130" }, { "kind": "label", "id": "w131" }, { "kind": "label", "id": "w132" }, { "kind": "label", "id": "w133" }, { "kind": "label", "id": "w134" }, { "kind": "label", "id": "w135" }, { "kind": "label", "id": "w136" }, { "kind": "label", "id": "w137" }, { "kind": "label", "id": "w138" }, { "kind": "label", "id": "w139" }, { "kind": "label", "id": "w140" }, { "kind": "label", "id": "w141" }, { "kind": "label", "id": "w142" }, { "kind": "label", "id": "w143" }, { "kind": "label", "id": "w144" }, { "kind": "label", "id": "w145" }, { "kind": "label", "id": "w146" }, { "kind": "label", "id": "w147" }, { "kind": "label", "id": "w148" }, { "kind": "label", "id": "w149" }, { "kind": "label", "id": "w150" }, { "kind": "label", "id": "w151" }, { "kind": "label", "id": "w152" }, { "kind": "label", "id": "w153" }, { "kind": "label", "id": "w154" }, { "kind": "label", "id": "w155" }, { "kind": "label", "id": "w156" }, { "kind": "label", "id": "w157" }, { "kind": "label", "id": "w158" }, { "kind": "label", "id": "w159" }, { "kind": "label", "id": "w160" }, { "kind": "label", "id": "w161" }, { "kind": "label", "id": "w162" }, { "kind": "label", "id": "w163" }, { "kind": "label", "id": "w164" }, { "kind": "label", "id": "w165" }, { "kind": "label", "id": "w166" }, { "kind": "label", "id": "w167" }, { "kind": "label", "id": "w168" }, { "kind": "label", "id": "w169" }, { "kind": "label", "id": "w170" }, { "kind": "label", "id": "w171" }, { "kind": "label", "id": "w172" }, { "kind": "label", "id": "w173" }, { "kind": "label", "id": "w174" }, { "kind": "label", "id": "w175" }, { "kind": "label", "id": "w176" }, { "kind": "label", "id": "w177" }, { "kind": "label", "id": "w178" }, { "kind": "label", "id": "w179" }, { "kind": "label", "id": "w180" }, { "kind": "label", "id": "w181" }, { "kind": "label", "id": "w182" }, { "kind": "label", "id": "w183" }, { "kind": "label", "id": "w184" }, { "kind": "label", "id": "w185" }, { "kind": "label", "id": "w186" }, { "kind": "label", "id": "w187" }, { "kind": "label", "id": "w188" }, { "kind": "label", "id": "w189" }, { "kind": "label", "id": "w190" }, { "kind": "label", "id": "w191" }, { "kind": "label", "id": "w192" }, { "kind": "label", "id": "w193" }, { "kind": "label", "id": "w194" }, { "kind": "label", "id": "w195" }, { "kind": "label", "id": "w196" }, { "kind": "label", "id": "w197" }, { "kind": "label", "id": "w198" }, { "kind": "label", "id": "w199" }, { "kind": "label", "id": "w200" }, { "kind": "label", "id": "w201" }, { "kind": "label", "id": "w202" }, { "kind": "label", "id": "w203" }, { "kind": "label", "id": "w204" }, { "kind": "label", "id": "w205" }, { "kind": "label", "id": "w206" }, { "kind": "label", "id": "w207" }, { "kind": "label", "id": "w208" }, { "kind": "label", "id": "w209" }, { "kind": "label", "id": "w210" }, { "kind": "label", "id": "w211" }, { "kind": "label", "id": "w212" }, { "kind": "label", "id": "w213" }, { "kind": "label", "id": "w214" }, { "kind": "label", "id": "w215" }, { "kind": "label", "id": "w216" }, { "kind": "label", "id": "w217" }, { "kind": "label", "id": "w218" }, { "kind": "label", "id": "w219" }, { "kind": "label", "id": "w220" }, { "kind": "label", "id": "w221" }, { "kind": "label", "id": "w222" }, { "kind": "label", "id": "w223" }, { "kind": "label", "id": "w224" }, { "kind": "label", "id": "w225" }, { "kind": "label", "id": "w226" }, { "kind": "label", "id": "w227" }, { "kind": "label", "id": "w228" }, { "kind": "label", "id": "w229" }, { "kind": "label", "id": "w230" }, { "kind": "label", "id": "w231" }, { "kind": "label", "id": "w232" }, { "kind": "label", "id": "w233" }, { "kind": "label", "id": "w234" }, { "kind": "label", "id": "w235" }, { "kind": "label", "id": "w236" }, { "kind": "label", "id": "w237" }, { "kind": "label", "id": "w238" }, { "kind": "label", "id": "w239" }, { "kind": "label", "id": "w240" }, { "kind": "label", "id": "w241" }, { "kind": "label", "id": "w242" }, { "kind": "label", "id": "w243" }, { "kind": "label", "id": "w244" }, { "kind": "label", "id": "w245" }, { "kind": "label", "id": "w246" }, { "kind": "label", "id": "w247" }, { "kind": "label", "id": "w248" }, { "kind": "label", "id": "w249" }, { "kind": "label", "id": "w250" }, { "kind": "label", "id": "w251" }, { "kind": "label", "id": "w252" }, { "kind": "label", "id": "w253" }, { "kind": "label", "id": "w254" }, { "kind": "label", "id": "w255" }
  ] } } ] } }
```

<!-- fixture: rejected "panel tree deeper than 8" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "panels": [ { "id": "p", "title": "P", "root":
    { "kind": "column", "id": "c1", "children": [ { "kind": "column", "id": "c2", "children": [ { "kind": "column", "id": "c3", "children": [
    { "kind": "column", "id": "c4", "children": [ { "kind": "column", "id": "c5", "children": [ { "kind": "column", "id": "c6", "children": [
    { "kind": "column", "id": "c7", "children": [ { "kind": "column", "id": "c8", "children": [ { "kind": "label", "id": "leaf" } ] } ] } ] } ] } ] } ] } ] } ] } } ] } }
```

Stages:

<!-- fixture: rejected "region_types need the contribute_rois permission" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "region_types": [ { "id": "tetra" } ] } }
```

<!-- fixture: rejected "duplicate region type id 'tetra'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "permissions": ["contribute_rois"],
  "contributions": { "region_types": [ { "id": "tetra" }, { "id": "tetra", "name": "Again" } ] } }
```

<!-- fixture: rejected "region type id 'box' is a built-in type" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "permissions": ["contribute_rois"],
  "contributions": { "region_types": [ { "id": "box" } ] } }
```

<!-- fixture: rejected "region type id 'vizi.tetra' is reserved: ids beginning 'vizi.' are the host's" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "permissions": ["contribute_rois"],
  "contributions": { "region_types": [ { "id": "vizi.tetra" } ] } }
```

<!-- fixture: rejected "region type id must start with a letter or digit" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "permissions": ["contribute_rois"],
  "contributions": { "region_types": [ { "id": ".tetra" } ] } }
```

<!-- fixture: rejected "duplicate stage id 'stabilize'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "stabilize" }, { "id": "stabilize", "name": "Again" } ] } }
```

<!-- fixture: rejected "key must be non-empty" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": " ", "type": "bool", "value": true } ] } ] } }
```

<!-- fixture: rejected "duplicate param key 'a'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "bool", "value": true }, { "key": "a", "type": "bool", "value": false } ] } ] } }
```

<!-- fixture: rejected "type must be one of ['float', 'int', 'bool', 'choice', 'string']" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "text", "value": "x" } ] } ] } }
```

<!-- fixture: rejected "needs a value, the default" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "bool" } ] } ] } }
```

<!-- fixture: rejected "a bool param does not take ['min']" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "bool", "value": true, "min": 0 } ] } ] } }
```

<!-- fixture: rejected "value must be a boolean" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "bool", "value": 1 } ] } ] } }
```

<!-- fixture: rejected "options must be a non-empty list of distinct strings" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "choice", "options": ["x", "x"], "value": "x" } ] } ] } }
```

<!-- fixture: rejected "value must be one of the options" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "choice", "options": ["x"], "value": "y" } ] } ] } }
```

<!-- fixture: rejected "a string param does not take ['options']" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "string", "value": "x", "options": ["x"] } ] } ] } }
```

<!-- fixture: rejected "value must be a string" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "string", "value": 3 } ] } ] } }
```

<!-- fixture: rejected "max_length must be an integer in [1, 4096]" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "string", "value": "", "max_length": 4097 } ] } ] } }
```

The bound counts UTF-8 bytes: `"é"` is two, so it fits a `max_length` of 2
and not of 1.

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "string", "value": "é", "max_length": 2 } ] } ] } }
```

<!-- fixture: rejected "value is longer than max_length (1 UTF-8 bytes)" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "string", "value": "é", "max_length": 1 } ] } ] } }
```

<!-- fixture: rejected "numeric field 'value' must be an integer" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "int", "value": 1.5 } ] } ] } }
```

`Value.i` is `sint64`, so an `int` default or bound must fit it: the edges pass,
one past either does not.

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "int", "value": 9223372036854775807, "min": -9223372036854775808 } ] } ] } }
```

<!-- fixture: rejected "numeric field 'value' must fit a signed 64-bit integer" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "int", "value": 9223372036854775808 } ] } ] } }
```

<!-- fixture: rejected "numeric field 'min' must fit a signed 64-bit integer" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "int", "value": 0, "min": -9223372036854775809 } ] } ] } }
```

<!-- fixture: rejected "needs min <= max" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "float", "value": 1, "min": 2, "max": 1 } ] } ] } }
```

<!-- fixture: rejected "value is outside [min, max]" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "stages": [ { "id": "s", "params": [ { "key": "a", "type": "int", "value": 0, "min": 1 } ] } ] } }
```

Exporters and overlays:

<!-- fixture: rejected "duplicate exporter id 'csv'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "exporters": [ { "id": "csv", "extension": "csv" }, { "id": "csv", "extension": "tsv" } ] } }
```

<!-- fixture: rejected "extension must be non-empty" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "exporters": [ { "id": "csv", "extension": "  " } ] } }
```

<!-- fixture: rejected "extension must be non-empty" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "exporters": [ { "id": "csv", "extension": "." } ] } }
```

<!-- fixture: rejected "duplicate overlay id 'o'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "c", "title": "C" } ], "overlays": [ { "id": "o" }, { "id": "o" } ] } }
```

<!-- fixture: rejected "segment_mib must be an int in [1, 512]" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "c", "title": "C" } ], "overlays": [ { "id": "o", "segment_mib": 1024 } ] } }
```

<!-- fixture: rejected "a chunked overlay's arena is sized by the host from the dataset; segment_mib does not apply" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "c", "title": "C" } ], "overlays": [ { "id": "o", "chunked": true, "segment_mib": 64 } ] } }
```

<!-- fixture: rejected "chunked must be a bool" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "c", "title": "C" } ], "overlays": [ { "id": "o", "chunked": "yes" } ] } }
```

<!-- fixture: rejected "time must be one of ['per_t', 'static']" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "c", "title": "C" } ], "overlays": [ { "id": "o", "chunked": true, "time": "always" } ] } }
```

<!-- fixture: rejected "time applies only to a chunked overlay" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "c", "title": "C" } ], "overlays": [ { "id": "o", "time": "static" } ] } }
```

<!-- fixture: accepted -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "overlays": [ { "id": "labels", "chunked": true, "time": "static" } ],
                     "panels": [ { "id": "p", "title": "Labels", "root": { "kind": "label", "id": "l", "text": "Labels" } } ] } }
```

<!-- fixture: rejected "overlays are drawn only while the plugin runs, and nothing declared here starts it" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "overlays": [ { "id": "labels", "chunked": true, "time": "static" } ],
                     "commands": [ { "id": "c", "title": "C", "menu": null } ] } }
```

Tools and bindings:

<!-- fixture: rejected "duplicate tool id 't'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t" }, { "id": "t" } ] } }
```

<!-- fixture: rejected "views must be a non-empty list from ['2d', '3d']" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "views": ["4d"] } ] } }
```

<!-- fixture: rejected "binding is not a field" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "binding": { "key": "T" } } ] } }
```

<!-- fixture: rejected "mode 'toggle' not in" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "mode": "toggle" } ] } }
```

<!-- fixture: rejected "cancel_on_camera_move must be a boolean" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "cancel_on_camera_move": "no" } ] } }
```

<!-- fixture: rejected "wants_pick must be a boolean" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "wants_pick": "no" } ] } }
```

<!-- fixture: rejected "button must be a string" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "button": 4 } ] } ] } }
```

<!-- fixture: rejected "button 'wheel' not in" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "button": "wheel" } ] } ] } }
```

<!-- fixture: rejected "modifiers 'hyper' not in" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "button": "right", "modifiers": ["hyper"] } ] } ] } }
```

<!-- fixture: rejected "a trigger names a key or a button, not both or neither" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "key": "T", "button": "right" } ] } ] } }
```

<!-- fixture: rejected "a trigger names a key or a button, not both or neither" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "modifiers": ["ctrl"] } ] } ] } }
```

<!-- fixture: rejected "a key trigger carries its modifiers in the key name" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "key": "T", "modifiers": ["ctrl"] } ] } ] } }
```

<!-- fixture: rejected "alt is not a mouse modifier" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "button": "left", "modifiers": ["alt"] } ] } ] } }
```

<!-- fixture: rejected "the bare left button is never a trigger" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "button": "left" } ] } ] } }
```

<!-- fixture: rejected "a mouse trigger is momentary" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "button": "middle", "hold": "toggle" } ] } ] } }
```

<!-- fixture: rejected "hold 'sticky' not in" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "key": "T", "hold": "sticky" } ] } ] } }
```

<!-- fixture: rejected "duplicate trigger" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "key": "T" }, { "key": "t" } ] } ] } }
```

<!-- fixture: rejected "more than 64 actions" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "actions": [ { "id": "a0", "key": "F1" }, { "id": "a1", "key": "F2" }, { "id": "a2", "key": "F3" }, { "id": "a3", "key": "F4" }, { "id": "a4", "key": "F5" }, { "id": "a5", "key": "F6" }, { "id": "a6", "key": "F7" }, { "id": "a7", "key": "F8" }, { "id": "a8", "key": "F9" }, { "id": "a9", "key": "F10" }, { "id": "a10", "key": "F11" }, { "id": "a11", "key": "F12" }, { "id": "a12", "key": "F1" }, { "id": "a13", "key": "F2" }, { "id": "a14", "key": "F3" }, { "id": "a15", "key": "F4" }, { "id": "a16", "key": "F5" }, { "id": "a17", "key": "F6" }, { "id": "a18", "key": "F7" }, { "id": "a19", "key": "F8" }, { "id": "a20", "key": "F9" }, { "id": "a21", "key": "F10" }, { "id": "a22", "key": "F11" }, { "id": "a23", "key": "F12" }, { "id": "a24", "key": "F1" }, { "id": "a25", "key": "F2" }, { "id": "a26", "key": "F3" }, { "id": "a27", "key": "F4" }, { "id": "a28", "key": "F5" }, { "id": "a29", "key": "F6" }, { "id": "a30", "key": "F7" }, { "id": "a31", "key": "F8" }, { "id": "a32", "key": "F9" }, { "id": "a33", "key": "F10" }, { "id": "a34", "key": "F11" }, { "id": "a35", "key": "F12" }, { "id": "a36", "key": "F1" }, { "id": "a37", "key": "F2" }, { "id": "a38", "key": "F3" }, { "id": "a39", "key": "F4" }, { "id": "a40", "key": "F5" }, { "id": "a41", "key": "F6" }, { "id": "a42", "key": "F7" }, { "id": "a43", "key": "F8" }, { "id": "a44", "key": "F9" }, { "id": "a45", "key": "F10" }, { "id": "a46", "key": "F11" }, { "id": "a47", "key": "F12" }, { "id": "a48", "key": "F1" }, { "id": "a49", "key": "F2" }, { "id": "a50", "key": "F3" }, { "id": "a51", "key": "F4" }, { "id": "a52", "key": "F5" }, { "id": "a53", "key": "F6" }, { "id": "a54", "key": "F7" }, { "id": "a55", "key": "F8" }, { "id": "a56", "key": "F9" }, { "id": "a57", "key": "F10" }, { "id": "a58", "key": "F11" }, { "id": "a59", "key": "F12" }, { "id": "a60", "key": "F1" }, { "id": "a61", "key": "F2" }, { "id": "a62", "key": "F3" }, { "id": "a63", "key": "F4" }, { "id": "a64", "key": "F5" } ] } ] } }
```

<!-- fixture: rejected "pane.icon must be non-empty" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "pane": { "icon": " " } } ] } }
```

<!-- fixture: rejected "duplicate action id 'a'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "actions": [ { "id": "a", "key": "A" }, { "id": "a", "key": "B" } ] } ] } }
```

<!-- fixture: rejected "an action needs a key or a pane button to be reachable" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "actions": [ { "id": "a" } ] } ] } }
```

<!-- fixture: rejected "key 'N' is also one of this tool's triggers" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "triggers": [ { "key": "N" } ], "actions": [ { "id": "a", "key": "N" } ] } ] } }
```

<!-- fixture: rejected "key 'N' is also another action's of this tool" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "actions": [ { "id": "a", "key": "N" }, { "id": "b", "key": "N" } ] } ] } }
```

<!-- fixture: rejected "pane.icon 'missing.svg' not found in the plugin directory" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "pane": { "icon": "missing.svg" } } ] } }
```

<!-- fixture: rejected "pane.icon 'icons/tool.gif' is not one of" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "pane": { "icon": "icons/tool.gif" } } ] } }
```

<!-- fixture: rejected "pane.icon '../tool.svg' must be a path inside the plugin directory" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "pane": { "icon": "../tool.svg" } } ] } }
```

<!-- fixture: rejected "pane.icon 'icons/big.png' is 65537 bytes; the limit is 65536" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "pane": { "icon": "icons/big.png" } } ] } }
```

<!-- fixture: rejected "pane.icon 'icons/fake.png' is not a PNG file" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "pane": { "icon": "icons/fake.png" } } ] } }
```

<!-- fixture: rejected "pane.icon 'icons/fake.svg' is not an SVG file" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "pane": { "icon": "icons/fake.svg" } } ] } }
```

<!-- fixture: rejected "pane.tooltip longer than 128 characters" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "tools": [ { "id": "t", "pane": { "icon": "icons/tool.svg", "tooltip": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" } } ] } }
```

<!-- fixture: rejected "pane.hold 'toggle': an action or command button fires once on click" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "contributions": { "commands": [ { "id": "c", "title": "C", "pane": { "icon": "icons/go.png", "hold": "toggle" } } ] } }
```

Assets:

<!-- fixture: rejected "id must start with an alphanumeric" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "assets": [ { "id": "-weights", "filename": "w.pt", "size": 1, "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "source": { "kind": "manual" } } ] }
```

<!-- fixture: rejected "duplicate asset id 'w'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "assets": [
    { "id": "w", "filename": "w.pt", "size": 1, "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "source": { "kind": "manual" } },
    { "id": "w", "filename": "v.pt", "size": 1, "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "source": { "kind": "manual" } } ] }
```

<!-- fixture: rejected "filename must be one plain file name" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "assets": [ { "id": "w", "filename": "../w.pt", "size": 1, "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "source": { "kind": "manual" } } ] }
```

<!-- fixture: rejected "size must be a positive integer" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "assets": [ { "id": "w", "filename": "w.pt", "size": 0, "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "source": { "kind": "manual" } } ] }
```

<!-- fixture: rejected "sha256 must be exactly 64 hexadecimal characters" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "assets": [ { "id": "w", "filename": "w.pt", "size": 1, "sha256": "abc123", "source": { "kind": "manual" } } ] }
```

<!-- fixture: rejected "source.kind must be 'manual' or 'https'" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "assets": [ { "id": "w", "filename": "w.pt", "size": 1, "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "source": { "kind": "ftp", "url": "ftp://x/w" } } ] }
```

<!-- fixture: rejected "HTTPS source needs a string url" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "assets": [ { "id": "w", "filename": "w.pt", "size": 1, "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "source": { "kind": "https" } } ] }
```

<!-- fixture: rejected "source.url must be an HTTPS URL without credentials" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "assets": [ { "id": "w", "filename": "w.pt", "size": 1, "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "source": { "kind": "https", "url": "https://user:pw@models.example.org/w.pt" } } ] }
```

<!-- fixture: rejected "manual source must not declare a url" -->
```json
{ "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["python3"] },
  "assets": [ { "id": "w", "filename": "w.pt", "size": 1, "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "source": { "kind": "manual", "url": "https://x/w" } } ] }
```
