package payload

import (
	"errors"
	"math"
	"slices"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// CopyExportChunk validates a full spatial slab against its OpenExport and
// returns owned [channel-in-chunk,Z,Y,X] bytes in the declared byte order.
// Channels are source indices in chunk.Channels order; OpenExport's C is the
// artifact channel count. T is also a source index; its artifact index is its
// rank among the export's distinct source timepoints (sent in ascending order).
// Use inside the Export.Chunk handler: control releases its descriptor use when
// that handler returns. No release or ownership handoff is invented here.
// This array-only API carries no provenance; inspect chunk.FilteredChannels
// with opened.Filter, or use CopyExportVolumes for per-channel Volume.Filter.
func CopyExportChunk(memory Copier, opened *pb.OpenExport, chunk *pb.ExportChunk, maxBytes uint64) (*Tensor, error) {
	if opened == nil || chunk == nil || len(opened.ShapeTczyx) != 5 {
		return nil, errors.New("export requires whole-artifact TCZYX shape")
	}
	if _, err := ByteLen(opened.ShapeTczyx, opened.Dtype); err != nil {
		return nil, err
	}
	if len(chunk.Channels) == 0 || uint64(len(chunk.Channels)) > uint64(opened.ShapeTczyx[1]) {
		return nil, errors.New("invalid export channel count")
	}
	shape := []uint32{uint32(len(chunk.Channels)), opened.ShapeTczyx[2], opened.ShapeTczyx[3], opened.ShapeTczyx[4]}
	n, err := ByteLen(shape, opened.Dtype)
	if err != nil {
		return nil, err
	}
	if maxBytes == 0 || n > maxBytes || n > uint64(int(^uint(0)>>1)) {
		return nil, errors.New("export chunk exceeds application copy budget")
	}
	var data []byte
	switch source := chunk.Source.(type) {
	case *pb.ExportChunk_Inline:
		if source == nil {
			return nil, errors.New("nil inline export source")
		}
		if chunk.Install != nil {
			return nil, errors.New("inline export cannot carry an installation")
		}
		if uint64(len(source.Inline)) != n || len(source.Inline) > control.MaxInlineBytes {
			return nil, errors.New("inline export length disagrees with geometry or exceeds 8 MiB")
		}
		data = append([]byte(nil), source.Inline...)
	case *pb.ExportChunk_Segment:
		if source == nil {
			return nil, errors.New("nil export segment source")
		}
		d := source.Segment
		if d == nil || d.SegmentId == 0 || d.Dtype != opened.Dtype || !slices.Equal(d.ShapeZyx, opened.ShapeTczyx[2:]) || d.Len != n || d.Offset > math.MaxUint64-d.Len {
			return nil, errors.New("export descriptor disagrees with declared chunk geometry")
		}
		if chunk.Install != nil && chunk.Install.SegmentId != d.SegmentId {
			return nil, errors.New("export installation names another segment")
		}
		if memory == nil {
			return nil, errors.New("export descriptor copier is nil")
		}
		data = make([]byte, int(n))
		if err := memory.CopyDescriptor(d, data); err != nil {
			return nil, err
		}
	default:
		return nil, errors.New("export chunk has no source")
	}
	return &Tensor{data, shape, opened.Dtype, opened.LittleEndian}, nil
}

// CopyExportVolumes copies a chunk once and returns one owned ZYX volume per
// chunk.Channels entry, in that order. T/C retain source indices. Level and
// OpenGeneration are zero (the export does not supply a dataset generation).
// Filter is present only for channels named by this chunk's FilteredChannels;
// an empty list means all volumes are raw, even when opened.Filter is present.
// Each nonnil filter is an independent snapshot; tensor storage is shared
// within the copied slab and immutable through Tensor's public API.
// Descriptor lifetime is the same as CopyExportChunk: call inside Chunk.
func CopyExportVolumes(memory Copier, opened *pb.OpenExport, chunk *pb.ExportChunk, maxBytes uint64) ([]*Volume, error) {
	if opened == nil || chunk == nil {
		return nil, errors.New("export open and chunk are required")
	}
	filtered := make(map[uint32]bool, len(chunk.FilteredChannels))
	channels := make(map[uint32]bool, len(chunk.Channels))
	for _, channel := range chunk.Channels {
		channels[channel] = true
	}
	for _, channel := range chunk.FilteredChannels {
		if opened.Filter == nil || !channels[channel] {
			return nil, errors.New("filtered channel requires an export filter and membership in this chunk")
		}
		filtered[channel] = true
	}
	slab, err := CopyExportChunk(memory, opened, chunk, maxBytes)
	if err != nil {
		return nil, err
	}
	volumes := make([]*Volume, len(chunk.Channels))
	stride := len(slab.data) / len(chunk.Channels)
	for i, channel := range chunk.Channels {
		end := (i + 1) * stride
		v := &Volume{T: chunk.T, C: channel, Tensor: &Tensor{
			data: slab.data[i*stride : end : end], shape: slices.Clone(slab.shape[1:]), dtype: slab.dtype, little: slab.little,
		}}
		if filtered[channel] {
			v.Filter = cloneFilter(opened.Filter)
		}
		volumes[i] = v
	}
	return volumes, nil
}
