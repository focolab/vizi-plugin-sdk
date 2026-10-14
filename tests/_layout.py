"""Where the SDK's tests find things in this tree.

Written by vizi's bin/sync-sdk.sh, one per tree from one list.  The tests read
the IDL, one package source file and the specification through the names
here rather than through a repo layout, so the same test file runs in the
vizi-plugin-sdk repository and in vizi's tree.  Do not edit.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTO = ROOT / "proto" / "vizi_plugin.proto"
PACKAGE = ROOT / "vizi_plugin_sdk"
SPEC_DOCS = [
    ROOT / "docs" / "spec" / "plugin_principles.md",
    ROOT / "docs" / "spec" / "control_plane_principles.md",
    ROOT / "docs" / "spec" / "control_plane_protocol.md",
    ROOT / "docs" / "spec" / "control_plane_implementation_details.md",
    ROOT / "docs" / "spec" / "doorbell_principles.md",
    ROOT / "docs" / "spec" / "doorbell_protocol.md",
    ROOT / "docs" / "spec" / "doorbell_M_memory.md",
    ROOT / "docs" / "spec" / "doorbell_E_input.md",
    ROOT / "docs" / "spec" / "doorbell_D_notification.md",
    ROOT / "docs" / "spec" / "doorbell_implementation_details.md",
    ROOT / "docs" / "spec" / "shared_memory_principles.md",
    ROOT / "docs" / "spec" / "shared_memory_protocol.md",
    ROOT / "docs" / "spec" / "shared_memory_implementation_details.md",
    ROOT / "docs" / "spec" / "manifest_schema.md",
    ROOT / "docs" / "spec" / "reader_writer_contract.md",
    ROOT / "docs" / "spec" / "processing_stages.md",
]
