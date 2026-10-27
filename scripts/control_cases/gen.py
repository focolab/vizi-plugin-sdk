"""Control-plane forward-compatibility cases: what a 1.0 receiver does when a
later 1.x peer sends a message kind, field or enum value it does not know.

    python -I scripts/control_cases/gen.py [--no-record]  # from cases/recording.jsonl
    python -I scripts/control_cases/gen.py --record       # re-record first (minutes)
    python -I scripts/control_cases/gen.py --recording FULL.jsonl
    python -I scripts/control_cases/gen.py --check        # committed cases are current

Everything is derived from this working tree; nothing per message is written
by hand:

  * the descriptor set, read from the compiled stubs in
    ``vizi_plugin_sdk/_proto/*_pb2.py`` without importing them;
  * the kind -> class partition in ``vizi_plugin_sdk/control.py``
    (``HOST_CLASSES``, ``PLUGIN_CLASSES``, ``REQUEST_REPLIES``, ``REPLY_KINDS``);
  * the rule anchors, found in ``docs/spec/``;
  * baselines: real, accepted instances recorded over the real transport while
    the emulator's self-check and the example drivers ran (``record.py``).

Outputs in ``cases/``: ``manifest.jsonl.gz`` (one case per line, gzip without
a timestamp so a regeneration is byte-identical), ``counts.json``, and, after
``--record`` or ``--recording``, ``recording.jsonl``: the recorded instances
the cases are built from, and nothing else.
"""

from __future__ import annotations

import argparse
import ast
import base64
import collections
import copy
import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CASES = HERE / "cases"
SPEC_DIR = ROOT / "docs/spec"

PKG = "vizi.plugin.v1"
ENVELOPES = {"HostMsg": "host->plugin", "PluginMsg": "plugin->host"}
OPPOSITE = {"HostMsg": "PluginMsg", "PluginMsg": "HostMsg"}

WT_VARINT, WT_I64, WT_LEN, WT_I32 = 0, 1, 2, 5
# A small valid submessage for an unknown arm: field 1, varint 1.
SMALL_SUBMSG = b"\x08\x01"
UNKNOWN_FIELD_PAYLOADS = {
    "varint": (WT_VARINT, b"\x01"),
    "length-delimited": (WT_LEN, b"\x03gen"),
    "fixed32": (WT_I32, b"\x01\x00\x00\x00"),
    "fixed64": (WT_I64, b"\x01\x00\x00\x00\x00\x00\x00\x00"),
}
MAX_DEPTH = 24
# The Value kinds a declared stage param takes (float, int, bool, choice/string).
STAGE_PARAM_KINDS = ("s", "f", "i", "b")
HANDSHAKE_MINORS = (1, 99)
# The id forms of a 1.x peer: host requests `r<n>`, plugin requests `q<n>`.
HOST_ID, PLUGIN_ID = "r1", "q1"


# ---------------------------------------------------------------- wire helpers


def varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def tag(number: int, wt: int) -> bytes:
    return varint((number << 3) | wt)


def ld(number: int, payload: bytes) -> bytes:
    return tag(number, WT_LEN) + varint(len(payload)) + payload


def b64(b: "bytes | None") -> "str | None":
    return None if b is None else base64.b64encode(b).decode()


def ser(m) -> bytes:
    return m.SerializeToString(deterministic=True)


# ---------------------------------------------------------------- tree inputs


