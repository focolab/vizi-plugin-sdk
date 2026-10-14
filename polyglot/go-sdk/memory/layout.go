package memory

import (
	"encoding/binary"
	"hash/crc32"
	"math"
	"sort"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

var le = binary.LittleEndian

func preamble(data []byte, id uint64) ([16]byte, *GrantFailure) {
	var proof [16]byte
	if len(data) < 32 || le.Uint64(data[:8]) != id || le.Uint32(data[8:12]) != 0 ||
		crc32.ChecksumIEEE(data[:28]) != le.Uint32(data[28:32]) {
		return proof, failure(Malformed, "preamble identity, padding or CRC is invalid")
	}
	copy(proof[:], data[12:28])
	return proof, nil
}

// Validate meaning before M arrives too. Mapping-dependent checks are separate.
func validateInstall(in InstallSegment, purposes map[string]bool) *GrantFailure {
	if in.SegmentID == 0 || in.Supersedes == in.SegmentID || in.Purpose == "" {
		return failure(Malformed, "zero identity/purpose or self-supersession")
	}
	if !purposes[in.Purpose] {
		return failure(Unsupported, "unrecognised purpose: "+in.Purpose)
	}
	switch in.Purpose {
	case PurposeOverlay, PurposePanel:
		if in.Subject == "" {
			return failure(Malformed, "O/P segment requires subject")
		}
	case PurposeReader, PurposeVolume, PurposeExport, PurposeStage:
		if in.Subject != "" {
			return failure(Malformed, "G/V/X/S segment forbids subject")
		}
	}
	if in.Purpose == PurposeStage && (len(in.Regions) == 0 || len(in.Regions) > 512) {
		return failure(Malformed, "S requires 1..512 regions")
	}
	seen := make([]bool, len(in.Regions))
	pluginArenas, pluginLanes := 0, 0
	for _, r := range in.Regions {
		if uint64(r.Index) >= uint64(len(seen)) || seen[r.Index] {
			return failure(Malformed, "region indices must be unique and dense")
		}
		seen[r.Index] = true
		if r.Offset < 32 || r.Extent == 0 || r.Extent > math.MaxUint64-r.Offset {
			return failure(Malformed, "region overlaps preamble, is empty or overflows")
		}
		if r.Discipline == 0 || r.Producer == 0 {
			return failure(Malformed, "missing discipline or producer")
		}
		if r.Discipline == Staged && r.Producer != Host {
			return failure(Malformed, "STAGED producer must be HOST")
		}
		if r.Producer != Host && r.Producer != Plugin {
			return failure(Unsupported, "unknown producer")
		}
		if (in.Purpose == PurposeStage) != (r.Discipline == Staged) {
			return failure(Malformed, "S requires only STAGED regions; STAGED requires S")
		}
		switch r.Discipline {
		case Staged:
			if r.Producer != Host || r.Offset%64 != 0 || r.Extent < 256 || r.Buffers != 0 || r.SlotBytes != 0 || r.EpochOffset != nil {
				return failure(Malformed, "invalid STAGED region table")
			}
			if !sharedAtomics {
				return failure(Unsupported, "STAGED requires process-shared atomics")
			}
		case Constant, Owned:
			// §8's receiver matrix accepts either known producer. §3.1's
			// HOST rule binds senders; mapping permissions are checked below.
		case Pulsed:
			if r.Producer == Plugin {
				pluginLanes++
				if pluginLanes > 1 {
					return failure(Malformed, "multiple plugin-produced Pulsed lanes")
				}
			}
			if r.EpochOffset != nil && (r.SlotBytes <= 8 || *r.EpochOffset != r.SlotBytes-8) {
				return failure(Malformed, "Pulsed epoch header must occupy final eight slot bytes")
			}
			n := uint64(r.Buffers)
			if r.Offset%8 != 0 || n < 3 || r.SlotBytes == 0 || r.SlotBytes > math.MaxUint64/n-16 || r.Extent != n*(16+r.SlotBytes) {
				return failure(Malformed, "invalid Pulsed alignment, buffer count or extent")
			}
			if !sharedAtomics {
				return failure(Unsupported, "Pulsed requires a supported process-shared atomic backend")
			}
		case Loaned:
			// §8's receiver-validation matrix ignores SlotBytes at installation.
			// All allocators pad to 64 bytes regardless of this reserved field.
			if r.Producer == Plugin {
				pluginArenas++
				if pluginArenas > 1 {
					return failure(Malformed, "multiple plugin-produced Loaned arenas")
				}
			}
		default:
			return failure(Unsupported, "unknown discipline")
		}
		if r.Discipline != Pulsed && r.Buffers != 0 {
			return failure(Malformed, "buffers must be zero outside Pulsed")
		}
		if r.EpochOffset != nil {
			bound := r.Extent
			if r.Discipline == Pulsed {
				bound = r.SlotBytes
			}
			if !inRange(*r.EpochOffset, 8, bound) {
				return failure(Malformed, "epoch header exceeds slot or region")
			}
		}
	}
	ordered := append([]Region(nil), in.Regions...)
	sort.Slice(ordered, func(i, j int) bool { return ordered[i].Offset < ordered[j].Offset })
	for i := 1; i < len(ordered); i++ {
		if ordered[i-1].Offset+ordered[i-1].Extent > ordered[i].Offset {
			return failure(Malformed, "regions overlap")
		}
	}
	return nil
}

func validateMapping(in InstallSegment, grant doorbell.Memory) *GrantFailure {
	for _, r := range in.Regions {
		if r.Offset > grant.Size || r.Extent > grant.Size-r.Offset {
			return failure(Malformed, "region exceeds declared mapping size")
		}
		if (r.Producer == Plugin || r.Discipline == Pulsed || r.Discipline == Staged) && grant.Permissions&doorbell.PluginWrite == 0 {
			return failure(Malformed, "plugin writes or Pulsed claims require PLUGIN_WRITE")
		}
	}
	return nil
}
