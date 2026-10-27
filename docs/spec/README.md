# The vizi plugin platform specification

These documents are the normative specification of the vizi plugin protocol;
changes to them are proposed as issues on this repository.

The `.proto` beside this directory settles the encoding. These documents settle
the behaviour, and the difference is the reason they exist: **two
implementations can both satisfy the IDL and still fail to interoperate.**

Three channels, each with the same three documents — the principles it is
consistent with, the normative specification, and what vizi ships plus how
that differs.

| | Principles | Specification | What ships |
|---|---|---|---|
| **Control plane** (gRPC) | `control_plane_principles.md` | `control_plane_protocol.md` | `control_plane_implementation_details.md` |
| **Doorbell** (fixed 112-byte records) | `doorbell_principles.md` | `doorbell_protocol.md` | `doorbell_implementation_details.md` |
| **Shared memory** | `shared_memory_principles.md` | `shared_memory_protocol.md` | `shared_memory_implementation_details.md` |

The doorbell's three opcodes are specified per-op: `doorbell_M_memory.md`
(memory grants), `doorbell_E_input.md` (input) and
`doorbell_D_notification.md` (notification).

`plugin_principles.md` is the parent set all three inherit — delivery, ordering,
bounds, recovery, truthfulness, and how much machinery a failure deserves. Read
it first; everything else assumes it.

**`manifest_schema.md` is the fourth document, not a channel**: what a host
accepts as `vizi-plugin.json`, field by field, with the accepted and rejected
fixtures each SDK's suite executes against its parser. It is what an SDK
certifies a manifest against, and it names the fields the channels size
themselves from.

## If you are writing an SDK

Start with the "what binds a plugin" section in each specification — they say
which parts of a symmetric protocol are yours, so you do not build host
machinery against a channel that will never ask for it:

- `control_plane_protocol.md` §11a
- `doorbell_protocol.md` §11a
- `shared_memory_protocol.md` §9a, which is the whole memory lifecycle in order

Then read the **implementation-details** document for each layer. It is not
background: it records where the shipping host differs from the specification,
and an SDK written against the specification alone will meet those differences
without warning.

## Versions

`protocol_major.protocol_minor` versions the whole boundary — this channel's
messages, the doorbell's record format and the shared-memory protocols. **One
version implies exactly one of each**, and the handshake matches on the
MAJOR: a major bump breaks and a minor bump does not
(`control_plane_protocol.md` §3.2). The protocol is `1.0`, and protocol `1.0`
hosts are vizi v0.9.5 and later.

Where a document opens with a status note, it says what is implemented and what
is not. Read it before assuming a section describes running code.
