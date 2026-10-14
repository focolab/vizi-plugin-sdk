# `vizi_hostsim` — a host emulator for plugins

A Python package that plays vizi's host to a plugin over the **real
transport**, so a plugin can be exercised end to end without vizi installed.
It launches the plugin's `entry.command` in its own directory as the Broker
would, connects the gRPC session over a Unix domain socket, hands the plugin
an inherited `SOCK_SEQPACKET` doorbell, grants segments over `SCM_RIGHTS`,
and then does what the `1.0` host does. Each example's `hostsim.py` imports
it and adds its dataset and its scenarios.

It is an emulator, not a host: it implements what the examples need and is
permissive about ordering. Unix only. The Windows transport (TCP doorbell,
port file, session token) has no emulator here.

## Running a plugin against it

Every example under `examples/` has a driver, `python/hostsim.py`, that
launches the plugin, plays host for a list of scenarios and prints one `ok` or
`FAIL` line per scenario:

```bash
cd examples/peak_roi/python
.venv/bin/python hostsim.py                       # this implementation
.venv/bin/python hostsim.py --plugin-dir ../rust  # any other implementation
```

`--plugin-dir` names a directory holding a `vizi-plugin.json`. The driver
reads that manifest and launches its command, so the same scenarios run
against any implementation of the example's `SPEC.md`.

Environment variables the emulator reads:

