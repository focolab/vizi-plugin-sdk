package main

import (
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
)

func (a *application) clearAim() {
	a.interaction = 0
	a.liveFailed = false
	if err := a.aim.Clear(); err != nil {
		a.say("Could not clear aim: " + err.Error())
	}
}
func (a *application) event(e doorbell.Event) {
	if e.Cancellation != nil {
		a.clearAim()
		return
	}
	f := e.Frame
	if f == nil || f.Disposition != doorbell.Decoded || f.Input == nil {
		return
	}
	in := *f.Input
	b := a.session.State.Tool(in.Tool)
	if b == nil || b.ToolId != "probe.point" {
		return
	}
	if in.Phase == doorbell.Fire {
		if id, ok := a.session.State.ActionID(in); ok && (id == "undo" || id == "clear") {
			if err := a.submit(job{work: func() error { return a.command("probe."+id, nil) }}); err != nil {
				a.say(err.Error())
				a.session.Client.Fail(err)
			}
		}
		return
	}
	if in.Phase == doorbell.Cancel {
		a.clearAim()
		return
	}
	if in.Phase == doorbell.Press {
		if a.interaction != 0 {
			a.clearAim()
		}
		a.interaction = in.Interaction
		a.liveFailed = false
	}
	if in.Phase == doorbell.Release {
		a.clearAim()
	}
	pane := a.session.State.Pane(in.ViewID)
	usable := f.InputContextError == nil && pane != nil && pane.Pane.Epoch == in.Epoch && uint32(pane.Pane.Gen) == in.Generation
	if in.Phase == doorbell.Release {
		if !usable {
			a.say("Input context is unusable; no pick made.")
			return
		}
		if in.Position.Kind == doorbell.None || in.Position.Flags&doorbell.Miss != 0 {
			a.say(errNothing.Error())
			return
		}
		channels := []uint32{}
		for c := uint32(0); c < 15; c++ {
			if in.Channels&(1<<c) != 0 {
				channels = append(channels, c)
			}
		}
		if in.Channels&(1<<15) != 0 {
			channels = append(channels, pane.Pane.ChannelsHigh...)
		}
		if len(channels) == 0 {
			a.say("No visible channel to probe.")
			return
		}
		c := channels[0]
		for _, v := range channels {
			c = min(c, v)
		}
		if err := a.submit(job{pick: true, work: func() error { return a.search(in, pane, c) }}); err != nil {
			a.say(err.Error())
		}
		return
	}
	if (in.Phase != doorbell.Press && in.Phase != doorbell.Drag) || !usable || in.Position.Flags&doorbell.Miss != 0 {
		return
	}
	start, end, ok := segment(in.Position)
	if !ok {
		return
	}
	scale, _, _, err := levelGeometry(pane.Dataset.GetGeometry(), in.Level)
	if err == nil {
		var itemErr error
		item, itemErr := (plugin.GeometryEncoder{ByteOrder: payload.LittleEndian}).Lines("aim", []point{level0(start, scale), level0(end, scale)}, plugin.Segments)
		err = itemErr
		if err == nil {
			_, err = a.aim.ReplaceLive(a.session, 0, &memory.PaneEpoch{ViewID: in.ViewID, Epoch: in.Epoch}, item)
		}
	}
	if err != nil && !a.liveFailed {
		a.liveFailed = true
		a.say("Aim preview unavailable: " + err.Error() + "; the pick can still proceed.")
	}
}
