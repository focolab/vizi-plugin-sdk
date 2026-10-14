package doorbell

import (
	"bytes"
	"errors"
	"io"
	"strings"
	"testing"
)

func TestWindowsSectionNames(t *testing.T) {
	for _, name := range []string{`Local\vizi-0123456789abcdef0123456789abcdef`, `Local\測定`, strings.Repeat("x", 80), "different-future-scheme"} {
		r, err := EncodeMemory(Memory{SegmentID: 1, Size: 32, Permissions: PluginRead, Name: name}, 1)
		if err != nil {
			t.Fatal(err)
		}
		d := newDecoder()
		d.windows = true
		f, fault := d.decode(r[:], 0, false)
		if fault != nil || f.Disposition != Decoded || f.Memory.Name != name || f.Descriptor != nil {
			t.Fatal(f, fault)
		}
		if int(r[26]) != len(name) || r[27+len(name)] != 0 {
			t.Fatalf("name layout: %x", r[26:108])
		}
	}
	for _, tc := range []struct {
		name   string
		change func(*RawRecord)
		why    string
	}{
		{"long", func(r *RawRecord) { r[26] = 81 }, "name_too_long"},
		{"huge", func(r *RawRecord) { r[26] = 255 }, "name_too_long"},
		{"empty", func(r *RawRecord) { clear(r[26:108]) }, "empty_name"},
		{"terminator", func(r *RawRecord) { r[31] = 1 }, "name_terminator"},
		{"embedded nul", func(r *RawRecord) { r[28] = 0 }, "name_contains_nul"},
		{"utf8", func(r *RawRecord) { r[27] = 255 }, "name_utf8"},
		{"tail", func(r *RawRecord) { r[107] = 1 }, "name_tail"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, _ := EncodeMemory(Memory{SegmentID: 1, Size: 32, Permissions: PluginRead, Name: "name"}, 1)
			tc.change(&r)
			checksum(&r)
			d := newDecoder()
			d.windows = true
			f, fault := d.decode(r[:], 0, false)
			if fault != nil || f.Violation == nil || f.Violation.Where != tc.why || f.Violation.Route != "grant_ack" || f.Violation.Class != "malformed" {
				t.Fatal(f, fault)
			}
			// Same name bytes are ignored on Unix. Platform behavior is explicit.
			d = newDecoder()
			f, fault = d.decode(r[:], 1, false)
			if fault != nil || f.Disposition != Decoded {
				t.Fatal(f, fault)
			}
		})
	}
}

func TestStreamAncillaryRules(t *testing.T) {
	for _, op := range []byte{OpMemory, OpInput, OpNotification, 0x99} {
		r := notification(t, 1)
		r[0] = op
		checksum(&r)
		d := newDecoder()
		d.windows = true
		_, fault := d.decode(r[:], 1, false)
		if fault == nil || fault.Class != "wrong_fd_count" {
			t.Fatal(op, fault)
		}
	}
}

func verifyChunking(t *testing.T, chunks []byte) {
	t.Helper()
	a := notification(t, 1)
	b := notification(t, 2)
	c := inputRecord(t, opening(), 3)
	input := append(append(append([]byte{}, a[:]...), b[:]...), c[:]...)
	w := streamWindow{}
	d := newDecoder()
	d.windows = true
	seen := 0
	for i := 0; len(input) > 0; i++ {
		n := len(input)
		if len(chunks) > 0 {
			n = 1 + int(chunks[i%len(chunks)])
		}
		n = min(n, len(w.tail()), len(input))
		copy(w.tail(), input[:n])
		input = input[n:]
		data, complete := w.advance(n)
		if !complete {
			if len(data) != 0 || w.used >= 112 {
				t.Fatal("partial frame exposed")
			}
			continue
		}
		f, fault := d.decode(data, 0, false)
		seen++
		if fault != nil || f.Header.ID != uint32(seen) {
			t.Fatal(f, fault)
		}
	}
	if seen != 3 || w.used != 0 {
		t.Fatal(seen, w.used)
	}
}
func TestEveryStreamSplit(t *testing.T) {
	for i := 1; i <= 112; i++ {
		verifyChunking(t, []byte{byte(i - 1)})
	}
	verifyChunking(t, []byte{255})
}
func TestPartialStreamCapture(t *testing.T) {
	var w streamWindow
	copy(w.tail(), "partial")
	w.advance(7)
	got := w.capture()
	if string(got) != "partial" {
		t.Fatal(got)
	}
	got[0] = 0
	if string(w.capture()) != "partial" {
		t.Fatal("capture aliases live buffer")
	}
}
func FuzzStreamChunks(f *testing.F) {
	f.Add([]byte{0, 110, 255, 3})
	f.Fuzz(func(t *testing.T, b []byte) { verifyChunking(t, b) })
}
func FuzzWindowsNames(f *testing.F) {
	f.Add([]byte{255, 0, 255})
	f.Add([]byte{4, 'n', 'a', 'm', 'e', 0})
	f.Fuzz(func(t *testing.T, b []byte) {
		r, _ := EncodeMemory(Memory{SegmentID: 1, Size: 32, Permissions: PluginRead}, 1)
		copy(r[26:108], b)
		checksum(&r)
		d := newDecoder()
		d.windows = true
		frame, fault := d.decode(r[:], 0, false)
		if fault != nil {
			t.Fatal(fault)
		}
		if frame.Disposition == Decoded && (len(frame.Memory.Name) == 0 || len(frame.Memory.Name) > 80) {
			t.Fatal("invalid name accepted")
		}
	})
}

type tokenWriter struct {
	bytes.Buffer
	limit int
	err   error
}

func (w *tokenWriter) Write(b []byte) (int, error) {
	if w.err != nil {
		return 0, w.err
	}
	return w.Buffer.Write(b[:min(w.limit, len(b))])
}
func TestTokenWriteAndValidation(t *testing.T) {
	token := "0123456789abcdef0123456789ABCDE"
	// The token above is deliberately short: validation must count bytes.
	if validateLaunch("127.0.0.1:5000", token) == nil {
		t.Fatal("short token")
	}
	token = "0123456789abcdef0123456789ABCDEF"
	if err := validateLaunch("127.0.0.1:5000", token); err != nil {
		t.Fatal(err)
	}
	for _, address := range []string{"localhost:1", "192.0.2.1:8", "[::1]:8", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:+12", ""} {
		if validateLaunch(address, token) == nil {
			t.Fatal(address)
		}
	}
	if validateLaunch("127.0.0.1:1", strings.Repeat("é", 16)) == nil {
		t.Fatal("non-ASCII token")
	}
	for _, limit := range []int{1, 3, 32} {
		w := &tokenWriter{limit: limit}
		if err := writeToken(w, token); err != nil || w.String() != token {
			t.Fatal(w.String(), err)
		}
	}
	if err := writeToken(&tokenWriter{limit: 0}, token); !errors.Is(err, io.ErrNoProgress) {
		t.Fatal(err)
	}
	if err := writeToken(&tokenWriter{err: io.ErrClosedPipe}, token); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatal(err)
	}
}