| Variable | Effect |
|---|---|
| `VIZI_HOSTSIM_PYTHON` | the interpreter substituted for a `.venv` interpreter the manifest names that does not exist (else this process's interpreter). An emulator convenience; see *Launcher* below |
| `VIZI_HOSTSIM_AUTHORITY` | overrides the gRPC `:authority` (default `localhost`, the host's value); empty restores grpcio's own derivation from the socket path |
| `VIZI_HOSTSIM_SUPERSEDE_UNDRAINED` | `1` makes a grown `V` ring supersede an undrained one; see *How `V` and `X` are granted* |
| `VIZI_PLUGIN_INIT_TIMEOUT_SECS` | overrides the launch budget, as it does on the host |

## Writing a driver for your own plugin

A driver is a script that builds a `Dataset`, adds the segments its scenarios
need, and drives a `Host` inside a `with` block:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # examples/
from vizi_hostsim import Host, Dataset, Segment, Voxel, check, failures, segment_id

host = Host(plugin_dir, DATASET)                       # reads vizi-plugin.json
host.add(Segment.host_ring(segment_id(1), "V", 8 << 20, host.directory))
host.add(Segment.overlay(segment_id(2), "markers", 1 << 20, host.directory))
host.volume_segment = segment_id(1)
with host:                                             # launches, handshakes
    host.check_handshake()
    host.grant(segment_id(1)); host.grant(segment_id(2))
    tool = host.arrange("probe")                       # binding, mapping, epochs
    g = host.press(tool, VIEW_2D, Voxel(7, 10, 12)); g.drag(...); g.release(...)
    host.await_live(1)          # consumed lane values, with `applied` and `pane`
    host.await_pushes(1)        # control-plane pushes, loans read and released
    host.invoke("cmd", value="x"); host.open_panel("p"); host.shutdown()
```

`check(label, fn)` runs one scenario: `fn` returns a detail string or raises,
and a failure is appended to `failures`, which the driver turns into its exit
status. What the plugin sent is kept on the `Host` for a scenario to assert
on (`host.patches`, `host.violations`, `host.refusals`, `host.diagnostics`,
`host.unhandled` and the others named below); every driver ends by asserting
`not host.unhandled`.

The existing drivers are the worked examples:

- `examples/peak_roi/python/hostsim.py`: the human-rate path (`V` deposits, a
  panel, ROI contribution, both grant orders, the doorbell window, teardown).
- `examples/voxel_probe/python/hostsim.py`: the fast path (two `O` segments, a
  `P`, arrangements, gestures, the lane, cancel, the stale-pane rule, a PNG
  loan).
- `examples/format_bridge/python/hostsim.py`: a data source (a reader through
  `G` in the contract's order, an exporter through `X` with the `M` half late,
  source-indexed chunks, the replacing open, a too-small arena, both
  calibration paths, the self-serve guard's reply, two sessions).
- `examples/movie_seg/python/hostsim.py`: a chunked overlay (two host-sized
  arenas, the want set served in order, eviction, a static overlay, the
  tables, a dataset change with both arenas re-granted superseding the old,
  and the deferred publish watched with the host's releases held).
- `examples/stage_chain/python/hostsim.py`: a processing-stage chain
  (`chain.py`).
- `examples/tetra_shape/`: a region type the plugin shapes.

## Its encoders and readers are its own

`check_encoder()` runs before any scenario and fails the run if the `M`
encoder disagrees with the cross-check corpus that the Python, Go, Rust and
Java SDKs all grade (`polyglot/CROSSCHECK.md`). The `E` and `D` layouts
and the lane reader are written from the documents. **Importing the SDK's
encoder or `PulsedReader` here would make the thing under test grade itself**,
which proves self-consistency and nothing else. What is imported from the SDK
is the protobuf stubs and the protocol version — the contract, not an
implementation of it. `atomicshm` is the same library the SDK uses for its
atomics, and that is not the SDK: it supplies `load`/`CAS`/`store` on a
mapping and knows nothing about this protocol.

## What it covers

Section numbers without a document name are `control_plane_protocol.md`'s.

### Session and transport

| | |
|---|---|
| Launch, handshake | the manifest's command in the plugin directory (`manifest_schema.md` §2.2); `Hello` with an identity string; the plugin's `Ready` checked for **form** and nothing else (§3.1). The gRPC channel is a grpcio `unix://` channel sending `:authority = localhost`, the host's value on both transports (§2.1) |
| Launcher | `entry.command` runs with the plugin directory as its working directory, with the environment the `1.0` host gives it: `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, the three `DYLD_*` and `PYTHONPATH`/`PYTHONHOME` removed (`SCRUBBED_ENV_KEYS`), so a plugin that only works because it inherited one fails here too. **One deliberate difference from the host:** a `.venv` interpreter the manifest names that does not exist is replaced by `VIZI_HOSTSIM_PYTHON`, else this process's interpreter, so every `hostsim.py` can run from one synced environment. The host has no such fallback; a missing `.venv` fails to launch in vizi |
| A refused manifest | a manifest the reference parser refuses is refused before launch, as the host refuses it at scan: for example overlays nothing can start, or a tool with `view_targets` and no panel with them |
| Deadlines | the host's: one launch budget from spawn to `Ready`, over the bind and `Ready` (`launch_budget`: 60 s, or the manifest's `entry.startup_s` held to 10–300 s; `Host(launch_timeout=)` overrides it, as does `$VIZI_PLUGIN_INIT_TIMEOUT_SECS`). A command waits 10 s for `CommandDone` only when UNSUPERVISED (`COMMAND_WINDOW_S`, `host.invoke`'s default, §8.3); vizi shows every command to the person, so it supervises them and waits with no deadline (§8.16) — `invoke_async(...)` with your own `result(timeout=)` is that wait |
| Message size | the channel decodes up to §9's 32 MiB |
| Per-field bounds | §9's bounds on every plugin message, as the host applies them: 65 536 per repeated field or map, 1 048 576 for `TimeMapping.t` and the `Roi` and `RoiPoseExtension` pose arrays, 64 for `Geometry.levels`, 8 MiB per bytes/string field (the table is the SDK's `control._FIELD_BOUNDS`, imported rather than copied). A request over one is answered `Error{PROTOCOL}`, a push reported on `repeated_field_overflow` or its own refusal site (`host.over_bound`) |
| Unknown kinds | a plugin message of a kind the emulator does not know is answered `Error{UNSUPPORTED}` when its id is a request, delivered to the waiter when it names one, noted when it is a push (`host.unknown_kinds`); `host.request_unknown_kind()` sends the plugin one and returns its answer (§11) |
| A handler that raises | never kills the reader thread: a request is answered `Error{INTERNAL}` and the exception lands in `host.unhandled`, so the driver still fails loudly |
| Contract violations | every plugin→host `ContractViolation` is kept in `host.violations`; `await_violations("unusable_context")` waits for a site by name |
| `RequestStatusQuery` | answered from a request ledger: `PENDING` for ANY plugin request taken in and not yet answered — a read parked by `hold_reads` as much as a held picker — with `awaiting_user` only for the picker; `UNKNOWN` otherwise |
| Teardown | `DoorbellShutdown` with its 2 s window and the `DoorbellFinalReport`; `Shutdown{reason, deadline_ms}`, the `ShutdownReport`, and the stream close inside the `deadline_ms` window, 5 s by default with **no margin** — the host kills a plugin that overruns it (§3.3, §8.13), and a plugin that overruns at exit is an `overran:` line in `host.diagnostics` and on stderr. Once teardown has begun, a data-access request is dropped unanswered (`host.dropped_in_shutdown`) |

### Segments and grants

| | |
|---|---|
| Grants, two halves | `M` on the doorbell with the descriptor, `InstallSegment` on the control plane, in either order, and the `GrantAck` carrying the proof read back out of the mapping (`doorbell_M_memory.md`, §8.12); `retire_segment` sends `RetireSegment` and waits for the plugin's `SegmentReleased` (`shared_memory_protocol.md` §9). On Linux the object is a memfd sealed shrink+grow as `doorbell_M_memory.md` §1 says the host's is (through libc when the interpreter lacks `os.memfd_create`) |
| Region tables per purpose | as the `1.0` host lays them out: `V`/`X` one host-produced `LOANED` ring; `G`/`P` one plugin-produced `LOANED` arena; `O` a plugin-published `PULSED` lane (3 buffers, pane header in each slot's last 8 bytes) then a plugin-produced arena; `subject` = the overlay or panel id |
| Grant acknowledgements | the **first** terminal answer stands (a later contradicting one is a `host.diagnostics` line, never a change); an `installed` whose identity proof is not the one written is recorded as `failed` (§8.12); an ack for a segment never granted is ignored |
| Loans | every `SegmentRef` a control-plane `OverlayUpdate` names and every `ImageRef` a `PanelPatch` names is read out of the plugin's arena, bounds-checked against the region, and released with `ReleaseLoan` once its declared `uses` are disposed of: `0`/`1` mean one, a push and a compile naming one reservation with `uses = 2` release it once, after the second, and a later message declaring a different count is `uses_disagreement` in `host.diagnostics` (§5.6). A descriptor into a segment that is not granted refuses the push (`overlay_item_refused`) rather than raising |
| A retiring segment | a push naming a segment the host retired and the plugin has not released is dropped quietly, its loans returned (`host.retiring_segments`); a panel patch loses only its image ops naming it, before anything else, label tables included. A `ReleaseLoan` crossing the plugin's own `SegmentReleased` is counted, not reported (§9.1) |
| `ReleaseRequest` | a hint: recorded in `host.release_requests` for a segment the session holds, never acted on |

### The doorbell

| | |
|---|---|
| Input | `E` records composed field by field from `doorbell_E_input.md` §1 and §3: `Voxel`, `Column`, `Ray`, `RayAndPicked`, `MISS`; interactions and `seq` allocated as the host does; `CANCEL` carrying `NONE` |
| The plugin's hints | a reader thread decodes the plugin's `D` records (`doorbell_D_notification.md` §1) and brings the lane scan forward |
| Pulsed lanes | read per `shared_memory_protocol.md` §6.5 through `atomicshm` on the host's own mapping — claim, validate the published length against the slot's capacity, copy from slot byte 0, release by exact CAS, consume — then §3.3's two rules and §3.2's pane rule as the receiver: a value stamped with a pair that is not the pane's current epoch is consumed and counted, not applied |
| The plugin's doorbell records | framing checked in `doorbell_protocol.md` §8's order on a buffer one byte longer than a record, with room for two descriptors, so an oversize or short datagram, a descriptor on a `D`, more than one descriptor, `MSG_CTRUNC`, a bad CRC or header, `0xFF`, and an `M` or `E` sent the wrong way are each a **terminal** fault — recorded in `host.doorbell_faults` and `host.unhandled`, and the doorbell stops, as the host tears the session down. The `DoorbellShutdown` names the class the host would (`BAD_CRC`, then `BAD_VER`, `BAD_FLAGS`, `BAD_RESERVED`, in that order). An empty datagram on a macOS `DGRAM` doorbell is `bad_length`, not a hang-up; on Linux `SEQPACKET` it is EOF. A 0-byte read carrying a descriptor or `MSG_CTRUNC` is a frame (ctrunc, then wrong_fd_count, then bad_length), not a hang-up; `host.send_raw_doorbell(data, fds)` sends the plugin one. An op this version does not define is dropped and counted in `host.skipped_ops` (§5), never a fault. A `D` naming segment **0** is dropped and answered `ContractViolation{zero_id}` (`host.refusals`); one naming a segment that is not granted — one retired a moment ago — is counted in `host.stray_hints` and the session goes on (D §4) |
| A doorbell's end | **a doorbell that closes ends its session, politely**: on a framing fault the emulator sees, the plugin's own `DoorbellFault`, or a hang-up it did not start, it sends `DoorbellShutdown`, waits out the 2 s window (the `DoorbellFinalReport` closes it early), releases its end and sends `Shutdown{session_fault}` naming the cause (`host.doorbell_end`, `host.doorbell_end_reason`; every notice sent is in `host.doorbell_shutdowns`). **A plain hang-up opens the window as `peer_closed`** (`doorbell_protocol.md` §10; no `failure_class`), and a `DoorbellFault` arriving inside that window is answered `peer_fault`: the ending is upgraded to the plugin's report, and the `Shutdown` detail names it (no second `DoorbellShutdown` is sent). A plugin must tolerate a `DoorbellShutdownReason` it does not know. `host.end_session_for_doorbell(detail, reason=, failure_class=)` drives the same path from a scenario; `doorbell_shutdown()` sends only the notice. The plugin must not end the session itself |

### Arrangements and tools

| | |
|---|---|
| Arrangements | `ToolBinding` → `TimeMapping` → one `EpochChange` per pane, each acknowledged before the next (§5.5, §8.10, §8.11). A dataset with no time increment (`Dataset.time_increment_s = 0`) gets **no** `TimeMapping` and its epochs name revision 0, as the host's do (`arrange()` skips `arrange_time`, which refuses to run for it). Epochs come from a per-pane counter that belongs to the PROCESS: 1 first, never restarting, so a second `Host` in one process continues the count |
| Subscriptions | `subscriptions`, `epochs_arranged`, `manifest_warnings` (`manifest_schema.md` §5.1). `arrange_pane` REFUSES for a session that declares neither a tool nor `"epochs"`. An `EpochChange` naming a time mapping that was never arranged is not acknowledged — call `arrange_time()` first. `"view"` and `"regions"` each need `data_access`, else they are warned in `manifest_warnings` and not arranged; an unknown name is warned and ignored |
| Tools, modal | `bind_tool(tool_id, triggers=, actions=)` sends the `ToolBinding` — the assigned `Trigger`s (a key name, or `(button, mods)`) and every declared action 1-based — and `host.fire(..., action=n)` writes `E.action` at `[42..44)`. A plugin's own `ActivateTool` (§8.10) is kept in `host.activations` and answered as the `1.0` host answers it: **the arrangement first, then the reply** — activating a tool that is not engaged retires the engaged one's binding (displacement) and binds this one under a NEW handle from the session counter (with the manifest's mode and actions, no triggers); deactivating the engaged tool retires its binding; either answered `ToolActivationChanged`. Refused `INVALID_ARGUMENT` for a tool the manifest does not declare and for any change while a gesture is open (`host.activation_refusals`). `bind_tool` engages the same way — binding a second tool, or the same one again, retires the live binding first and waits for both acks — and `host.engaged` names the engaged tool |

### Panels

| | |
|---|---|
| Opening a panel | `open_panel(panel_id, grant=seg)` is vizi's order: `P` granted, both halves, and `PanelOpened` sent in the same call **without waiting for the `GrantAck`**, so a plugin can see `PanelOpened` before `P` is installed. Without `grant=` a driver grants `P` itself (and usually waits for the ack), an order vizi never produces. `reprovision_panel(panel_id, seg, visible=)` is vizi's crash relaunch: on a fresh `Host`, `P` is granted for a panel that stays on screen and the panel is live from the grant; `PanelOpened` follows when the dock is showing and not when it is hidden |
| Panel patches | applied op by op, and every op NOT applied reported to the plugin as `ContractViolation{panel_patch_refused}` (`host.refusals`): an op naming a widget the panel does not declare; an op the widget's kind does not take (`set_value` only on slider (a number), checkbox, dropdown (an option it holds or an index in range), line_edit, label, button; `set_progress` only on progress; `set_options` only on dropdown; `set_image` only on image); a `set_image` not into this panel's `P` segment or not a PNG. A refused op is not in `host.patches`; its image loan is released either way. At exit a run prints how many refusals of each site it sent |
| A panel that is not open | **a declared panel that is not open KEEPS its state**: the latest op per (widget, property, row), applied when `open_panel` runs, `set_rows` first, then `set_options`; an undeclared widget is refused, and an image is refused ("the panel is not open, and its image segment is granted when it opens"; the SDK holds an image until `PanelOpened`, §8.4). Held patches are bounded: `set_rows` past `max_rows` is refused, and cells are held only for rows the list will have. A closed dock is hidden, not unopened: `close_panel` keeps applying patches, and `open_panel` again is an open |
| The `list` widget | its `row` template's widgets are cells in the panel's id space; `set_rows` is the whole ordered key list, refused whole past `max_rows` (default 256), and for an empty, repeated or longer-than-128 key; a cell op needs a `row_key` naming a row the list holds; a `row_key` on a widget that is not a cell is refused; a selectable list's `set_value` takes a held key or `""`. `list_rows`, `row_patches` (panel, widget, row, value), `row_images` and `list_selection` keep the state for a driver; `invoke(cmd, row=key, value=...)` sends a row's event |
| `progress` | `await_progress(panel, widget, until=)`, `progress`, `progress_log`. The panel lane COALESCES: the fixture sends four values and one arrives, and the command's reply can overtake it. Assert the state, never the count. **Pass `until=` if your SDK has a widget mirror**: the mirror re-pushes its declared opening value when the panel opens, so a bare wait answers *that*, not the last value sent. A NaN value shows empty, a value is clamped to [0, 1], and any value is ignored while indeterminate |
| Pickers | the four picker kinds parse (`manifest_schema.md` §3.3.1), and their `set_value` is checked against the emulator's own choices: a multiple `channel_picker` takes a list (`Value.ints`), a single one an index the dataset has, a `colormap_picker` one of `host.colormaps`, a `roi_picker` a region the host has; a colour must be `#RRGGBB` (vizi's swatch also takes other spellings). `Value.ints` goes both ways (`invoke(value=[0, 2])`). For a `roi_picker` with `follows_selection`, `invoke_widget(panel, picker, value=id)` is the person's selection (`select_region_as_person`), and every region choice sends `name`. The emulator renders no `channel_picker` labels |
| `GetColormap` | `colormaps` holds vizi's curated names, `gray` exact and the rest the emulator's own ramps; unknown `NOT_FOUND` |

### Host data access

| | |
|---|---|
| `GetViewState` | answered from the driver's `Dataset`: `visible_channels` (§8.7; every channel by default), `time_increment`/`time_unit`, `selected_region_id`, `channels`, `dataset_series`, the applied filter, and `file_state_dir`. `Dataset.spacing_zyx` / `spacing_unit` flow into it: with no spacing set, the unit is `""` and the provenance is `DISPLAY_FALLBACK` — "we do not know how big a voxel is", which is not a measurement. A partly recorded pitch, e.g. `(None, 0.3208, 0.3208)`, reaches `ViewState` as NaN on the unrecorded axis with the unit and `FILE`, and `Geometry` as the finite display fallback |
| `ReadDataVolume` | answered from the `Dataset`, deposits into the `V` ring at advancing offsets, or refused `UNSUPPORTED` while `host.self_serve_guard` is set — the reply a dataset served by this plugin's own reader gets (`reader_writer_contract.md` §1.9; `GetViewState` is not guarded). A one-sided or short ROI is `INVALID_ARGUMENT`; a `t` or `c` past the dataset is `OUT_OF_BOUNDS`. Every read is kept whole in `host.read_requests` (`ReadRequest`: `t`, `c`, `level`, `roi`, `raw`) beside the `host.reads` triples |
| The applied filter | `Dataset.applied_filter` is served in `DataVolume.filter`, `ViewState.filter` and `ViewChanged.filter`. With it set, a read is the shared filter's output (`Dataset.filter_fn(volume, t, c)`, else the source under its name) and carries `DataVolume.filter`; a `raw` read is the source with no `filter` |
| A pyramid | `Dataset.levels`: level `n` is level 0 block-mean downsampled by `2**n` per axis with an odd trailing sample discarded, so `Geometry.levels` carries the SAMPLING FACTOR and `ViewState.levels` the shapes `max(1, floor(n / s))` (§8.11); a read at level `n` with an ROI in that level's coordinates is served from it, a level or an ROI past the shape is `OUT_OF_BOUNDS` |
| Channel names | `Dataset.channel_names` are the FILE's names (`""` or missing = unnamed); `Dataset.channel_infos()` gives every channel as vizi does, the file's name with `FILE` or `"Channel N"` (1-based) with `PLACEHOLDER`, and it feeds `Geometry.channels`, `ViewState.channels` and `OpenExport.channels` |
| Subscribed pushes | after `Ready`, a 40 ms tick restates each subscribed state and sends it only when it changed. `ViewChanged` carries `open_generation`, `t`, `z`, `visible_channels`, `selected_region_id`; `RegionsChanged` is `reset` on the first push and on a new `open_generation` (every region listed), then one `RegionChange` per changed region, a deleted one with revision 0 and origin `""`. Every push sent is in `view_pushes` / `region_pushes`. `close_dataset()` / `open_dataset()` give a new generation; the close's `ViewChanged` is generation 0 and nothing else |
| Regions | `GetRois` and `ContributeRois` against a host-side annotation store (§8.8: `contrib_id` stable per plugin namespace, `replace_all`). `RoiRecord.revision` comes from one counter, new whenever a different record sits under an id or an in-place edit retires it, never reused; `content_hash` is 16 hex over the `Roi` (the emulator's digest, not vizi's: a plugin compares, never computes). `GetRois.region_ids` filters, a missing id absent. Driver helpers: `add_region(roi, name, origin="user")`, `delete_region(id)`, `select_region(id)`, `edit_region(id, name=, color=, visible=)` (bumps the revision and leaves `content_hash`). `ContributeRois` refuses `INVALID_ARGUMENT` a NaN or infinite vertex position or direction, pose row or frame basis, and a pose domain outside `[0, T)`, the batch checked whole before the first write |
| The `GetRois` pose window | `pose_domains`, `max_message_bytes`. **The host's shapes**: without a window each record is posed at the frame on screen — no pose rows and `t_start`/`t_end` ABSENT, a `HIDE` region absent at that frame not listed at all. With one, the rest form with rows and `t_start`/`t_end` clipped to it; a window that misses the domain returns the shape timeless (rows and `t_start`/`t_end` cleared); half a window is open at the missing end; an inverted window is `INVALID_ARGUMENT`. The domain pair is on EVERY record of a region with a domain — one declared over `[t_start, t_end]`, rows or not — and a slice is told by comparing it with `roi.t_start`/`t_end`. An oversized reply is refused `OUT_OF_BOUNDS`, never clipped — lower `max_message_bytes` to drive that without 32 MiB of poses |
| `ExtendRoiPoses` | `pose_extensions`. A batch is refused `INVALID_ARGUMENT` when empty, when it names one region twice, when an extension carries no component or a wrong-length one, when its component set differs from the region's, for a frame outside `[0, T)` of the open dataset or a NaN or infinite value in any row; `NOT_FOUND` for a region not contributed; overlap replaces, a gap is refused, and an extension grows the region's span as well as its domain; more than 1 048 576 doubles in one component of one extension (174 762 stretch frames) is §9's bound, `Error{PROTOCOL}`. `frame_count` is the *rows written* |
| `ReadRoiInclusion` | `inclusion_requests`. A `t` outside the DATASET is `OUT_OF_BOUNDS`; past the region's pose domain it is **held** at its end row — `HOLD` is the wire default — and answered present with a deposit, and only a `HIDE` region answers `present=false` with NO segment, which owes no release; a region clipped out of the dataset is present with no segment and `voxel_count = 0`. A present region deposits into `V` and is released like any other. **The clip box** (`roi_min_zyx`/`roi_max_zyx`): level 0, min inclusive, max exclusive, `ReadDataVolume`'s semantics — the answer is the region's voxels inside it (none: present, `voxel_count = 0`), an empty box or one leaving the dataset `OUT_OF_BOUNDS`, other than 3 values at either end `INVALID_ARGUMENT`, and a region whose clipped box is past `host.inclusion_voxel_budget` (`INCLUSION_VOXEL_BUDGET`, 256 Mi voxels; the spec names no number) `OUT_OF_BOUNDS` |
| `CompileLabelVolume`, `ReadMaskVoxels` | a compile is validated as a voxel selection with segment-backed labels and LUT, one mask per nonzero label, coloured by the `1.0` LUT rule (row `1 + (id - 1) mod 255`, never row 0) and named from `label_names` keyed by label id (§8.8), both loans disposed of; `ReadMaskVoxels` is a u8 deposit into `V` with the origin, preceded by the standalone `InstallSegment` when its ring is still in `host.ride_along` (§8.12) |
| Pickers: `RequestAsset`, `RequestSavePath` | `RequestAsset` from a slot table (picked, or declined) with the import-time size and digest (§8.9). `RequestSavePath` is scripted by `save_path_answer` (`None` declines), the extension appended as vizi appends it, a separator in `suggested_name` `INVALID_ARGUMENT`; requests in `save_requests`. **One picker per session at a time**: a second request while a held one is up is `OUT_OF_BOUNDS` and opens nothing (`host.picker_refusals`). `hold_picker()`, `await_picker()`, `answer_picker(abandoned=)` and `status_queries` drive the lifecycle: the host keeps serving while its dialog is up, which is the only reason the plugin's `RequestStatusQuery` can get through. A command MAY wait on a picker (§8.9); a supervised command has no deadline to hold, so the held deadline matters only to an unsupervised host |
| Permissions | every request is gated on the manifest's `data_access` / `contribute_rois` as the host gates it (§8.7) |
| The plugin's folders | `VIZI_PLUGIN_SETTINGS_DIR` NAMES a settings folder the emulator does **not** create (`host.settings_dir`, under its temp dir rather than beside the manifest, its parent existing; `None` sends the variable absent): the SDK creates it on first use, so an accessor that refuses a missing folder fails here as it fails on the host. `file_state_dir` rides `ViewState` (`host.file_state_dir`), `OpenReader` (`host.file_state_for(uri)`, whose `plugins/<id>` applies §2.3's folder-name rule via `plugin_folder_name(id)` — an id with an upper-case letter is derived, `Org.Example` -> `Org.Example-353f0972` — `""` for a URI with a scheme, or `open_reader(..., file_state_dir=)`) and `OpenExport` (the SOURCE's, overridable the same way). The per-file folders are NAMED and never created, so a scenario can assert the plugin did not create one on open; set `host.file_state_dir = ""` for a dataset with no local path |
| Proving a busy guard | `hold_reads()` parks every `ReadDataVolume` (recorded on arrival, not answered); `await_reads(n)` returns once `n` are parked, so the plugin's operation is PROVABLY in flight; the driver fires its second request and asserts the refusal, which arrives because the reader thread was never blocked; `release_reads()` answers the parked reads from the driver's thread. `peak_roi`, `interactive_seg` and `ray_probe` prove their busy guards this way. `read_delay` exists for a merely slow host, but it sleeps ON the gRPC reader thread, so everything the plugin sends meanwhile waits behind it — a race, not a proof |

### Readers and exporters

| | |
|---|---|
| Readers | `OpenReader`, `ReadVolume`, `ReadPlane`, `Metadata`, `CloseReader` as host requests; a reply's `SegmentRef` into the `G` arena is bounds-checked, read as a loan and released (§8.2, §5.6). The order is `reader_writer_contract.md` §1.1's: open first, then `Segment.reader_arena(seg, opened, dir)` sizes `G` from the reply and `grant` waits for the installed ack before the first read. Grant a `G` after every `ReaderOpened`, as vizi does: the SDK holds a reader's requests until it is installed. A **replacement is §1.2's handshake**: the next `OpenReader` goes while the previous reader is open (H1), the plugin opens-then-closes and answers a NEW handle (H2), the driver grants the new `G` through `host.supersede(old_g, Segment.reader_arena(...))` — `supersedes` naming the old, its `SegmentReleased` awaited (H3–H5) — and sends the old reader no `CloseReader` and no `RetireSegment`; a deliberate close is `close_reader` then `retire_segment` (§1.8); a request naming a replaced handle is the plugin's `NOT_FOUND`, which the drivers check. **A reply is checked as the host checks it**: the segment must be that reader's `G` (the one granted after its open), the dtype the one `ReaderOpened` announced, a level-0 shape the announced one (`(1, Y, X)` or `(Y, X)` for a plane), the length shape × dtype — a reply that fails is refused `PluginError(PROTOCOL)` naming the rule, noted in `host.diagnostics`, and its loan released either way |
| Series | `open_reader(uri, reader_id, series=...)` sends `OpenReader.series` only when given (presence is the signal; `""` is an id). A `SeriesList` answer raises `SeriesChoiceRequired` (`.entries`, `.truncated`, `.choose(id)`, which repeats the open naming it on the same session); every listing is in `host.series_lists`. A listing changed nothing, except that a listing of the URI the open reader holds closed that reader, so the emulator drops that handle from `host.readers`. A list answered to a NAMED series is refused `PROTOCOL` and recorded in `diagnostics`. On receipt the host refuses, with the open failing and no choice offered, a malformed `SeriesList` (no entries, a shape present but not five positive sizes, one id twice) and a `ReaderOpened` with more `channel_names` than C: `series_list_problem` / `reader_opened_problem`, recorded in `diagnostics`. `Dataset.dataset_series` rides `ViewState`; it is `None` by default and then ABSENT (the field is `optional`; `""` is an id) |
| Exporters | `OpenExport` (with the calibration the host knows — `open_export(spacing_zyx=, time_increment=)`, unset = unknown, §8.3), `ExportChunk` (a deposit into the `X` ring, or `inline`), `FinishExport`, `AbortExport`; the plugin's `ReleaseDeposit` honoured (§8.6). `X` is granted standalone BEFORE `OpenExport` and the host waits for no ack (`reader_writer_contract.md` §2.1); `host.ride_along.add(X)` makes the install ride the first chunk instead, the other §8.12 shape. `open_export(..., source_metadata=(form, text), channels=None \| source indices \| ChannelInfos, provenance=None \| dict \| str)`: `channels` defaults to the dataset's first C; `provenance=None` builds a record in the shape of vizi's (`""` sends none). A chunk answered with an `Error` ends the export, and the host then sends `AbortExport` (`host.export_aborts`) |

### Chunked overlays

`control_plane_protocol.md` §5.7 as the host.

| | |
|---|---|
| The arena | `Segment.chunk_arena(seg, overlay, dataset, dir)` grants a chunked overlay (`manifest_schema.md` §3.5 `chunked`, read from the manifest with its `time`; `segment_mib` beside it refused at construction) the ordinary overlay table with the arena **host-sized from the dataset** — one level-0 u16 volume plus one at §8.11's level-1 shape, each padded to 64 (`chunk_arena_bytes`; `arena_bytes=` overrides). `host.supersede(old, replacement)` re-grants it at a new id naming the old in `InstallSegment.supersedes`, waiting for the ack and the old segment's `SegmentReleased` (`shared_memory_protocol.md` §9.5); a plugin that retires it silently gets an explicit `RetireSegment` and a `supersede:` line in `host.diagnostics`, so a driver fails the scenario with a reason rather than a timeout |
| The want set | `host.want_chunks(overlay, [(level, t)])` restates the WHOLE set (`t = None` for a static overlay's want; `[]` for nothing; at most 8), evicting held chunks whose key left (`host.evicted`), one send per change and never two closer than the host's 40 ms pump tick (`want_tick`) |
| A received `OverlayChunk` | **refused** on site 30 (`host.refuse` → a host→plugin `ContractViolation`, kept in `host.refusals`) when the overlay is not chunked, the level is not the dataset's, a static chunk carries `t` or a per_t one lacks it, the labels are not a 3-D u8/u16 volume, the descriptor is into another segment or the wrong length, the inline field is over 8 MiB, or the box is outside the level's shape; **dropped and counted** (`host.dropped_chunks`) when its key is not in the current set; **applied** otherwise — copied out into `host.chunks[(overlay, level, t)]` and `host.chunk_log`, its loan released in the same tick unless `host.hold_chunk_releases` is set (then `host.held_chunks`, `release_chunk`, `release_held_chunks` — how a scenario watches a plugin DEFER on a full arena). `await_chunks(overlay, keys)` / `await_chunk_log(n)` wait |
| Tables and misuse | `OverlayLabelTable` → `host.label_tables` / `host.table_log` (LUT inline or a loan, names and visibility by id, style; a malformed LUT refused on 30). An `OverlayUpdate` to a chunked overlay is refused on 30 and its loans released; `OverlayRemove` clears its chunks and table; `CompileLabelVolume` naming one is `INVALID_ARGUMENT` (§8.8) |

### Moving the view (§8.15)

| | |
|---|---|
| Set-view | **`invoke_widget(panel, widget, row=, **args)`** fires a widget's command as the person would, and for a `moves_view` widget with a fresh `cause` (`last_cause`); a newer cause from the same widget and row supersedes an unanswered older one. `ViewRequest` is answered: `INVALID` (not exactly one cause, a decline with targets, no target and no decline, a cause never minted or forgotten after 20 s, `select_region` beside a different `frame_region`), `CONSUMED` (a different request under a spent cause; the same one again gets the same answer, not applied twice), `STALE` (past 2 s), `DATASET_CHANGED`, `DECLINED`, `NOT_DECLARED` (outside the `view_targets`), `NOT_ENABLED`, `EXPORT_ACTIVE` (`export_active`), `FOLLOW_ACTIVE`, `SUPERSEDED` (`person_touches("t" \| "z" \| "selection" \| "camera")` since the cause), `OUT_OF_RANGE`, `UNKNOWN_REGION`, else `APPLIED`, which moves the emulator's `t` / `z` / selection / `camera` (`ViewChanged` follows). `view_script = (outcome, reason, detail)` answers any request after the cause checks, for a result the rules cannot reach. `view_requests` and `view_moves` keep every one. No cause is minted with no dataset open |
| The switch | the person's "let the plugin move the view" switch is per panel, on a panel with `view_targets` or `follow_targets`, and **off at every open**: `set_view_toggle(panel, True)`, or `view_toggle = True` for every panel. It counts only while its panel is open and showing |
| A tool's FIRE | is a cause when the tool declares `view_targets`, keyed `(tool, interaction)` as `fire()` sends it, minted before the record goes, and judged by the panel rules. It is enabled only by the switch of an open panel that declares `view_targets`. The tool fixture is `fixture_tool/` |
| Follow | **Offers** by panel in `follow_offers` (kept for an unopened panel; one from a panel without `follow_targets` is a diagnostic). **`press_follow(panel)`** starts one only on an open, showing panel with an offer, its switch on, a dataset open and no export, ending one from another panel `REPLACED`; pressed on the panel whose Follow runs, it is Stop (`STOPPED`); `FollowStarted` carries the panel's `follow_targets`. **States** are taken if newer than the last applied and the pending one (else `follow_dropped`), applied at up to 30 Hz by a 33 ms pump into `follow_applied`; one naming nothing, an undeclared target, or one the target checks refuse ends it `INVALID`. **Ends**: `EndFollow` → `PLUGIN_ENDED`; `person_touches` on a driven target → `USER_TOOK_CONTROL` (then `resume_follow()` starts it `resumed`); the switch off → `STOPPED`; `stop_follow()`; withdrawing the offer → `WITHDRAWN`; `close_panel(panel)` → `PANEL_CLOSED`; a new `open_generation` → `DATASET_CHANGED`; `end_follow(reason, detail)` scripts the rest (`AUTOSPIN`, `EXPORT`). Every `FollowStarted`/`FollowEnded` is in `follows_started`/`follows_ended` |
| Not modelled | a held slider's one-in-flight stream (`invoke_widget` sends what a driver sends), viewpoints, Play |

### Long-running work (§8.16)

`WorkProgress` is received as vizi receives it: title and text cut to 256
bytes at a character boundary, a report for a request the host is not waiting
on dropped (`host.work_dropped`), a task past §9's 64 not shown
(`host.tasks_unshown`); every report kept in `host.work_reports`, the latest
per subject in `host.work[("request" | "task", id)]`, and
`await_work(key, predicate)` waits on the latest (it coalesces).
`request_async(**arm)` / `invoke_async(command_id, **args)` return a
`PendingRequest` (`.id`, `.result(timeout)`) so a scenario can cancel work in
flight: `cancel_work(request_id=)` or `cancel_work(task_id=)` is a host
one-way, and the request keeps its slot until it is answered. `CANCELLED`
arrives as a `PluginError` like any other kind; the emulator marks no fault
for any. `stop_plugin(deadline_ms)` is *Stop plugin*: `Shutdown{USER_STOPPED}`,
the window plus a second's grace, since a plugin may spend its window letting
ignored work finish.

### Processing stages (`chain.py`)

vizi drives a chain of one stage (an applied filter) and no chain of several;
`chain.py` drives chains of several. `Chain(dataset)`
runs several plugin sessions, each a `Host` with its own session epoch, and
plays the host's half of `processing_stages.md`:

- `PlanStage` per position with every declared param typed from the manifest
  (§2), each answer the next one's input, `adds` indices assigned above the
  source's channel count, §3.1's backward walk for the channels to load
  (re-planned with the smaller input when it is smaller) and §3.2's
  `plane_cap` and `capacity`;
- ONE `S` ring (`StageRing`: empty subject, `STAGED` slots at 64-byte offsets,
  producer `HOST`, read-write, no future-write seal) granted to every session
  **under one id minted from an epoch no session uses** (§4), each grant
  awaited;
- `StageArm` per position awaiting its `StageArmAck`, or
  `send_arm(step, **overrides)` for a bad one; a position counts as the
  stage's once an arm is acknowledged there, and `start` refuses while any
  current revision is unacknowledged (§4);
- the slot filled from its own §5.1 codec (not the SDK's);
- the `u64` turn word with a signed position (§6.1), the host storing `first`,
  holding every position no stage is armed at (`chain.inspect` sees the slot
  there) and taking the slot back at `−k` or after the last stage;
- `D` to the stage a slot is at, and every stage's `D` **relayed** to the
  session the slot moved to (`relay=False` or `relay_delay` to prove a stage
  polls);
- the host's deadline (`DEADLINE_S` 2 s; §7 leaves it to the host) with the
  slot quarantined until the turn reaches a host position (§7); a session gone
  while it holds a turn loses it (`kill_session`; the host stores `−k`);
- `regrow()` supersedes the ring with a larger one in every session, retires
  it in a session with no stage left, and re-arms every position (§4).

Driven by `examples/stage_chain/python/hostsim.py`.

## How `V` and `X` are granted, and the grant backstop

| | |
|---|---|
| **`V`, the host's way** | leave `host.volume_segment` unset and the emulator grants `V` as vizi's `1.0` host does: nothing until the first read, then a ring sized to exactly that deposit, `M` at once and the installation riding the first reply; a deposit that does not fit gets a new ring of `max(deposit, 2 × capacity)` at a new id. The grants are in `host.v_rings`. A second reply into a ring whose installation already rode goes **bare**, for the plugin to hold (`host.bare_replies`), and nothing is written into an outgrown ring. **`supersedes` names only a drained ring**: an outgrown ring that still has deposits lent is sent `RetireSegment` when its last deposit comes back instead (`host.v_retired_on_drain`), because superseding it would let the plugin retire it under an uncopied deposit. `VIZI_HOSTSIM_SUPERSEDE_UNDRAINED=1` (or `host.v_supersede_undrained`) supersedes it undrained, to reproduce that failure. A failed replacement leaves the incumbent current (§9). At exit a run prints one line saying how many rings, bare replies and drained retirements it saw. `movie_seg`, `interactive_seg` and `ray_probe` run this way; setting `volume_segment` to a driver-granted ring keeps one fixed ring, which `peak_roi`'s ride-along scenarios need. **Coverage:** every implementation grows `V` in these drivers, but only `selfcheck.py`'s concurrent-reads scenario reliably produces a bare reply |
| **`X`, one per export** | `host.grant_export_arena(payload_bytes)` grants a fresh `X` sized to one chunk, `M` and the standalone install before `OpenExport` with no acknowledgement awaited, every chunk at the same offset and the arena reused at each `ChunkWritten` (§2.3); `host.retire_export_arena()` retires it afterwards, as the host does at the end of the job. `format_bridge`'s multichannel export uses it; its first export keeps the fixed `X` for the late-`M` scenario |
| **The grant backstop** | `host.query_grant(seg)` sends `GrantStateQuery` and returns the `GrantState` push; `host.recover_grant(seg)` does what `doorbell_M_memory.md` §7's table obliges — nothing for `installed`/`failed`/`released`, the missing `InstallSegment` (never `M`) for `pending`, and for `not_present` **the same grant again, `M` byte for byte with the same doorbell id** (`host.resend_capability`), as the host does. `grant_capability(seg, lost=True)` numbers an `M` without sending it, which is how a scenario loses one. The actions are in `host.recoveries` |

## Generic regions and the types a plugin shapes

This half follows `control_plane_protocol.md` §8.8's text; where vizi checks
something itself (a handle `frame` of 9 values, no `views`, no
`direction_specs`), it follows vizi. The geometry is `regions.py`, pure
functions a driver may call too.

| | |
|---|---|
| **Contributions** | `ContributeRois` also refuses, `INVALID_ARGUMENT` naming the rule: a `vizi.` type; `SHELL`/`POINTS`; an `enclosing` control no compile row exposes (rule 0); an `ENCLOSED` surface that is not closed (rule 1, combinatorial); a `STAR` cloud with fewer than 4 points, one at the root, two on one ray, or directions that do not strictly surround the root (rule 2); a `DirectionHandle` on an asked region; an index out of range. Every rule reads display space, with absolute STAR tolerances. `host.region_types` is the manifest's §3.8 list, and a region of one of those types gets `RoiRecord.type_owner` |
| **A declared drag** | `host.drag_handle(region_id, handle, p1)`: the ROI scheme's §4 order (project onto the handle's DoF, clamp a `PRIMARY` handle's extent from `extent_anchor`), then one level of `LinearBinding`s, as one edit (a new revision). Returns the `Resolution` |
| **An asked drag** | `drag = host.begin_shape_drag(region_id, handle)`; `drag.move(p1)` sends `shape_drag` (projected, at most one per 40 ms tick); `drag.await_proposal()`; `drag.release()` sends `shape_commit` and waits for `shape_answer` (supervised: no deadline; `supervised=False` is §9's 10 s, then `REVERTED`); `drag.cancel()`. `host.spinbox_edit(...)` is a commit with no drag. `drag.send_late_drag()` sends a `shape_drag` after the drag ended, as the wire's per-queue order allows. `ShapeEditEnded` goes out on every ending, and a region deleted, contributed again, or the dataset closed ends its edit `CANCELLED` |
| **What the host checks** | `shape_out_of_step` for a `drag_id` never sent, a `seq` never sent in that drag, a second answer to one commit, and (this emulator's reading) an answer before any commit; `shape_refused` for a shape that is not one finite position per vertex, or that leaves the region no inside. A reply for an ended drag, a proposal after its commit, and one older than the shape on screen are dropped silently. `host.shape_proposals`, `host.shape_answers`, `host.shape_drags` keep what happened |
| **Not emulated** | the inside of a region (no rasterising: the voxels a curve covers are the host's own certified tessellation, within a quarter voxel, so there is nothing exact to emulate; `ReadRoiInclusion` is unchanged), handles locked while the type's plugin has no session, the lazy launch on selecting a region (manifest §6), undo/redo, and a held region refusing other edits beyond a second drag |

`examples/tetra_shape/` is the example that drives it.

## The emulator's own check

```bash
cd examples/vizi_hostsim/fixture && uv venv --python 3.11 && uv pip install --editable ../../.. atomicshm
cd .. && fixture/.venv/bin/python selfcheck.py
```

`fixture/` is a plugin on the reference SDK with one of everything the
emulator answers that the example drivers do not reach: a reader with
`read_plane` and `metadata`, a reader of files holding several images (the
`stack`), the long-running shapes (a command that honours Cancel, one that
ignores it, a background task, the `slow` reader), an exporter with abort
cleanup, a label-volume push and compile through the reserve-twice route,
`get_rois`, `read_mask_voxels`, a native ROI contribution, a runtime asset
request, and a chunked overlay with a `@chunk_source` plus a command that
misuses it (an `OverlayUpdate` to it, a chunk the host never named, a compile
of it) so the refusal and drop paths run somewhere. `fixture_tool/` is a
second, smaller plugin that declares a tool.

`selfcheck.py` runs the encoder, folder-name, manifest and bound checks, drives
`fixture/` through its scenarios in one session, then runs a few short
sessions of their own for the scenarios that need one (a tool's cause, *Stop
plugin*, a refused shape, the launch budget, raw reads, and on Linux the plain
hang-up). It ends with `emulator self-check passed`. Run it after changing the
emulator; it is what says the emulator still answers what a plugin asks.

The fixture's `fixture.progress`, `fixture.track`, `fixture.inclusion` and
`fixture.picker_status` commands and `selfcheck.py`'s scenarios for them are
worked examples of the progress widget, the pose window, inclusion and the
picker's lifecycle.

## Not emulated

- A host-produced `PULSED` region (`D` host→plugin); the `1.0` host does not
  send one either.
- The host's want *policy* for a chunked overlay (which `(level, t)` the
  panes ask for, the scrub-direction prefetch) and `GrantFailed{too_large}`
  on a dataset the session ceiling cannot fund: a driver names its own
  wants, and every arena here is funded.
- The compile's transform rules are checked as
  `examples/interactive_seg/SPEC.md` states them; whether the `1.0` host's
  are exactly those is not settled here.
- Of processing stages (`chain.py`): several pipelines on one ring (a
  `Chain` is one pipeline).
- A region vizi holds as several stitched segments (a `RoiSeq`): every region
  here is one `Roi`.
- Re-posing a region's vertices at the frame on screen for a windowless
  `GetRois`: the emulator clears the rows and `t_start`/`t_end` as the host
  does, but leaves the vertices at rest.
- `VIZI_PLUGIN_ASSETS` and `RequestAsset.force_reprompt` (the host's store
  copy of a picked file): the emulator answers the picked path itself.
- Windows.
