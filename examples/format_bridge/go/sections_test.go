package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"hash/crc32"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// Construct the published layout directly, without using the crate writer.
func sectionFixture(t *testing.T, names []byte, flags uint32) string {
	t.Helper()
	form, text, record := []byte("ome-xml"), []byte("<source/>"), []byte(`{"source":"test"}`)
	h := make([]byte, 96)
	copy(h, "VIZCRATE")
	binary.LittleEndian.PutUint16(h[8:10], 1)
	binary.LittleEndian.PutUint16(h[10:12], 1)
	binary.LittleEndian.PutUint32(h[12:16], flags)
	for i, n := range []uint32{1, 3, 1, 1, 1} {
		binary.LittleEndian.PutUint32(h[16+i*4:], n)
	}
	binary.LittleEndian.PutUint16(h[36:38], uint16(len(form)))
	binary.LittleEndian.PutUint32(h[38:42], uint32(len(text)))
	binary.LittleEndian.PutUint32(h[80:84], uint32(len(record)))
	binary.LittleEndian.PutUint32(h[84:88], uint32(len(names)))
	// Reserved bytes must still be ignored, including reserved flag bits.
	h[88] = 255
	data := append(h, form...)
	data = append(data, text...)
	data = append(data, record...)
	data = append(data, names...)
	start := len(data)
	data = append(data, 11, 22, 33)
	index := make([]byte, 72)
	for i, v := range []byte{11, 22, 33} {
		p := index[24*i:]
		binary.LittleEndian.PutUint64(p, uint64(start+i))
		binary.LittleEndian.PutUint64(p[8:], 1)
		binary.LittleEndian.PutUint32(p[16:], crc32.ChecksumIEEE([]byte{v}))
	}
	footer := make([]byte, 32)
	binary.LittleEndian.PutUint64(footer, uint64(len(data)))
	binary.LittleEndian.PutUint64(footer[8:], 3)
	binary.LittleEndian.PutUint32(footer[16:], crc32.ChecksumIEEE(index))
	copy(footer[24:], "ETARCZIV")
	data = append(data, index...)
	data = append(data, footer...)
	path := filepath.Join(t.TempDir(), "sections.crate")
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestSectionsFromHandBuiltBytes(t *testing.T) {
	for _, tc := range []struct {
		name      string
		raw       []byte
		want      []string
		malformed bool
	}{
		{"none", nil, nil, false},
		{"short", []byte("red"), []string{"red"}, false},
		{"unnamed", []byte("\x00green"), []string{"", "green"}, false},
		{"all", []byte("red\x00\x00blue"), []string{"red", "", "blue"}, false},
		{"too many", []byte("a\x00b\x00c\x00d"), nil, true},
		{"invalid UTF8", []byte{255}, nil, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := sectionFixture(t, tc.raw, 2|0x80)
			a := newApplication()
			close(a.ready)
			opened, err := a.openReader(context.Background(), nil, &pb.OpenReader{Uri: path})
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { a.reader.Close() })
			if !reflect.DeepEqual(opened.ChannelNames, tc.want) || a.reader.Header.Provenance != `{"source":"test"}` {
				t.Fatal(opened, a.reader.Header)
			}
			if tc.malformed && (len(a.reader.Notes) != 1 || strings.Count(a.status, a.reader.Notes[0]) != 1) {
				t.Fatal(a.status, a.reader.Notes)
			}
			for ch, v := range []byte{11, 22, 33} {
				b, err := a.reader.Volume(0, uint32(ch), true)
				if err != nil || !bytes.Equal(b, []byte{v}) {
					t.Fatal(ch, b, err)
				}
			}
			meta, err := a.metadata(context.Background(), nil, &pb.Metadata{Handle: opened.Handle})
			if err != nil || meta.Form != "" || meta.Text != "" {
				t.Fatal(meta, err)
			}
		})
	}
	path := sectionFixture(t, []byte("red"), 0)
	a := newApplication()
	close(a.ready)
	opened, err := a.openReader(context.Background(), nil, &pb.OpenReader{Uri: path})
	if err != nil {
		t.Fatal(err)
	}
	defer a.reader.Close()
	meta, err := a.metadata(context.Background(), nil, &pb.Metadata{Handle: opened.Handle})
	if err != nil || meta.Form != "ome-xml" || meta.Text != "<source/>" {
		t.Fatal(meta, err)
	}
}

