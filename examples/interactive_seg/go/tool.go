package main

import (
	"fmt"
	"math"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

const msgActive = "Lasso active — drag a loop in a 2D view"
const msgMIP = "Turn off Z-MIP — the tool needs to know which plane you are pointing at."
const msgLevel = "Zoom in to full resolution — the tool segments level-0 voxels."
const msgChannels = "Show a single channel — the tool cannot tell which one you mean."
const msgBusy = "Busy — a job is already running."

type gesture struct {
	interaction           uint32
	pair                  memory.PaneEpoch
	contextSet, anchorSet bool
	z                     int
	t, channel, gen       uint32
	fullGen               uint64
	level                 uint8
	shape                 [3]int
	refusal               string
	path                  []point
	liveFailed            bool
}

func (g *gesture) take(in doorbell.Input, usable bool, pane *control.PaneState) {
	if !usable {
		return
	}
	if !g.contextSet {
		g.contextSet = true
		g.t, g.gen, g.level = in.Time, in.Generation, in.Level
		var channels []uint32
		for c := uint32(0); c < 15; c++ {
			if in.Channels&(1<<c) != 0 {
				channels = append(channels, c)
			}
		}
		if pane != nil && pane.Pane != nil && pane.Pane.Epoch == g.pair.Epoch {
			g.fullGen = pane.Pane.Gen
			if in.Channels&(1<<15) != 0 {
				channels = append(channels, pane.Pane.ChannelsHigh...)
			}
			if extent := pane.Dataset.GetGeometry().GetExtent(); extent != nil {
				g.shape = [3]int{int(extent.ZN), int(extent.YN), int(extent.XN)}
			}
		} else if in.Channels&(1<<15) != 0 {
			channels = append(channels, 15, 16)
		}
		if in.Level != 0 {
			g.refusal = msgLevel
		} else if len(channels) != 1 {
			g.refusal = msgChannels
		} else {
			g.channel = channels[0]
		}
	}
	switch in.Position.Kind {
	case doorbell.ColumnYX, doorbell.ColumnZX, doorbell.ColumnZY:
		g.refusal = msgMIP
		return
	case doorbell.Voxel:
		if !g.anchorSet {
			g.z = int(math.RoundToEven(float64(in.Position.Values[0])))
			g.anchorSet = true
		}
		if in.Position.Flags&doorbell.Miss == 0 {
			g.path = append(g.path, point{float64(in.Position.Values[1]), float64(in.Position.Values[2])})
		}
	}
}

func trailItem(g *gesture) (*pb.OverlayItem, error) {
	points := decimate(g.path)
	xyz := make([]geometry.ZYX, len(points))
	for i, p := range points {
		xyz[i] = geometry.ZYX{float64(g.z), p.y, p.x}
	}
	item, err := (plugin.GeometryEncoder{ByteOrder: payload.LittleEndian}).Markers("lasso", xyz)
	if err != nil {
		return nil, err
	}
	item.Style = &pb.OverlayStyle{ColorRgba: []uint32{255, 255, 255, 220}}
	item.GetMarker().SizePx = 5
	return item, nil
}

// Input and lifecycle hooks are serialized by the SDK listener. No host request
// or application job mutex is taken here; segmentation goes to our worker.
func (a *application) event(e doorbell.Event) {
	if e.Cancellation != nil {
		a.clearGesture()
		return
	}
	f := e.Frame
	if f == nil || f.Disposition != doorbell.Decoded || f.Input == nil {
		return
	}
	in := *f.Input
	b := a.session.State.Tool(in.Tool)
	if b == nil || b.ToolId != "seg.lasso" {
		return
	}
	if in.Phase == doorbell.Press {
		if a.gesture != nil {
			a.clearGesture()
		}
		a.gesture = &gesture{interaction: in.Interaction, pair: memory.PaneEpoch{ViewID: in.ViewID, Epoch: in.Epoch}}
	}
	g := a.gesture
	if g == nil || g.interaction != in.Interaction {
		return
	}
	if in.Phase == doorbell.Cancel {
		a.clearGesture()
		return
	}
	if in.Phase != doorbell.Press && in.Phase != doorbell.Drag && in.Phase != doorbell.Release {
		return
	}
	var pane *control.PaneState
	if !g.contextSet && f.InputContextError == nil {
		pane = a.session.State.Pane(in.ViewID)
	}
	g.take(in, f.InputContextError == nil, pane)
	if in.Phase == doorbell.Release {
		a.clearGesture()
		if !g.contextSet {
			return
		}
		if g.refusal != "" {
			a.say(g.refusal)
			return
		}
		if !g.anchorSet {
			return
		}
		if !a.start(func() error { return a.segment(g) }) {
			a.say(msgBusy)
		}
		return
	}
	if len(g.path) == 0 || g.level != 0 || g.refusal == msgMIP {
		return
	}
	started := time.Now()
	item, err := trailItem(g)
	if err == nil {
		var sample memory.Sample
		sample, err = a.trail.ReplaceLive(a.session, 0, &g.pair, item)
		if sample.Revision != 0 {
			a.liveBytes.Store(uint64(sample.Length))
			a.liveCount.Add(1)
		}
	}
	a.liveNanos.Add(uint64(time.Since(started)))
	if err != nil && !g.liveFailed {
		g.liveFailed = true
		detail := err.Error()
		if seg, e := a.session.Memory.Registry.FindContribution(memory.PurposeOverlay, "seg.trail"); e == nil {
			if lane, e := seg.LiveLane(); e == nil {
				detail = fmt.Sprintf("%s (granted preview capacity %d bytes)", detail, lane.PayloadCapacity())
			}
		}
		a.say("Preview unavailable: " + detail + "; segmentation will still run on release.")
	}
}

func (a *application) clearGesture() {
	a.gesture = nil
	if err := a.trail.Clear(); err != nil {
		a.say("Could not clear trail: " + err.Error())
	}
}
