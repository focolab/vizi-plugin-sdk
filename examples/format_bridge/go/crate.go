package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"hash/crc32"
	"io"
	"math"
	"os"
	"slices"
	"strings"
	"unicode/utf8"
)

var le = binary.LittleEndian

type header struct {
	DType      uint16
	Flags      uint32
	Shape      [5]uint32
	Form, Text string
	Provenance string
	Names      []string
	Spacing    [4]float64 // Z, Y, X in micrometres, then time in seconds.
}
type indexEntry struct {
	Offset, Length uint64
	CRC            uint32
}
type calibration map[uint32]int32
type crate struct {
	file        *os.File
	Path        string
	Header      header
	Index       []indexEntry
	Calibration calibration
	Notes       []string
}

func itemSize(dtype uint16) (uint64, error) {
	switch dtype {
	case 1:
		return 1, nil
	case 2:
		return 2, nil
	case 3:
		return 4, nil
	}
	return 0, fmt.Errorf("unsupported dtype %d", dtype)
}
func (h header) blockSize() (uint64, error) {
	n, err := itemSize(h.DType)
	if err != nil {
		return 0, err
	}
	for i, v := range h.Shape {
		if v == 0 {
			return 0, fmt.Errorf("zero %s axis", []string{"T", "C", "Z", "Y", "X"}[i])
		}
		if i >= 2 {
			if n > math.MaxInt64/uint64(v) {
				return 0, errors.New("volume size overflow")
			}
			n *= uint64(v)
		}
	}
	return n, nil
}
func known(v float64) float64 {
	if v <= 0 || math.IsNaN(v) || math.IsInf(v, 0) {
		return 0
	}
	return v
}
func readAt(f *os.File, off uint64, n uint64) ([]byte, error) {
	if off > math.MaxInt64 || n > uint64(int(^uint(0)>>1)) {
		return nil, errors.New("file range exceeds address space")
	}
	b := make([]byte, int(n))
	_, err := f.ReadAt(b, int64(off))
	return b, err
}
func loadCalibration(path string) (calibration, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if len(b) < 16 || string(b[:8]) != "VIZCAL01" {
		return nil, fmt.Errorf("%s: invalid calibration magic", path)
	}
	n := uint64(le.Uint32(b[8:12]))
	if 16+8*n != uint64(len(b)) {
		return nil, fmt.Errorf("%s: calibration entry count disagrees with file length", path)
	}
	cal := make(calibration)
	for i := uint64(0); i < n; i++ {
		p := b[16+8*i:]
		cal[le.Uint32(p)] = int32(le.Uint32(p[4:]))
	}
	return cal, nil
}
func openCrate(path string, cal calibration) (_ *crate, result error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer func() {
		if result != nil {
			f.Close()
			result = fmt.Errorf("%s: %w", path, result)
		}
	}()
	st, err := f.Stat()
	if err != nil {
		return nil, err
	}
	b, err := readAt(f, 0, 96)
	if err != nil {
		return nil, fmt.Errorf("not a crate: short header: %w", err)
	}
	if string(b[:8]) != "VIZCRATE" || le.Uint16(b[8:]) != 1 {
		return nil, errors.New("not a crate: magic or format_version")
	}
	h := header{DType: le.Uint16(b[10:]), Flags: le.Uint32(b[12:])}
	for i := range h.Shape {
		h.Shape[i] = le.Uint32(b[16+4*i:])
	}
	block, err := h.blockSize()
	if err != nil {
		return nil, err
	}
	cr := &crate{file: f, Path: path, Header: h, Calibration: cal}
	for i := range h.Spacing {
		v := math.Float64frombits(le.Uint64(b[48+8*i:]))
		cr.Header.Spacing[i] = known(v)
		if v != 0 && known(v) == 0 {
			cr.Notes = append(cr.Notes, fmt.Sprintf("invalid calibration value %d treated as unknown", i))
		}
	}
	formLen, textLen := uint64(le.Uint16(b[36:])), uint64(le.Uint32(b[38:]))
	provenanceLen, namesLen := uint64(le.Uint32(b[80:])), uint64(le.Uint32(b[84:]))
	start := 96 + formLen + textLen + provenanceLen + namesLen
	if st.Size() < 128 {
		return nil, errors.New("unfinished crate — an export of this file did not complete")
	}
	foot, err := readAt(f, uint64(st.Size()-32), 32)
	if err != nil {
		return nil, err
	}
	if string(foot[24:]) != "ETARCZIV" {
		return nil, errors.New("unfinished crate — an export of this file did not complete")
	}
	indexOff, count := le.Uint64(foot), le.Uint64(foot[8:])
	want := uint64(h.Shape[0]) * uint64(h.Shape[1])
	end := uint64(st.Size() - 32)
	if count != want || indexOff < start || indexOff > end || count > (end-indexOff)/24 || count*24 != end-indexOff {
		return nil, errors.New("index inconsistent with header or footer")
	}
	ib, err := readAt(f, indexOff, count*24)
	if err != nil {
		return nil, err
	}
	if crc32.ChecksumIEEE(ib) != le.Uint32(foot[16:]) {
		return nil, errors.New("index CRC mismatch")
	}
	cr.Index = make([]indexEntry, int(count))
	for i := range cr.Index {
		p := ib[24*i:]
		e := indexEntry{le.Uint64(p), le.Uint64(p[8:]), le.Uint32(p[16:])}
		if e.Length != block || e.Offset < start || e.Offset > indexOff || e.Length > indexOff-e.Offset {
			return nil, fmt.Errorf("index inconsistent at (t=%d, c=%d)", i/int(h.Shape[1]), i%int(h.Shape[1]))
		}
		cr.Index[i] = e
	}
	meta, err := readAt(f, 96, formLen+textLen)
	if err != nil {
		return nil, err
	}
	if utf8.Valid(meta[:formLen]) && utf8.Valid(meta[formLen:]) {
		cr.Header.Form = string(meta[:formLen])
		cr.Header.Text = string(meta[formLen:])
	} else {
		cr.Notes = append(cr.Notes, "invalid UTF-8 metadata; carrying no document")
	}
	sections, err := readAt(f, 96+formLen+textLen, provenanceLen+namesLen)
	if err != nil {
		return nil, err
	}
	cr.Header.Provenance = string(sections[:provenanceLen])
	names := sections[provenanceLen:]
	if len(names) > 0 {
		if !utf8.Valid(names) {
			cr.Notes = append(cr.Notes, "invalid UTF-8 channel names; carrying no names")
		} else if parsed := strings.Split(string(names), "\x00"); uint64(len(parsed)) > uint64(h.Shape[1]) {
			cr.Notes = append(cr.Notes, "more channel names than channels; carrying no names")
		} else {
			cr.Header.Names = parsed
		}
	}
	if h.Flags&1 != 0 && cal == nil {
		return nil, errors.New("calibration unavailable; import vizi-crate-cal.bin in Plugin Manager")
	}
	return cr, nil
}
func (c *crate) Close() error {
	if c.file == nil {
		return nil
	}
	err := c.file.Close()
	c.file = nil
	return err
}
func (c *crate) entry(t, ch uint32) (indexEntry, error) {
	if c.file == nil {
		return indexEntry{}, fmt.Errorf("%s: closed reader", c.Path)
	}
	if t >= c.Header.Shape[0] || ch >= c.Header.Shape[1] {
		return indexEntry{}, fmt.Errorf("%s: (t=%d, c=%d) out of bounds", c.Path, t, ch)
	}
	return c.Index[uint64(t)*uint64(c.Header.Shape[1])+uint64(ch)], nil
}
func (c *crate) decode(b []byte, ch uint32) {
	if c.Header.Flags&1 == 0 {
		return
	}
	off := int64(c.Calibration[ch])
	switch c.Header.DType {
	case 1:
		for i, v := range b {
			n := int64(v) - off
			if n < 0 {
				n = 0
			}
			b[i] = byte(n)
		}
	case 2:
		for i := 0; i < len(b); i += 2 {
			n := int64(le.Uint16(b[i:])) - off
			if n < 0 {
				n = 0
			}
			le.PutUint16(b[i:], uint16(n))
		}
	case 3:
		for i := 0; i < len(b); i += 4 {
			v := math.Float32frombits(le.Uint32(b[i:]))
			le.PutUint32(b[i:], math.Float32bits(v-float32(off)))
		}
	}
}
func (c *crate) Volume(t, ch uint32, verify bool) ([]byte, error) {
	e, err := c.entry(t, ch)
	if err != nil {
		return nil, err
	}
	b, err := readAt(c.file, e.Offset, e.Length)
	if err == nil && verify && crc32.ChecksumIEEE(b) != e.CRC {
		err = errors.New("CRC mismatch")
	}
	if err != nil {
		return nil, fmt.Errorf("%s: (t=%d, c=%d): %w", c.Path, t, ch, err)
	}
	c.decode(b, ch)
	return b, nil
}
func (c *crate) Plane(t, z, ch uint32) ([]byte, error) {
	e, err := c.entry(t, ch)
	if err != nil {
		return nil, err
	}
	if z >= c.Header.Shape[2] {
		return nil, fmt.Errorf("%s: (t=%d, c=%d) z=%d out of bounds", c.Path, t, ch, z)
	}
	n := e.Length / uint64(c.Header.Shape[2])
	b, err := readAt(c.file, e.Offset+uint64(z)*n, n)
	if err != nil {
		return nil, fmt.Errorf("%s: (t=%d, c=%d) z=%d: %w", c.Path, t, ch, z, err)
	}
	c.decode(b, ch)
	return b, nil
}

