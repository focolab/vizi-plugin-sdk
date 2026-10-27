package memory

import (
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"testing"
)

func TestStagedGrantTable(t *testing.T) {
	valid := func() InstallSegment {
		return InstallSegment{SegmentID: 17, Purpose: PurposeStage, Regions: []Region{{Offset: 64, Extent: 256, Discipline: Staged, Producer: Host}}}
	}
	for _, tc := range []struct {
		name   string
		change func(*InstallSegment)
	}{
		{"subject", func(i *InstallSegment) { i.Subject = "stage" }},
		{"empty", func(i *InstallSegment) { i.Regions = nil }},
		{"513", func(i *InstallSegment) { i.Regions = make([]Region, 513) }},
		{"other purpose", func(i *InstallSegment) { i.Purpose = PurposeVolume }},
		{"other discipline", func(i *InstallSegment) { i.Regions[0].Discipline = Owned }},
		{"producer", func(i *InstallSegment) { i.Regions[0].Producer = Plugin }},
		{"unaligned", func(i *InstallSegment) { i.Regions[0].Offset = 72 }},
		{"short", func(i *InstallSegment) { i.Regions[0].Extent = 255 }},
		{"buffers", func(i *InstallSegment) { i.Regions[0].Buffers = 3 }},
		{"slot bytes", func(i *InstallSegment) { i.Regions[0].SlotBytes = 16 }},
		{"epoch", func(i *InstallSegment) { v := uint64(0); i.Regions[0].EpochOffset = &v }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			i := valid()
			tc.change(&i)
			e := validateInstall(i, map[string]bool{PurposeStage: true, PurposeVolume: true})
			if e == nil || e.Class != Malformed {
				t.Fatal(e)
			}
		})
	}
	if e := validateInstall(valid(), map[string]bool{PurposeStage: true}); sharedAtomics && e != nil || !sharedAtomics && (e == nil || e.Class != Unsupported) {
		t.Fatal(e)
	}
	if e := validateMapping(valid(), doorbell.Memory{Size: 320, Permissions: doorbell.PluginRead}); e == nil || e.Class != Malformed {
		t.Fatal("read-only stage grant", e)
	}
	if e := validateMapping(valid(), doorbell.Memory{Size: 320, Permissions: doorbell.PluginRead | doorbell.PluginWrite}); e != nil {
		t.Fatal(e)
	}
}
