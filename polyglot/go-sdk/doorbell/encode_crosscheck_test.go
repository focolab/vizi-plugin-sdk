//go:build linux || darwin

package doorbell

// Outbound byte agreement: the D records a plugin actually sends. The expected
// bytes come from the Python SDK's encoder, so a difference here is a wire
// divergence rather than a labeling one.

import (
	"encoding/hex"
	"testing"
)

func TestNotificationBytesMatchPythonSDK(t *testing.T) {
	mask := func(regions ...uint16) Mask {
		var m Mask
		for _, r := range regions {
			if err := m.Set(r); err != nil {
				t.Fatalf("set %d: %v", r, err)
			}
		}
		return m
	}
	cases := []struct {
		n    Notification
		want string
	}{
		{Notification{SegmentID: 9, Mask: mask(3, 64), Epoch: 2, ViewID: 1}, ""},
		{Notification{SegmentID: 1, Mask: mask(0)}, ""},
		{Notification{SegmentID: 0xdeadbeefcafe, Mask: mask(0, 7, 8, 511), Epoch: 7, ViewID: 255}, ""},
		{Notification{SegmentID: 5, Mask: mask(100), Epoch: 1}, ""},
	}
	want := pythonNotificationHex
	if len(want) != len(cases) {
		t.Fatalf("expected %d python vectors, have %d", len(cases), len(want))
	}
	for i, c := range cases {
		r, err := EncodeNotification(c.n, 1)
		if err != nil {
			t.Fatalf("case %d: %v", i, err)
		}
		if got := hex.EncodeToString(r[:]); got != want[i] {
			t.Errorf("case %d bytes differ\n  go     %s\n  python %s", i, got, want[i])
		}
	}
}

var pythonNotificationHex = []string{
	"4402000001000000090000000000000008000000000000000100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000020000000100000000000000000000000000000000000000000000000b3e18eb",
	"4402000001000000010000000000000001000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000a0382b9",
	"4402000001000000fecaefbeadde00008101000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000008007000000ff0000000000000000000000000000000000000000000000a87825d3",
	"44020000010000000500000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000100000000000000000000000000000000000000000000000000000002462bbc",
}