def serialized_pb(pb2_source: bytes) -> bytes:
    tree = ast.parse(pb2_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "AddSerializedFile":
            return node.args[0].value
    raise SystemExit("no AddSerializedFile in pb2 source")


def module_literal(source: bytes, name: str):
    """A module-level `NAME = {...}` / `NAME: T = {...}` literal, with
    `Class.X` attribute values rendered as the string 'X'."""
    tree = ast.parse(source)
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == name:
            value = node.value
            if isinstance(value, ast.Call):  # frozenset({...})
                value = value.args[0]
            if isinstance(value, ast.Dict):
                return {
                    ast.literal_eval(k): (v.attr if isinstance(v, ast.Attribute) else ast.literal_eval(v))
                    for k, v in zip(value.keys, value.values)
                }
            if isinstance(value, ast.Set):
                return {ast.literal_eval(e) for e in value.elts}
    raise SystemExit(f"{name} not found in control.py")


def optional_literal(source: bytes, name: str):
    """:func:`module_literal`, or None when control.py has no such name."""
    try:
        return module_literal(source, name)
    except SystemExit:
        return None


# ---------------------------------------------------------------- descriptors


class Schema:
    def __init__(self, files: "dict[str, bytes]"):
        self.fdps = {n: descriptor_pb2.FileDescriptorProto.FromString(b) for n, b in files.items()}
        self.pool = descriptor_pool.DescriptorPool()
        for n in ("roi_pb2.py", "vizi_plugin_pb2.py"):
            self.pool.AddSerializedFile(files[n])
        self.dp = {}  # full name -> DescriptorProto
        self.synthetic_oneofs = set()  # (msg full name, oneof name)

        def walk(prefix, msgs):
            for m in msgs:
                full = f"{prefix}.{m.name}"
                self.dp[full] = m
                for f in m.field:
                    if f.proto3_optional:
                        self.synthetic_oneofs.add((full, m.oneof_decl[f.oneof_index].name))
                walk(full, m.nested_type)

        for fdp in self.fdps.values():
            walk(fdp.package, fdp.message_type)

    def msg(self, full: str):
        return self.pool.FindMessageTypeByName(full)

    def cls(self, desc):
        return message_factory.GetMessageClass(desc)

    def next_number(self, desc) -> int:
        dp = self.dp[desc.full_name]
        top = max([f.number for f in dp.field] + [r.end - 1 for r in dp.reserved_range] + [0])
        return top + 1

    def real_oneofs(self, desc):
        return [o for o in desc.oneofs if (desc.full_name, o.name) not in self.synthetic_oneofs]


def is_repeated(fd) -> bool:
    r = getattr(fd, "is_repeated", None)
    return fd.label == fd.LABEL_REPEATED if r is None else bool(r)


def is_map(fd) -> bool:
    return fd.message_type is not None and fd.message_type.GetOptions().map_entry


# ---------------------------------------------------------------- instances


def synth_fill(schema: Schema, msg) -> None:
    """Mechanical minimum for a message the recording did not supply: string
    ids named like ids get a value, protocol_major is 1, and every real oneof
    gets its lowest-numbered arm so an unknown-arm injection has an arm to
    remove."""
    d = msg.DESCRIPTOR
    for f in d.fields:
        if is_repeated(f):
            continue
        if f.type == f.TYPE_STRING and (f.name == "id" or f.name.endswith("_id")) and f.containing_oneof is None:
            setattr(msg, f.name, f"gen-{f.name}")
        if f.name == "protocol_major" and f.containing_oneof is None:
            setattr(msg, f.name, 1)
    for o in schema.real_oneofs(d):
        if msg.WhichOneof(o.name) is not None:
            continue
        f = sorted(o.fields, key=lambda x: x.number)[0]
        set_minimal(msg, f)


def set_minimal(msg, f) -> None:
    if f.type == f.TYPE_MESSAGE:
        getattr(msg, f.name).SetInParent()
    elif f.type == f.TYPE_STRING:
        setattr(msg, f.name, "gen")
    elif f.type == f.TYPE_BYTES:
        setattr(msg, f.name, b"gen")
    elif f.type == f.TYPE_BOOL:
        setattr(msg, f.name, True)
    elif f.type in (f.TYPE_FLOAT, f.TYPE_DOUBLE):
        setattr(msg, f.name, 1.0)
    elif f.type == f.TYPE_ENUM:
        vals = sorted(v.number for v in f.enum_type.values)
        setattr(msg, f.name, vals[1] if len(vals) > 1 else vals[0])
    else:
        setattr(msg, f.name, 1)


def map_default_key(fd):
    k = fd.message_type.fields_by_name["key"]
    return "gen" if k.type == k.TYPE_STRING else (True if k.type == k.TYPE_BOOL else 1)


class _MapCursor:
    def __init__(self, container, key, entry):
        self.container, self.key, self.entry = container, key, entry


def materialize(schema: Schema, root, steps):
    """Return (instance, notes) where every step of `steps` is present.  Each
    newly created message is synth-filled; a oneof displaced to reach the path
    is noted."""
    inst = copy.deepcopy(root)
    notes = []
    msg = inst
    for fd, sel in steps:
        if is_map(fd):
            m = getattr(msg, fd.name)
            if sel not in m:
                vf = fd.message_type.fields_by_name["value"]
                if vf.type == vf.TYPE_MESSAGE:
                    synth_fill(schema, m[sel])
                else:
                    m[sel] = {vf.TYPE_STRING: "gen", vf.TYPE_BOOL: True}.get(vf.type, 1)
                notes.append(f"created {fd.name}{{{sel!r}}}")
            entry_cls = schema.cls(fd.message_type)
            e = entry_cls()
            e.key = sel
            vf = fd.message_type.fields_by_name["value"]
            if vf.type == vf.TYPE_MESSAGE:
                e.value.CopyFrom(m[sel])
            else:
                e.value = m[sel]
            # Descending past a map entry continues inside the live map value.
            msg = _MapCursor(m, sel, e)
            continue
        if isinstance(msg, _MapCursor):
            msg = msg.container[msg.key]  # step into the entry's `value`
            continue
        if is_repeated(fd):
            r = getattr(msg, fd.name)
            if len(r) == 0:
                synth_fill(schema, r.add())
                notes.append(f"created {fd.name}[0]")
            msg = r[sel]
            continue
        o = fd.containing_oneof
        if o is not None and (msg.DESCRIPTOR.full_name, o.name) not in schema.synthetic_oneofs:
            cur = msg.WhichOneof(o.name)
            if cur != fd.name:
                if cur is not None:
                    notes.append(f"displaced {o.name}={cur}")
                getattr(msg, fd.name).SetInParent()
                synth_fill(schema, getattr(msg, fd.name))
                notes.append(f"created {fd.name}")
        elif not msg.HasField(fd.name):
            getattr(msg, fd.name).SetInParent()
            synth_fill(schema, getattr(msg, fd.name))
            notes.append(f"created {fd.name}")
        msg = getattr(msg, fd.name)
    return inst, notes


def encode_with(schema: Schema, msg, steps, leaf):
    """Serialize `msg`, replacing the node at `steps` by leaf(node) bytes.  The
    path field is cleared and re-appended by hand, so the baseline (leaf =
    plain serialize) and every injection share one byte layout."""
    if not steps:
        return leaf(msg)
    fd, sel = steps[0]
    c = copy.deepcopy(msg)
    c.ClearField(fd.name)
    head = ser(c)
    out = b""
    if is_map(fd):
        entry_cls = schema.cls(fd.message_type)
        vf = fd.message_type.fields_by_name["value"]
        m = getattr(msg, fd.name)
        for k in sorted(m.keys()):
            e = entry_cls()
            e.key = k
            if vf.type == vf.TYPE_MESSAGE:
                e.value.CopyFrom(m[k])
            else:
                e.value = m[k]
            if k == sel:
                rest = steps[1:]
                eb = encode_with(schema, e, rest, leaf) if rest else leaf(e)
            else:
                eb = ser(e)
            out += ld(fd.number, eb)
    elif is_repeated(fd):
        for i, el in enumerate(getattr(msg, fd.name)):
            eb = encode_with(schema, el, steps[1:], leaf) if i == sel else ser(el)
            out += ld(fd.number, eb)
    else:
        out = ld(fd.number, encode_with(schema, getattr(msg, fd.name), steps[1:], leaf))
    return head + out


def walk_paths(schema: Schema, env_desc, arm):
    """Every message node reachable under one envelope arm, as (path, steps,
    descriptor).  The envelope root comes first.  A message type already on
    the current path is not re-entered."""
    yield env_desc.name, [], env_desc
    stack = [(f"{env_desc.name}.{arm.name}", [(arm, None)], arm.message_type, (env_desc.full_name, arm.message_type.full_name))]
    while stack:
        path, steps, d, seen = stack.pop()
        yield path, steps, d
        if len(steps) >= MAX_DEPTH or d.GetOptions().map_entry:
            continue
        for f in sorted(d.fields, key=lambda x: x.number, reverse=True):
            if f.type != f.TYPE_MESSAGE:
                continue
            if is_map(f):
                entry = f.message_type
                key = map_default_key(f)
                epath = f"{path}.{f.name}{{}}"
                vf = entry.fields_by_name["value"]
                if vf.type == vf.TYPE_MESSAGE and vf.message_type.full_name not in seen:
                    stack.append(
                        (f"{epath}.value", steps + [(f, key), (vf, None)], vf.message_type, seen + (vf.message_type.full_name,))
                    )
                stack.append((epath, steps + [(f, key)], entry, seen))
                continue
            if f.message_type.full_name in seen:
                continue
            sel = 0 if is_repeated(f) else None
            suffix = "[]" if is_repeated(f) else ""
            stack.append((f"{path}.{f.name}{suffix}", steps + [(f, sel)], f.message_type, seen + (f.message_type.full_name,)))


def node_at(schema, inst, steps):
    """The node message at `steps`; a map entry node is a fresh entry object
    built from the live (key, value), since protobuf exposes no entry."""
    msg = inst
    for i, (fd, sel) in enumerate(steps):
        if isinstance(msg, _MapCursor):
            msg = msg.container[msg.key]
            continue
        if is_map(fd):
            m = getattr(msg, fd.name)
            e = schema.cls(fd.message_type)()
            e.key = sel
            vf = fd.message_type.fields_by_name["value"]
            if vf.type == vf.TYPE_MESSAGE:
                e.value.CopyFrom(m[sel])
            else:
                e.value = m[sel]
            msg = _MapCursor(m, sel, e) if i + 1 < len(steps) else e
            continue
        msg = getattr(msg, fd.name)[sel] if is_repeated(fd) else getattr(msg, fd.name)
    return msg


# ---------------------------------------------------------------- spec anchors


def anchor(doc: str, needle: str) -> str:
    """`<doc> §N "<needle>"`: the section a rule sits in, found by text, so an
    anchor survives an edit elsewhere in the document.  Fails when the text
    has gone, because every expected outcome cites one."""
    section = None
    for line in (SPEC_DIR / doc).read_text().splitlines():
        m = re.match(r"#{2,4} (\d+[a-z]?(?:\.\d+)*)\.? ", line)
        if m:
            section = m.group(1)
        if needle in line:
            return f'{doc} §{section} "{needle.replace("**", "")}"'
    raise SystemExit(f"anchor text not found in {doc}: {needle!r}")


def anchors() -> "dict[str, str]":
    cp = "control_plane_protocol.md"
    return {
        "handshake": anchor(cp, "**The host sends `Hello{protocol_major, protocol_minor, session_token}`"),
        "major": anchor(cp, "**The version match is on the MAJOR, and both ends check.**"),
        "minor_serves": anchor(cp, "A `1.1` host serves a `1.0` plugin"),
        "append_only": anchor(cp, "**Within a major the wire is append-only**"),
        "rule1": anchor(cp, "1. **Every message that deserves a reply carries an id unique on its session"),
        "reuse": anchor(cp, "7. **A second message under a used id is a protocol error, except a re-send.**"),
        "one_reply": anchor(cp, "8. **A pending request's id belongs to its reply alone.**"),
        "after_handshake": anchor(cp, "**The unknown-kind rule applies only after the handshake.**"),
        "ready_ceiling": anchor(cp, "**`Ready.protocol_minor` MUST NOT exceed the host's `Hello` minor.**"),
        "rule5": anchor(cp, "5. **An unmatched id is never silently discarded.**"),
        "reack": anchor(cp, "4. **The re-ack rule: a re-sent arrangement is acknowledged again**"),
        "content": anchor(cp, "The same identity carrying *different content* is a"),
        "fwd": anchor(cp, "**Forward compatibility is not a violation.**"),
        "kind": anchor(cp, "**An unknown message KIND is the same, with one obligation added.**"),
        "arm_plan": anchor("processing_stages.md", "no answered `PlanStage` for its `stage_id`, `input` and `params` stands behind"),
        "plan_refused": anchor("processing_stages.md", "A stage that cannot work on the offered input answers `Error` naming why"),
        "arm_refused": anchor("processing_stages.md", "`STAGE_ARM_REFUSED`; the arrangement's deadline"),
        "binding_refused": anchor(cp, "names no statement either, so a receiver **refuses it and does"),
        "widget_kind": anchor("manifest_schema.md", "**Unknown kinds are rejected**"),
    }


# ---------------------------------------------------------------- recording


def load_recording(path: Path):
    records, meta = [], None
    with open(path) as fh:
        for line in fh:
            r = json.loads(line)
            if "meta" in r:
                meta = r["meta"]
                continue
            r["env"] = "HostMsg" if r["direction"] == "host->plugin" else "PluginMsg"
            r["raw"] = base64.b64decode(r["b64"])
            records.append(r)
    if meta is None:
        raise SystemExit(f"{path}: no meta line (record.py --all writes one)")
    return records, meta


def run_recorder() -> Path:
    full = Path(tempfile.mkdtemp(prefix="vizi-control-cases-")) / "recording.jsonl"
    rc = subprocess.run([sys.executable, "-I", "-B", str(HERE / "record.py"), "--all", str(full)]).returncode
    if rc != 0:
        raise SystemExit("record.py failed; the recording is incomplete")
    return full


# ---------------------------------------------------------------- generation


def generate(recording: "list[dict]", meta: dict):
    """(cases, kept records, counts) from one recording and this tree."""
    pb2_files = {
        n: serialized_pb((ROOT / "vizi_plugin_sdk/_proto" / n).read_bytes()) for n in ("roi_pb2.py", "vizi_plugin_pb2.py")
    }
    schema = Schema(pb2_files)
    control = (ROOT / "vizi_plugin_sdk/control.py").read_bytes()
    host_classes = module_literal(control, "HOST_CLASSES")
    plugin_classes = module_literal(control, "PLUGIN_CLASSES")
    request_replies = module_literal(control, "REQUEST_REPLIES")
    reply_kinds = module_literal(control, "REPLY_KINDS")
    alternate_replies = module_literal(control, "ALTERNATE_REPLIES")
    classes ={"HostMsg": host_classes, "PluginMsg": plugin_classes}
    A = anchors()

    envs = {n: schema.msg(f"{PKG}.{n}") for n in ENVELOPES}
    class_gaps = {}
    for n, d in envs.items():
        arms = {f.name for f in d.oneofs_by_name["msg"].fields}
        class_gaps[n] = {
            "arms_without_class": sorted(arms - set(classes[n])),
            "classes_without_arm": sorted(set(classes[n]) - arms),
        }

    recorded = collections.defaultdict(list)  # (env, kind) -> [record]
    by_session_id = collections.defaultdict(list)  # (session, env, id) -> [record]
    for r in recording:
        if r["kind"]:
            recorded[(r["env"], r["kind"])].append(r)
            if r["id"]:
                by_session_id[(r["session"], r["env"], r["id"])].append(r)
    # How many instances of each kind the drivers sent.  A pruned recording
    # carries the full run's counts in its meta line.
    seen = meta.get("seen") or {f"{e}.{k}": len(v) for (e, k), v in sorted(recorded.items())}

    def recorded_request(r):
        """The request a recorded message answers: same session, opposite
        direction, same id, earlier, and of a request class."""
        want = {"PluginMsg": ("HostMsg", {"HOST_REQUEST"}), "HostMsg": ("PluginMsg", {"PLUGIN_REQUEST"})}[r["env"]]
        best = None
        for q in by_session_id.get((r["session"], want[0], r["id"]), []):
            ok = classes[want[0]].get(q["kind"]) in want[1] or (r["kind"] == "grant_state" and q["kind"] == "grant_state_query")
            if ok and q["seq"] < r["seq"] and (best is None or q["seq"] > best["seq"]):
                best = q
        return best

    def class_id(env, kind, cls):
        if kind == "hello":
            return "Hello from vizi gen"
        if kind == "ready":
            return "Ready; I am gen 1.0"
        if cls in ("HOST_REQUEST", "REPLY") or kind in ("grant_state_query", "grant_state"):
            return HOST_ID
        if cls == "PLUGIN_REQUEST" or (env == "HostMsg" and kind in reply_kinds):
            return PLUGIN_ID
        return ""

    families = []
    kept = []  # the recorded instances the cases use
    for env_name, env_desc in envs.items():
        for arm in sorted(env_desc.oneofs_by_name["msg"].fields, key=lambda f: f.number):
            cls = classes[env_name].get(arm.name)
            env_cls = schema.cls(env_desc)
            recs = recorded.get((env_name, arm.name), [])
            pre = None
            if recs:
                # The smallest recorded instance with a non-empty payload
                # (minimal, and accepted by a live peer when recorded); among
                # replies, prefer one whose request is in the same session.
                def rank(r):
                    empty = len(r["raw"]) <= len(ld(arm.number, b"")) + 2 + len(r["id"])
                    unpaired = bool(r["id"]) and arm.name not in ("hello", "ready") and cls not in ("HOST_REQUEST", "PLUGIN_REQUEST") and recorded_request(r) is None
                    return (empty, unpaired, len(r["raw"]))

                r = sorted(recs, key=rank)[0]
                kept.append(r)
                root = env_cls.FromString(r["raw"])
                source = f"recorded {r['session']} seq {r['seq']} ({seen[f'{env_name}.{arm.name}']} seen; smallest non-empty chosen)"
                q = recorded_request(r) if r["id"] and cls not in ("HOST_REQUEST", "PLUGIN_REQUEST") and arm.name not in ("hello", "ready") else None
                if q is not None:
                    kept.append(q)
                    pre = {"envelope": q["env"], "direction": q["direction"], "kind": q["kind"], "id": q["id"],
                           "b64": q["b64"], "source": f"recorded {q['session']} seq {q['seq']}"}
            else:
                root = env_cls()
                getattr(root, arm.name).SetInParent()
                synth_fill(schema, getattr(root, arm.name))
                root.id = class_id(env_name, arm.name, cls)
                source = "synthesized: no driver sends it, so the baseline is the mechanical minimum and no peer has accepted it"
            families.append(
                {
                    "family": f"{env_name}.{arm.name}", "envelope": env_name, "arm": arm.name,
                    "message": arm.message_type.full_name, "direction": ENVELOPES[env_name], "class": cls,
                    "baseline_source": source, "root": root, "precondition": pre,
                }
            )
    fam_by_name = {f["family"]: f for f in families}
    # The reply kind each plugin request takes: control.py's
    # PLUGIN_REQUEST_REPLIES where it has one, otherwise the pairs the
    # recording shows (a host reply, and the plugin request it answered).
    plugin_request_replies = optional_literal(control, "PLUGIN_REQUEST_REPLIES")
    if plugin_request_replies is None:
        plugin_request_replies = {
            f["precondition"]["kind"]: f["arm"] for f in families
            if f["envelope"] == "HostMsg" and f["precondition"] and f["arm"] in reply_kinds and f["arm"] != "host_error"
        }

    # What a receiver must already hold for a family's baseline to be
    # accepted: a StageArm needs its ring, its stage and the PlanStage behind
    # it (processing_stages.md §4); an EpochChange needs the TimeMapping its
    # datasets name (§8.11); an InstallSegment completes a grant whose doorbell
    # half has landed (§8.12).  A replayer establishes `setup` before the case.
    pb = {n: schema.cls(schema.msg(f"{PKG}.{n}")) for n in ("HostMsg", "PlanStage")}
    arm_fam = fam_by_name["HostMsg.stage_arm"]
    arm_root = arm_fam["root"]
    def plan_for(arm):
        """The PlanStage behind a StageArm, and the stage it needs declared."""
        plan = pb["HostMsg"](id=fam_by_name["HostMsg.plan_stage"]["root"].id)
        plan.plan_stage.stage_id = arm.stage_arm.stage_id
        plan.plan_stage.input.CopyFrom(arm.stage_arm.input)
        for k, v in arm.stage_arm.params.items():
            plan.plan_stage.params[k].CopyFrom(v)
        params = [{"key": k, "kind": arm.stage_arm.params[k].WhichOneof("kind")} for k in sorted(arm.stage_arm.params)]
        return plan, {
            "stage_ring": {"segment_id": arm.stage_arm.segment_id, "purpose": "S"},
            "declare_stage": {"stage_id": arm.stage_arm.stage_id, "params": params},
        }

    plan_root, stage_setup = plan_for(arm_root)
    hm_desc = envs["HostMsg"]

    def step(msg_bytes, kind):
        return {"direction": "host->plugin", "kind": kind, "b64": b64(msg_bytes)}

    tm = fam_by_name["HostMsg.time_mapping"]["root"].time_mapping
    for ds in fam_by_name["HostMsg.epoch_change"]["root"].epoch_change.datasets:
        if (ds.gen, ds.time_mapping_revision) != (tm.gen, tm.revision):
            raise SystemExit("the recorded EpochChange names a TimeMapping other than the recorded one")
    # The recorded PlanStage names a stage the recording's plugin did not
    # declare; the receiver declares it, so the baseline is planned and a
    # case reaches the planner's checks.
    plan_stage_root = fam_by_name["HostMsg.plan_stage"]["root"].plan_stage
    family_setup = {
        "HostMsg.stage_arm": dict(stage_setup, prior=[step(ser(plan_root), "plan_stage")]),
        "HostMsg.plan_stage": {"declare_stage": {
            "stage_id": plan_stage_root.stage_id,
            "params": [{"key": k, "kind": plan_stage_root.params[k].WhichOneof("kind")} for k in sorted(plan_stage_root.params)],
        }},
        "HostMsg.epoch_change": {"prior": [step(ser(fam_by_name["HostMsg.time_mapping"]["root"]), "time_mapping")]},
        # The doorbell's half of the grant, so the InstallSegment completes it.
        "HostMsg.install_segment": {"capability": {"segment_id": fam_by_name["HostMsg.install_segment"]["root"].install_segment.segment_id}},
    }

    cases = []
    selfcheck = collections.Counter()

    def kind_outcome(arm_name, idv):
        if arm_name in ("hello", "ready"):
            return "session-faulted", (
                f"replaces the handshake message: {A['handshake']} requires it first and fails the session on a "
                f"malformed first message, and {A['after_handshake']}, so {A['kind']}'s Error{{UNSUPPORTED}} does not apply."
            )
        if idv:
            return "unsupported", f"{A['kind']}: an unknown kind with a non-empty id and nothing in flight is answered Error{{UNSUPPORTED}} echoing the id; the session continues"
        return "diagnostic-only", f"{A['kind']}: an unknown kind with an empty id is a push: a local diagnostic, no reply, no ContractViolation; the session continues"

    def case(**kw):
        cases.append(kw)

    def item_refusal(arm_name, f):
        """The rule for an unknown enum value that refuses its one item, or
        None: a plan or an arm with an unknown DType, and a binding with an
        unknown ToolMode.  The item is refused with a local diagnostic naming
        the value; it is never a ContractViolation and never a session fault."""
        if f.enum_type.full_name == f"{PKG}.DType" and arm_name == "plan_stage":
            return (f"{A['plan_refused']}: a dtype this build does not know is an input the stage cannot work on, so the "
                    f"plan alone is refused, answered Error{{UNSUPPORTED}} naming the dtype; {A['fwd']}: it is not a "
                    "ContractViolation and the session continues")
        if f.enum_type.full_name == f"{PKG}.DType" and arm_name == "stage_arm":
            return (f"{A['arm_refused']}: an arm with a dtype this build does not know cannot be used, so it is not "
                    f"acknowledged and the host sees it by the arrangement's deadline; {A['fwd']}: the refusal is a local "
                    "diagnostic naming the dtype, not a ContractViolation, and the session continues")
        if f.enum_type.full_name == f"{PKG}.ToolMode" and arm_name == "tool_binding":
            return (f"{A['binding_refused']}: a binding whose mode this build does not know is refused the same way, "
                    "neither activated nor acknowledged (ToolBindingAck carries no reason), and the reason is a local "
                    f"diagnostic naming the mode; {A['fwd']}: not a ContractViolation, and the session continues")
        return None

    # ---- per family: every node under the arm
    for fam in families:
        env_desc = envs[fam["envelope"]]
        arm = env_desc.fields_by_name[fam["arm"]]
        for path, steps, d in walk_paths(schema, env_desc, arm):
            inst, notes = materialize(schema, fam["root"], steps)
            node = node_at(schema, inst, steps)
            base = encode_with(schema, inst, steps, ser)
            nxt = schema.next_number(d)
            common = {
                "family": fam["family"], "direction": fam["direction"],
                "receiver": "plugin" if fam["envelope"] == "HostMsg" else "host",
                "class": fam["class"], "path": path, "node_message": d.full_name,
                "baseline_b64": b64(base),
                "baseline_source": fam["baseline_source"] + ("; materialized: " + ", ".join(notes) if notes else ""),
                "request_context": None, "precondition": fam["precondition"],
                "setup": family_setup.get(fam["family"]),
            }
            if fam["family"] == "HostMsg.stage_arm" and (
                    inst.stage_arm.input != arm_root.stage_arm.input or inst.stage_arm.params != arm_root.stage_arm.params
            ) and all(v.WhichOneof("kind") in STAGE_PARAM_KINDS for v in inst.stage_arm.params.values()):
                # Reaching the node created content the recorded arm lacks
                # (a params entry): the PlanStage behind it states the same,
                # so the baseline is accepted and the case alone differs.  A
                # stage param is never a Value.ints, so a node under one has
                # no plan to stand behind it and keeps the family's setup.
                plan, setup = plan_for(inst)
                common["setup"] = dict(setup, prior=[step(ser(plan), "plan_stage")])
            is_root = not steps

            # (a) an unknown oneof arm: the arm cleared, field max+1 in its place
            for o in schema.real_oneofs(d):
                def leaf(m, o=o, nxt=nxt):
                    c = copy.deepcopy(m)
                    c.ClearField(o.name)
                    return ser(c) + ld(nxt, SMALL_SUBMSG)

                removed = node.WhichOneof(o.name) if hasattr(node, "WhichOneof") else None
                inj = encode_with(schema, inst, steps, leaf)
                if is_root:
                    exp, rule = kind_outcome(fam["arm"], inst.id)
                    pre = None
                else:
                    exp = "not-a-violation"
                    rule = (
                        f"{A['fwd']}: unknown field {nxt} is skipped and is not a violation. The oneof "
                        f"{d.name}.{o.name} then decodes unset (removed arm: {removed}); the spec gives no rule for an "
                        "unset oneof beyond that, so only the absence of a ContractViolation and of a session fault is checked."
                    )
                    pre = fam["precondition"]
                case(**common, injection="unknown-oneof-arm", oneof=o.name, field_number=nxt, removed_arm=removed,
                     envelope_b64=b64(inj), expected=exp, rule=rule)
                cases[-1]["precondition"] = pre
                ref = copy.deepcopy(inst)
                node_at(schema, ref, steps).ClearField(o.name)
                want = schema.cls(env_desc).FromString(encode_with(schema, ref, steps, ser))
                got = schema.cls(env_desc).FromString(inj)
                got.DiscardUnknownFields()
                selfcheck["oneof_ok" if got == want else "oneof_bad"] += 1

            # (b) an unknown field, one per wire type.  Not inside a map entry:
            # an entry is exactly a key and a value and can never gain a
            # field, so no host or plugin can send one.
            for wtname, (wt, payload) in UNKNOWN_FIELD_PAYLOADS.items():
                if d.GetOptions().map_entry:
                    break
                inj = encode_with(schema, inst, steps, lambda m, wt=wt, payload=payload: ser(m) + tag(nxt, wt) + payload)
                got = schema.cls(env_desc).FromString(inj)
                got.DiscardUnknownFields()
                same = got == schema.cls(env_desc).FromString(base)
                selfcheck["field_ok" if same else "field_bad"] += 1
                exp, rule = "ignored", f"{A['fwd']} and {A['append_only']}: field {nxt} is skipped; the behavior is the baseline's, with at most a local diagnostic"
                case(**common, injection=f"unknown-field:{wtname}", field_number=nxt, envelope_b64=b64(inj),
                     expected=exp, rule=rule)

            # (c) unknown enum values
            for f in sorted(d.fields, key=lambda x: x.number):
                if f.type != f.TYPE_ENUM:
                    continue
                emax = max(v.number for v in f.enum_type.values)
                for v in sorted({emax + 1, 1000}):
                    def leaf(m, f=f, v=v):
                        c = copy.deepcopy(m)
                        c.ClearField(f.name)
                        if is_repeated(f):
                            return ser(c) + ld(f.number, varint(v))
                        return ser(c) + tag(f.number, WT_VARINT) + varint(v)

                    inj = encode_with(schema, inst, steps, leaf)
                    got = node_at(schema, schema.cls(env_desc).FromString(inj), steps)
                    val = getattr(got, f.name)
                    ok = val is not None and ((list(val) == [v]) if is_repeated(f) else val == v)
                    selfcheck["enum_ok" if ok else "enum_bad"] += 1
                    note = f" The field is in oneof {f.containing_oneof.name}, so setting it displaces that oneof's arm." if f.containing_oneof else ""
                    exp, rule = "not-a-violation", (
                        f"{A['fwd']}: an unknown enum value is a local diagnostic, never a ContractViolation, and the "
                        f"session continues. The spec gives no per-field meaning for an unknown {f.enum_type.name} value, "
                        f"so nothing else is checked.{note}")
                    refused = item_refusal(fam["arm"], f)
                    if refused is not None:
                        exp, rule = "item-refused", refused
                    case(**common, injection=f"unknown-enum:{'max+1' if v == emax + 1 else '1000'}", field=f.name,
                         enum=f.enum_type.full_name, value=v, envelope_b64=b64(inj), expected=exp, rule=rule)

        # (d) id collision, request families only, with a 1.x peer's id forms
        if fam["class"] in ("HOST_REQUEST", "PLUGIN_REQUEST") or fam["arm"] == "grant_state_query":
            request = copy.deepcopy(fam["root"])
            request.id = HOST_ID if fam["envelope"] == "HostMsg" else PLUGIN_ID
            req = ser(request)
            rid = request.id
            for variant, env_name in (("peer", OPPOSITE[fam["envelope"]]), ("same-direction", fam["envelope"])):
                e = envs[env_name]
                env_inst = schema.cls(e)()
                env_inst.id = rid
                inj = env_inst.SerializeToString() + ld(schema.next_number(e), SMALL_SUBMSG)
                if variant == "peer":
                    exp, rule = "session-faulted", (
                        f"{A['one_reply']}. The id names the receiver's own request in flight, and a message of a kind "
                        "the receiver does not know is never that request's reply, so the session is faulted, naming "
                        f"{rid!r}; the request fails with the fault. {A['kind']} applies only under an id that names "
                        "nothing in flight. The receiver keeps the ids of requests it received apart from the ids of "
                        "replies to its own requests, so the fault is never read as a reused id."
                    )
                else:
                    exp, rule = "session-faulted", (
                        f"the sender breaks {A['rule1']}: {A['reuse']}. The receiver holds {rid!r} as a request it "
                        "received, and the unknown kind under it is different content, so the session is faulted, naming "
                        f"{rid!r}."
                    )
                case(family=fam["family"], direction=ENVELOPES[env_name],
                     receiver="plugin" if env_name == "HostMsg" else "host", **{"class": fam["class"]},
                     path=f"{env_name}.<unknown {schema.next_number(e)}>", node_message=e.full_name,
                     injection=f"id-collision:{variant}", field_number=schema.next_number(e), envelope_b64=b64(inj),
                     baseline_b64=None, baseline_source=fam["baseline_source"],
                     request_context={"envelope": fam["envelope"], "direction": fam["direction"], "kind": fam["arm"], "id": rid, "b64": b64(req)},
                     precondition=fam["precondition"], expected=exp, rule=rule)

        # (e) id reuse: a message that deserves a reply, or a reply, sent again
        # under its id -- byte-identical (a re-send), or with field max+1
        # appended to the envelope (different content).  The receiver of a
        # reply holds the request it answers in flight.
        is_request = fam["class"] in ("HOST_REQUEST", "PLUGIN_REQUEST") or fam["arm"] == "grant_state_query"
        is_reply = (fam["class"] == "REPLY" or fam["arm"] == "grant_state"
                    or (fam["envelope"] == "HostMsg" and fam["arm"] in reply_kinds))
        if (is_request or is_reply) and fam["arm"] not in ("hello", "ready"):
            first = copy.deepcopy(fam["root"])
            pre = fam["precondition"] if is_reply else None
            if is_request:
                first.id = HOST_ID if fam["envelope"] == "HostMsg" else PLUGIN_ID
            elif pre is None or not first.id:
                pre = None
            else:
                # The recorded request and its reply, under the requester's 1.x id form.
                first.id = PLUGIN_ID if fam["envelope"] == "HostMsg" else HOST_ID
                asked = schema.cls(envs[pre["envelope"]]).FromString(base64.b64decode(pre["b64"]))
                asked.id = first.id
                pre = dict(pre, id=first.id, b64=b64(ser(asked)), source=pre["source"] + f"; id rewritten to {first.id!r}")
            if is_request or pre is not None:
                once = ser(first)
                other = once + tag(schema.next_number(env_desc), WT_VARINT) + b"\x01"
                held = "a request it received" if is_request else "the reply to its own request"
                for variant, second, exp, rule in (
                    ("identical-resend", once, "deduplicated", (
                        f"{A['reuse']}. The second message is byte-identical to the first, so it is a re-send: "
                        f"dropped, with no second dispatch, nothing sent and no fault.")),
                    ("different-content", other, "session-faulted", (
                        f"{A['reuse']}. The receiver holds {first.id!r} as {held}; the second message under it carries "
                        "field max+1 on the envelope, different bytes, so the session is faulted, naming the id. Bytes "
                        "are compared, so a field the receiver does not know counts.")),
                ):
                    seq = [{"direction": fam["direction"], "kind": fam["arm"], "b64": b64(once)},
                           {"direction": fam["direction"], "kind": fam["arm"], "b64": b64(second)}]
                    case(family=fam["family"], direction=fam["direction"],
                         receiver="plugin" if fam["envelope"] == "HostMsg" else "host", **{"class": fam["class"]},
                         path=f"{fam['family']} twice under {first.id!r}", node_message=fam["message"],
                         injection=f"id-reuse:{variant}", field_number=None if variant == "identical-resend" else schema.next_number(env_desc),
                         envelope_b64=None, baseline_b64=None, sequence=seq, baseline_source=fam["baseline_source"],
                         setup=family_setup.get(fam["family"]), request_context=None, precondition=pre,
                         expected=exp, rule=rule, fault_id=first.id)

    # A StageArm's input and params must match the PlanStage behind it, which
    # a single-message case leaves as it was; the paired cases below give
    # both messages the same new content.
    # Content a receiver does not know takes no part in that match (a plan's
    # identity is its known fields), so an unknown field there is ignored.  An
    # unknown oneof arm there is a kind this build does not know, and refuses
    # the arm exactly as an unknown DType does: not acknowledged, a local
    # diagnostic naming the kind, no
    # ContractViolation, and the plan stays planned for a re-arm.
    for c in cases:
        if c["family"] == "HostMsg.stage_arm" and c["path"].startswith(("HostMsg.stage_arm.input", "HostMsg.stage_arm.params")):
            if c["injection"] == "unknown-oneof-arm":
                c["expected"] = "item-refused"
                c["rule"] = (
                    f"{A['arm_refused']}: an arm whose {c['node_message'].rsplit('.', 1)[-1]}.{c['oneof']} carries an arm "
                    f"(field {c['field_number']}) this build does not know cannot be used, exactly as an arm with an "
                    "unknown dtype: it is not acknowledged and the host sees it by the arrangement's deadline, and the "
                    f"plan stays planned so the host may re-arm with a kind the plugin knows; {A['fwd']}: the refusal is "
                    "a local diagnostic naming the kind, not a ContractViolation, and the session continues"
                )
                continue
            if c["injection"].startswith("unknown-field:"):
                c["expected"] = "ignored"
                c["rule"] = (
                    f"{A['arm_plan']}: the arm must match its PlanStage on stage_id, input and params, compared on the "
                    f"fields the receiver knows; field {c['field_number']} is not one, so it is skipped ({A['fwd']}) and "
                    "the arm is installed and acknowledged as in the baseline"
                )
                continue
            if c["expected"] == "item-refused":
                continue
            c["expected"] = "UNSTATED"
            c["rule"] = (
                f"{A['arm_plan']}: the arm must match its PlanStage on stage_id, input and params, and this case "
                "changes the arm alone. The spec does not say whether content the receiver does not know takes part "
                "in that match; the paired plan-and-arm cases cover the same content on both."
            )

    # ---- the handshake at a later minor.  A newer host serves an older
    # plugin, which answers with its Ready clamped to the host's minor; a
    # Ready above the host's own Hello minor is refused at the handshake.
    for fam_name, expected, rule in (
        ("HostMsg.hello", "session-continues", (
            f"{A['major']}: the match is on the major, and a differing minor does not fail the session; "
            f"{A['minor_serves']}. The plugin proceeds, records the host's (major, minor), answers with a Ready whose "
            "minor is the lower of the host's and its own, runs at that minor, and handles what follows.")),
        ("PluginMsg.ready", "handshake-refused", (
            f"{A['major']}: both ends check the version. {A['ready_ceiling']}: the plugin clamps its Ready to the "
            "lower of the two, and a host that receives a higher one refuses at the handshake with \"this plugin "
            "speaks protocol 1.{ready}; this vizi speaks 1.{hello}\". The host's Hello here is 1.0.")),
    ):
        fam = fam_by_name[fam_name]
        for minor in HANDSHAKE_MINORS:
            later = copy.deepcopy(fam["root"])
            getattr(later, fam["arm"]).protocol_minor = minor
            case(family=fam_name, direction=fam["direction"], receiver="plugin" if fam["envelope"] == "HostMsg" else "host",
                 **{"class": fam["class"]}, path=f"{fam_name}.protocol_minor", node_message=fam["message"],
                 injection=f"protocol-minor:{minor}", field_number=2, value=minor, envelope_b64=b64(ser(later)),
                 baseline_b64=b64(ser(fam["root"])), baseline_source=fam["baseline_source"],
                 request_context=None, precondition=None, expected=expected,
                 rule=rule.replace("{ready}", str(minor)).replace("{hello}", "0"))

    # ---- paired: new content in two messages that must agree
    def with_unknown(msg, steps_names, number_of):
        """`msg` serialized with an unknown varint field appended at the node
        named by `steps_names` (field names from the envelope down)."""
        steps, d = [], hm_desc
        for name in steps_names:
            fd = d.fields_by_name[name]
            steps.append((fd, None))
            d = fd.message_type
        return encode_with(schema, msg, steps, lambda m: ser(m) + tag(number_of(d), WT_VARINT) + b"\x01")

    def paired(family, path, injection, baseline_seq, seq, expected, rule, setup=None, source=""):
        case(family=family, direction="host->plugin", receiver="plugin", **{"class": "ARRANGEMENT"}, path=path,
             node_message=None, injection=injection, envelope_b64=None, baseline_b64=None,
             baseline_sequence=baseline_seq, sequence=seq, setup=setup, baseline_source=source,
             request_context=None, precondition=None, expected=expected, rule=rule)

    nn = schema.next_number
    plan_b = ser(plan_root)
    arm_b = ser(arm_root)
    plan_u = with_unknown(plan_root, ["plan_stage"], nn)
    arm_u = with_unknown(arm_root, ["stage_arm"], nn)
    plan_iu = with_unknown(plan_root, ["plan_stage", "input"], nn)
    arm_iu = with_unknown(arm_root, ["stage_arm", "input"], nn)
    src = f"StageArm {arm_fam['baseline_source']}; the PlanStage restates its stage_id, input and params"
    base_pair = [step(plan_b, "plan_stage"), step(arm_b, "stage_arm")]
    match_rule = f"{A['arm_plan']}: an arm is matched to the answered PlanStage on its stage_id, input and params"
    paired("HostMsg.plan_stage+stage_arm", "PlanStage, then StageArm: field max+1 on both", "paired:plan-and-arm:both",
           base_pair, [step(plan_u, "plan_stage"), step(arm_u, "stage_arm")], "ignored",
           f"{match_rule}, and a field neither end knows is outside all three and is skipped ({A['fwd']}): the arm is installed and acknowledged as in the baseline",
           stage_setup, src)
    paired("HostMsg.plan_stage+stage_arm", "PlanStage.input, then StageArm.input: field max+1 on both",
           "paired:plan-and-arm:both-inputs", base_pair, [step(plan_iu, "plan_stage"), step(arm_iu, "stage_arm")], "ignored",
           f"{match_rule}. Both inputs carry the same new field, so they are the same input whether or not an unknown field counts: the arm is installed and acknowledged as in the baseline",
           stage_setup, src)
    for which, seq in (("arm-input-only", [step(plan_b, "plan_stage"), step(arm_iu, "stage_arm")]),
                       ("plan-input-only", [step(plan_iu, "plan_stage"), step(arm_b, "stage_arm")])):
        paired("HostMsg.plan_stage+stage_arm", f"PlanStage.input, then StageArm.input: field max+1 on the {which.split('-')[0]} only",
               f"paired:plan-and-arm:{which}", base_pair, seq, "ignored",
               f"{match_rule}, compared on the fields the receiver knows: a field it does not know is skipped ({A['fwd']}), "
               "so the inputs match and the arm is installed and acknowledged as in the baseline",
               stage_setup, src)

    # A restatement and the arrangement it restates (§5.5 rule 4).
    for fam in families:
        if fam["class"] != "ARRANGEMENT" or fam["envelope"] != "HostMsg":
            continue
        r = fam["root"]
        rb, ru = ser(r), with_unknown(r, [fam["arm"]], nn)
        prefix = [step(plan_b, "plan_stage")] if fam["arm"] == "stage_arm" else []
        setup = {
            "stage_arm": stage_setup,
            "epoch_change": family_setup["HostMsg.epoch_change"],
            "install_segment": {"capability": {"segment_id": r.install_segment.segment_id}},
        }.get(fam["arm"])
        base_seq = prefix + [step(rb, fam["arm"]), step(rb, fam["arm"])]
        paired(fam["family"], f"{fam['message'].split('.')[-1]} twice: field max+1 on both", "paired:restatement:both",
               base_seq, prefix + [step(ru, fam["arm"]), step(ru, fam["arm"])], "ignored",
               f"{A['reack']}: a re-sent arrangement is acknowledged again and changes nothing. Both statements carry the same new field, so their content is the same whether or not an unknown field counts",
               setup, fam["baseline_source"])
        paired(fam["family"], f"{fam['message'].split('.')[-1]} twice: field max+1 on the restatement only",
               "paired:restatement:restatement-only", base_seq, prefix + [step(rb, fam["arm"]), step(ru, fam["arm"])],
               "ignored",
               f"{A['content']} (arrangement_content_mismatch), where content is the fields the receiver knows: a field it "
               f"does not know is skipped ({A['fwd']}), so the restatement is the same content and {A['reack']}",
               setup, fam["baseline_source"])

    # ---- examples: one new kind, one new field on a common message, new
    # arms inside a request and a push, and a new widget kind in a manifest
    host_cls = schema.cls(envs["HostMsg"])
    new_kind = schema.next_number(envs["HostMsg"])
    for idv in ("", HOST_ID):
        m = host_cls(id=idv)
        exp, rule = kind_outcome("a later kind", idv)
        case(family="EXAMPLE", direction="host->plugin", receiver="plugin", **{"class": None},
             path=f"HostMsg.<new kind {new_kind}>", node_message=f"{PKG}.HostMsg",
             injection="example:new-host-kind:" + ("with-id" if idv else "push"), field_number=new_kind,
             envelope_b64=b64(m.SerializeToString() + ld(new_kind, b"")), baseline_b64=None,
             baseline_source="an empty submessage under the next HostMsg arm number", request_context=None,
             precondition=None, expected=exp, rule=rule)
    inv = fam_by_name["HostMsg.invoke_command"]
    ic = schema.msg(f"{PKG}.InvokeCommand")
    ic_new = schema.next_number(ic)
    steps = [(envs["HostMsg"].fields_by_name["invoke_command"], None)]
    case(family="EXAMPLE", direction="host->plugin", receiver="plugin", **{"class": inv["class"]},
         path=f"HostMsg.invoke_command.<new string field {ic_new}>", node_message=ic.full_name,
         injection="example:new-field-on-common-message", field_number=ic_new,
         envelope_b64=b64(encode_with(schema, inv["root"], steps, lambda m: ser(m) + ld(ic_new, b"gen-widget"))),
         baseline_b64=b64(encode_with(schema, inv["root"], steps, ser)), baseline_source=inv["baseline_source"],
         request_context=None, precondition=None, expected="ignored",
         rule=f"{A['fwd']}: InvokeCommand field {ic_new} is skipped and the command runs as in the baseline")
    for pth, mname in (("HostMsg.invoke_command.args{}.value", "Value"), ("PluginMsg.panel_patch.ops[]", "PatchOp")):
        d = schema.msg(f"{PKG}.{mname}")
        hits = [c for c in cases if c["path"] == pth and c["injection"] == "unknown-oneof-arm"]
        for h in hits[:1]:
            case(**dict(h, family="EXAMPLE", injection=f"example:new-arm:{mname}.{schema.next_number(d)}",
                        path=pth + f"<{schema.next_number(d)}>"))
    mp = "examples/interactive_seg/python/vizi-plugin.json"
    man = json.loads((ROOT / mp).read_text())
    txt = json.dumps(man)
    if '"kind": "slider"' not in txt:
        raise SystemExit(f"{mp} has no slider widget to rename")
    newman = json.loads(txt.replace('"kind": "slider"', '"kind": "range_slider"', 1))
    case(family="EXAMPLE", direction="manifest", receiver="host", **{"class": None},
         path=f"{mp}: first widget kind slider -> range_slider", node_message=None,
         injection="example:new-widget-kind", field_number=None, envelope_b64=None, baseline_b64=None,
         manifest_b64=b64(json.dumps(newman).encode()), manifest_baseline_b64=b64(json.dumps(man).encode()),
         baseline_source=mp, request_context=None, precondition=None, expected="manifest-refused",
         rule=f"{A['widget_kind']}: an unknown widget kind refuses the whole manifest at parse")

    # ---- a reply of the wrong kind under a pending request's id.  Each
    # request family, in flight under its requester's 1.x id form, and each
    # known reply kind of the opposite envelope that the request does not
    # take (Error aside) under that id.  A reply whose baseline names a
    # SegmentRef is left out: what the receiver does with a descriptor it
    # holds unread is not this rule's.
    def names_segment(m) -> bool:
        for f, v in m.ListFields():
            if f.message_type is None:
                continue
            if f.message_type.full_name == f"{PKG}.SegmentRef":
                return True
            items = v.values() if is_map(f) else (v if is_repeated(f) else [v])
            if any(names_segment(x) for x in items if hasattr(x, "ListFields")):
                return True
        return False

    takes = {"HostMsg": {}, "PluginMsg": {}}  # request envelope -> request kind -> reply kinds
    for k, v in request_replies.items():
        takes["HostMsg"].setdefault(k, set()).add(v)
    for k, v in alternate_replies.items():
        takes["HostMsg"].setdefault(k, set()).add(v)
    for k, v in plugin_request_replies.items():
        takes["PluginMsg"].setdefault(k, set()).add(v)
    reply_arms = {"HostMsg": set().union(*takes["HostMsg"].values()), "PluginMsg": set().union(*takes["PluginMsg"].values())}
    for fam in families:
        env = fam["envelope"]
        if fam["class"] not in ("HOST_REQUEST", "PLUGIN_REQUEST") or fam["arm"] not in takes[env]:
            continue
        rid = HOST_ID if env == "HostMsg" else PLUGIN_ID
        request = copy.deepcopy(fam["root"])
        request.id = rid
        own = takes[env][fam["arm"]]
        for wrong in sorted(reply_arms[env] - own):
            w = fam_by_name[f"{OPPOSITE[env]}.{wrong}"]
            reply = copy.deepcopy(w["root"])
            if names_segment(getattr(reply, wrong)):
                continue
            reply.id = rid
            case(family=fam["family"], direction=w["direction"], receiver="plugin" if env == "PluginMsg" else "host",
                 **{"class": fam["class"]}, path=f"{w['family']} under {rid!r}", node_message=w["message"],
                 injection="reply-kind:wrong", field_number=None, envelope_b64=b64(ser(reply)),
                 baseline_b64=None, baseline_source=w["baseline_source"],
                 request_context={"envelope": env, "direction": fam["direction"], "kind": fam["arm"], "id": rid, "b64": b64(ser(request))},
                 precondition=None, expected="session-faulted", fault_id=rid,
                 rule=(f"{A['one_reply']}. {fam['arm']} takes {' or '.join(sorted(own))}; {wrong} is a known reply kind "
                       f"it does not take, so the session is faulted, naming {rid!r}, and the waiter never receives it."))

    # ---- ids, and the summary
    out_cases = []
    ids = set()
    for c in cases:
        key = f"{c['family']}|{c['path']}|{c['injection']}|{c.get('oneof', '')}|{c.get('field', '')}|{c.get('value', '')}"
        cid = "cc-" + hashlib.sha256(key.encode()).hexdigest()[:12]
        if cid in ids:
            raise SystemExit(f"duplicate case id for {key}")
        ids.add(cid)
        out_cases.append({"case_id": cid, **c})

    by = lambda f: dict(sorted(collections.Counter(f(c) for c in out_cases).items()))  # noqa: E731
    fam_rows = collections.defaultdict(collections.Counter)
    for c in out_cases:
        fam_rows[c["family"]][c["injection"].split(":")[0]] += 1
    counts = {
        "recording": {"sdk_and_examples_clean": meta["sdk_and_examples_clean"]},
        "inputs_sha256": {
            **{n: hashlib.sha256(b).hexdigest() for n, b in sorted(pb2_files.items())},
            "control.py class tables": hashlib.sha256(
                json.dumps([host_classes, plugin_classes, request_replies, sorted(reply_kinds)], sort_keys=True).encode()
            ).hexdigest(),
            "reply tables": hashlib.sha256(
                json.dumps([alternate_replies, plugin_request_replies], sort_keys=True).encode()
            ).hexdigest(),
        },
        "total": len(out_cases),
        "families": len(families),
        "by_injection": by(lambda c: c["injection"]),
        "by_expected": by(lambda c: c["expected"]),
        "by_receiver": by(lambda c: c["receiver"]),
        "selfcheck": dict(sorted(selfcheck.items())),
        "class_gaps": class_gaps,
        "anchors": A,
        "per_family": {
            f["family"]: {"class": f["class"], "baseline_source": f["baseline_source"], "cases": dict(sorted(fam_rows[f["family"]].items()))}
            for f in families
        },
    }
    if selfcheck.get("oneof_bad") or selfcheck.get("field_bad") or selfcheck.get("enum_bad"):
        raise SystemExit(f"self-check failed: {dict(selfcheck)}")

    pruned_meta = dict(meta, seen=seen)
    order = {id(r): i for i, r in enumerate(recording)}
    kept_lines = [json.dumps({"meta": pruned_meta}, sort_keys=True)]
    for r in sorted({id(r): r for r in kept}.values(), key=lambda r: order[id(r)]):
        kept_lines.append(json.dumps({k: r[k] for k in ("script", "session", "seq", "direction", "kind", "id", "b64")}))
    return out_cases, kept_lines, counts


def write_outputs(out: Path, cases, counts, kept_lines=None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    raw = "".join(json.dumps(c) + "\n" for c in cases).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(raw)
    (out / "manifest.jsonl.gz").write_bytes(buf.getvalue())
    (out / "counts.json").write_text(json.dumps(counts, indent=1) + "\n")
    if kept_lines is not None:
        (out / "recording.jsonl").write_text("\n".join(kept_lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--no-record", action="store_true", help="regenerate from cases/recording.jsonl (the default)")
    mode.add_argument("--record", action="store_true", help="re-record the baselines first, then regenerate")
    mode.add_argument("--recording", type=Path, help="regenerate from a full recording written by record.py --all")
    mode.add_argument("--check", action="store_true", help="fail when the committed cases differ from a regeneration")
    ap.add_argument("--out", type=Path, default=CASES)
    a = ap.parse_args()

    full = run_recorder() if a.record else a.recording
    source = full if full is not None else CASES / "recording.jsonl"
    records, meta = load_recording(source)
    cases, kept, counts = generate(records, meta)

    if a.check:
        with tempfile.TemporaryDirectory() as tmp:
            write_outputs(Path(tmp), cases, counts)
            stale = []
            for name in ("counts.json",):
                if (Path(tmp) / name).read_bytes() != (CASES / name).read_bytes():
                    stale.append(name)
            fresh = gzip.decompress((Path(tmp) / "manifest.jsonl.gz").read_bytes())
            if fresh != gzip.decompress((CASES / "manifest.jsonl.gz").read_bytes()):
                stale.append("manifest.jsonl.gz")
        if stale:
            print(f"stale: {', '.join(stale)}; regenerate with scripts/control_cases/gen.py", file=sys.stderr)
            return 1
        print(f"cases current: {counts['total']} cases")
        return 0

    write_outputs(a.out, cases, counts, kept if full is not None else None)
    print(f"{counts['total']} cases over {counts['families']} families; "
          f"self-check {counts['selfcheck']}")
    gaps = {k: v for k, v in counts["class_gaps"].items() if v["arms_without_class"] or v["classes_without_arm"]}
    if gaps:
        print(f"class gaps: {gaps}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