func TestWriterSectionsAndRecreate(t *testing.T) {
	path := sectionFixture(t, []byte("red\x00\x00blue"), 2|1|0x80)
	output := filepath.Join(t.TempDir(), "recreated.crate")
	if err := recreateCrate(path, output, "ascending", calibration{}); err != nil {
		t.Fatal(err)
	}
	c := mustOpen(t, output, nil)
	if c.Header.Flags != 2 || c.Header.Provenance != `{"source":"test"}` || !reflect.DeepEqual(c.Header.Names, []string{"red", "", "blue"}) {
		t.Fatal(c.Header)
	}
	b, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	if binary.LittleEndian.Uint32(b[80:84]) != 17 || binary.LittleEndian.Uint32(b[84:88]) != 9 || !bytes.Equal(b[88:96], make([]byte, 8)) {
		t.Fatal(b[:96])
	}
	wantOffset := uint64(96 + len("ome-xml") + len("<source/>") + len(`{"source":"test"}`) + len("red\x00\x00blue"))
	if c.Index[0].Offset != wantOffset {
		t.Fatal(c.Index[0], wantOffset)
	}
	for ch, v := range []byte{11, 22, 33} {
		b, err := c.Volume(0, uint32(ch), true)
		if err != nil || !bytes.Equal(b, []byte{v}) {
			t.Fatal(b, err)
		}
	}
}

func TestExportKeepsSourceAndOnlyDeclaredNames(t *testing.T) {
	a := newApplication()
	close(a.ready)
	path := filepath.Join(t.TempDir(), "export.crate")
	source := `<Pixels PhysicalSizeZ="99"/>`
	record := `{ "source": {"uri":"x"}, "selection":[5,9] }`
	q := &pb.OpenExport{ExporterId: "crate", Locator: path, ShapeTczyx: []uint32{1, 7, 1, 1, 1}, Dtype: pb.DType_DTYPE_U8, LittleEndian: true, SourceMetadata: &pb.MetadataDoc{Form: "ome-xml", Text: source}, Provenance: record, Channels: []*pb.ChannelInfo{
		{Index: 5, Name: "red", Source: 1}, {Index: 9, Name: "Channel 10", Source: 2}, {Index: 11, Name: "green", Source: 3}, {Index: 12, Name: "bad\x00name", Source: 1}, {Index: 13, Name: "blue", Source: 1}, {Index: 14, Name: "unspecified"}, {Index: 15, Name: "future", Source: 999},
	}}
	opened, err := a.openExport(context.Background(), nil, q)
	if err != nil {
		t.Fatal(err)
	}
	w := a.exports[opened.Handle].writer
	t.Cleanup(func() { w.Abort() })
	for i := uint32(0); i < 7; i++ {
		if err := w.Write(0, i, []byte{byte(i)}, true); err != nil {
			t.Fatal(err)
		}
	}
	if _, err = a.finishExport(context.Background(), nil, &pb.FinishExport{Handle: opened.Handle}); err != nil {
		t.Fatal(err)
	}
	c := mustOpen(t, path, nil)
	if c.Header.Flags != 2 || c.Header.Form != "ome-xml" || c.Header.Text != source || c.Header.Provenance != record || c.Header.Spacing != [4]float64{} || !reflect.DeepEqual(c.Header.Names, []string{"red", "", "green", "", "blue"}) {
		t.Fatal(c.Header)
	}
}

func TestComparatorNamesAndProvenance(t *testing.T) {
	write := func(name string, flags uint32, record string) string {
		t.Helper()
		path := filepath.Join(t.TempDir(), "compare.crate")
		w, err := newWriter(path, header{DType: 1, Shape: [5]uint32{1, 1, 1, 1, 1}, Names: []string{name}, Flags: flags, Provenance: record})
		if err != nil {
			t.Fatal(err)
		}
		if err = w.Write(0, 0, []byte{42}, true); err != nil {
			t.Fatal(err)
		}
		if err = w.Finish(); err != nil {
			t.Fatal(err)
		}
		return path
	}
	first := write("red", 0, "")
	v, err := compareCrates(first, write("green", 0, ""), nil)
	if err != nil || v.Tier != "voxel-equal" || !strings.Contains(v.String(), "channel names") {
		t.Fatal(v, err)
	}
	v, err = compareCrates(first, write("red", 2, `{"timestamp":1}`), nil)
	if err != nil || v.Tier != "semantically equal" || !strings.Contains(v.String(), "DERIVED") || !strings.Contains(v.String(), "provenance") {
		t.Fatal(v, err)
	}
}
