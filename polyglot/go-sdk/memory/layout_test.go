package memory

import (
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

func TestConstantAndOwnedProducerValidationMatrix(t *testing.T) {
	for _, discipline := range []Discipline{Constant, Owned} {
		for _, tc := range []struct {
			producer Producer
			failure  FailureClass
		}{{Host, ""}, {Plugin, ""}, {0, Malformed}, {99, Unsupported}} {
			in := InstallSegment{SegmentID: 17, Purpose: PurposeVolume, Regions: []Region{{Index: 0, Offset: 32, Extent: 64, Discipline: discipline, Producer: tc.producer}}}
			got := validateInstall(in, map[string]bool{PurposeVolume: true})
			if tc.failure != "" {
				if got == nil || got.Class != tc.failure {
					t.Fatalf("discipline=%d producer=%d: %v, want %s", discipline, tc.producer, got, tc.failure)
				}
				continue
			}
			if got != nil {
				t.Fatalf("discipline=%d producer=%d: %v", discipline, tc.producer, got)
			}
			grant := doorbell.Memory{SegmentID: 17, Size: 128, Permissions: doorbell.PluginRead}
			got = validateMapping(in, grant)
			if tc.producer == Plugin {
				if got == nil || got.Class != Malformed {
					t.Fatal("plugin producer still requires a writable mapping", got)
				}
			} else if got != nil {
				t.Fatal(got)
			}
			grant.Permissions |= doorbell.PluginWrite
			if got := validateMapping(in, grant); got != nil {
				t.Fatal(got)
			}
		}
	}
}