type crateWriter struct {
	file   *os.File
	Path   string
	Header header
	Index  []indexEntry
}

func newWriter(path string, h header) (*crateWriter, error) {
	if _, err := h.blockSize(); err != nil {
		return nil, err
	}
	if len(h.Form) > math.MaxUint16 || uint64(len(h.Text)) > math.MaxUint32 || !utf8.ValidString(h.Form) || !utf8.ValidString(h.Text) {
		return nil, errors.New("invalid metadata lengths or UTF-8")
	}
	h.Names = slices.Clone(h.Names)
	for i, name := range h.Names {
		if strings.ContainsRune(name, 0) {
			h.Names[i] = ""
		}
	}
	for len(h.Names) > 0 && h.Names[len(h.Names)-1] == "" {
		h.Names = h.Names[:len(h.Names)-1]
	}
	names := strings.Join(h.Names, "\x00")
	if uint64(len(h.Names)) > uint64(h.Shape[1]) || !utf8.ValidString(names) || uint64(len(names)) > math.MaxUint32 || uint64(len(h.Provenance)) > math.MaxUint32 {
		return nil, errors.New("invalid provenance or channel names length or UTF-8")
	}
	n := uint64(h.Shape[0]) * uint64(h.Shape[1])
	if n > uint64(int(^uint(0)>>1))/24 {
		return nil, errors.New("index exceeds address space")
	}
	f, err := os.Create(path)
	if err != nil {
		return nil, err
	}
	h.Flags &= 2 // Preserve DERIVED; decoded output is never CALIBRATED.
	w := &crateWriter{file: f, Path: path, Header: h, Index: make([]indexEntry, int(n))}
	b := make([]byte, 96)
	copy(b, "VIZCRATE")
	le.PutUint16(b[8:], 1)
	le.PutUint16(b[10:], h.DType)
	le.PutUint32(b[12:], h.Flags)
	for i, v := range h.Shape {
		le.PutUint32(b[16+4*i:], v)
	}
	le.PutUint16(b[36:], uint16(len(h.Form)))
	le.PutUint32(b[38:], uint32(len(h.Text)))
	le.PutUint32(b[80:], uint32(len(h.Provenance)))
	le.PutUint32(b[84:], uint32(len(names)))
	for i, v := range h.Spacing {
		w.Header.Spacing[i] = known(v)
		le.PutUint64(b[48+8*i:], math.Float64bits(known(v)))
	}
	for _, p := range [][]byte{b, []byte(h.Form), []byte(h.Text), []byte(h.Provenance), []byte(names)} {
		if _, err = f.Write(p); err != nil {
			return nil, errors.Join(err, w.Abort())
		}
	}
	return w, nil
}
func (w *crateWriter) Write(t, ch uint32, b []byte, little bool) error {
	if w.file == nil {
		return errors.New("export is closed")
	}
	if t >= w.Header.Shape[0] || ch >= w.Header.Shape[1] {
		return fmt.Errorf("%s: (t=%d, c=%d) out of bounds", w.Path, t, ch)
	}
	n, _ := w.Header.blockSize()
	if uint64(len(b)) != n {
		return fmt.Errorf("%s: (t=%d, c=%d) payload length %d, want %d", w.Path, t, ch, len(b), n)
	}
	size, _ := itemSize(w.Header.DType)
	if !little && size > 1 {
		b = append([]byte(nil), b...)
		for i := 0; i < len(b); i += int(size) {
			for a, z := i, i+int(size)-1; a < z; a, z = a+1, z-1 {
				b[a], b[z] = b[z], b[a]
			}
		}
	}
	off, err := w.file.Seek(0, io.SeekEnd)
	if err != nil {
		return err
	}
	if _, err = w.file.Write(b); err != nil {
		return fmt.Errorf("%s: (t=%d, c=%d): %w", w.Path, t, ch, err)
	}
	w.Index[uint64(t)*uint64(w.Header.Shape[1])+uint64(ch)] = indexEntry{uint64(off), n, crc32.ChecksumIEEE(b)}
	return nil
}
func (w *crateWriter) Abort() error {
	if w.file == nil {
		return nil
	}
	err := w.file.Close()
	w.file = nil
	remove := os.Remove(w.Path)
	if errors.Is(remove, os.ErrNotExist) {
		remove = nil
	}
	return errors.Join(err, remove)
}
func (w *crateWriter) Finish() (result error) {
	if w.file == nil {
		return errors.New("export is closed")
	}
	defer func() {
		if result != nil {
			result = errors.Join(result, w.Abort())
		}
	}()
	missing, first := 0, 0
	for i, e := range w.Index {
		if e.Length == 0 {
			if missing == 0 {
				first = i
			}
			missing++
		}
	}
	if missing > 0 {
		return fmt.Errorf("%s: incomplete export: %d missing slots; first (t=%d, c=%d)", w.Path, missing, first/int(w.Header.Shape[1]), first%int(w.Header.Shape[1]))
	}
	off, err := w.file.Seek(0, io.SeekEnd)
	if err != nil {
		return err
	}
	b := make([]byte, 24*len(w.Index))
	for i, e := range w.Index {
		p := b[24*i:]
		le.PutUint64(p, e.Offset)
		le.PutUint64(p[8:], e.Length)
		le.PutUint32(p[16:], e.CRC)
	}
	f := make([]byte, 32)
	le.PutUint64(f, uint64(off))
	le.PutUint64(f[8:], uint64(len(w.Index)))
	le.PutUint32(f[16:], crc32.ChecksumIEEE(b))
	copy(f[24:], "ETARCZIV")
	if _, err = w.file.Write(b); err != nil {
		return err
	}
	if _, err = w.file.Write(f); err != nil {
		return err
	}
	if err = w.file.Sync(); err != nil {
		return err
	}
	if err = w.file.Close(); err != nil {
		return err
	}
	w.file = nil
	return nil
}
