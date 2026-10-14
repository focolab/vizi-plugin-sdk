//go:build linux || darwin

// Command doorbell-peer is one end of a live doorbell, for
// scripts/head_to_head.py.
//
// It adopts the descriptor in VIZI_DOORBELL_FD and either sends the D hints
// named on the command line or prints the ones it receives, one canonical line
// each. The other end is another SDK's peer over the same socketpair, so this
// exercises this SDK's encoder against that one's decoder — no recording, and
// no implementation grading another.
//
//	VIZI_DOORBELL_FD=3 doorbell-peer send 9/1/2/3,64
//	VIZI_DOORBELL_FD=3 doorbell-peer receive
//
// A hint is written segment/pane/epoch/regions, with "-" for an absent pane.
package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

// A sender outlives its own records; a receiver outlives the slowest peer's
// startup, which is the JVM's. Both are harness timing, not protocol.
const (
	sendWindow    = 1200 * time.Millisecond
	receiveWindow = 2200 * time.Millisecond
)

func parse(spec string) (doorbell.Notification, error) {
	parts := strings.Split(spec, "/")
	if len(parts) != 4 {
		return doorbell.Notification{}, fmt.Errorf("want segment/pane/epoch/regions, got %q", spec)
	}
	segment, err := strconv.ParseUint(parts[0], 10, 64)
	if err != nil {
		return doorbell.Notification{}, err
	}
	n := doorbell.Notification{SegmentID: segment}
	if parts[1] != "-" {
		pane, err := strconv.ParseUint(parts[1], 10, 8)
		if err != nil {
			return doorbell.Notification{}, err
		}
		n.ViewID = uint8(pane)
	}
	if parts[2] != "-" {
		epoch, err := strconv.ParseUint(parts[2], 10, 32)
		if err != nil {
			return doorbell.Notification{}, err
		}
		n.Epoch = uint32(epoch)
	}
	for _, r := range strings.Split(parts[3], ",") {
		if r == "" {
			continue
		}
		region, err := strconv.ParseUint(r, 10, 16)
		if err != nil {
			return doorbell.Notification{}, err
		}
		if err := n.Mask.Set(uint16(region)); err != nil {
			return doorbell.Notification{}, err
		}
	}
	return n, nil
}

// show is the canonical line every peer prints for one received hint.
func show(n doorbell.Notification) string {
	pane, epoch := "-", "-"
	if n.Epoch != 0 {
		pane, epoch = strconv.FormatUint(uint64(n.ViewID), 10), strconv.FormatUint(uint64(n.Epoch), 10)
	}
	var regions []string
	for r := uint16(0); r < 512; r++ {
		if n.Mask.Has(r) {
			regions = append(regions, strconv.FormatUint(uint64(r), 10))
		}
	}
	return fmt.Sprintf("hint segment=%d pane=%s epoch=%s regions=%s",
		n.SegmentID, pane, epoch, strings.Join(regions, ","))
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: doorbell-peer send|receive [hint ...]")
		os.Exit(2)
	}
	fd, err := strconv.Atoi(os.Getenv("VIZI_DOORBELL_FD"))
	if err != nil {
		fmt.Fprintf(os.Stderr, "VIZI_DOORBELL_FD: %v\n", err)
		os.Exit(2)
	}
	tr, err := doorbell.Adopt(fd, doorbell.Config{Handler: func(e doorbell.Event) {
		if e.Frame == nil {
			return
		}
		if e.Frame.Notification != nil {
			fmt.Println(show(*e.Frame.Notification))
			return
		}
		fmt.Printf("other %s\n", e.Frame.Disposition)
	}})
	if err != nil {
		fmt.Fprintf(os.Stderr, "adopt: %v\n", err)
		os.Exit(1)
	}
	if err := tr.HandshakeComplete(doorbell.ProtocolMajor, doorbell.ProtocolMinor); err != nil {
		fmt.Fprintf(os.Stderr, "handshake: %v\n", err)
		os.Exit(1)
	}
	if os.Args[1] == "send" {
		for _, spec := range os.Args[2:] {
			n, err := parse(spec)
			if err != nil {
				fmt.Fprintf(os.Stderr, "parse %q: %v\n", spec, err)
				os.Exit(2)
			}
			result, err := tr.Notify(n)
			if err != nil || result != doorbell.Sent {
				fmt.Printf("refused %v %v\n", result, err)
			}
		}
		time.Sleep(sendWindow)
	} else {
		time.Sleep(receiveWindow)
	}
	tr.Stop()
}
