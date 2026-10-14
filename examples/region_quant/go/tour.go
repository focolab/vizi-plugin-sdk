package main

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func afterTourRow(r, last row) bool {
	return r.display() > last.display() || r.display() == last.display() && r.id > last.id
}

func (a *application) tour(ctx context.Context, c *control.Client, f *plugin.Follow) (err error) {
	// A host end cancels ctx. Use the session context to publish the final status,
	// without holding the state lock during any dwell.
	if err = a.lock(c.Context()); err != nil {
		return err
	}
	if !f.Active() {
		a.unlock()
		return nil
	}
	a.tourID = f.ID()
	rows := ordered(a.rows)
	last, haveLast := a.lastTourRow, a.haveTourRow
	a.unlock()
	defer func() {
		reason := f.Reason()
		if reason == pb.FollowEndReason_FOLLOW_END_UNSPECIFIED || reason == pb.FollowEndReason_FOLLOW_END_PLUGIN_ENDED {
			return
		}
		if e := a.lock(c.Context()); e != nil {
			err = errors.Join(err, e)
			return
		}
		defer a.unlock()
		if a.tourID == f.ID() {
			err = errors.Join(err, a.status("Tour stopped: "+strings.TrimPrefix(reason.String(), "FOLLOW_END_")))
		}
	}()
	for _, r := range rows {
		if f.Resumed() && haveLast && !afterTourRow(r, last) {
			continue
		}
		if err = a.lock(ctx); err != nil {
			return err
		}
		current, exists := a.rows[r.id]
		if !exists {
			a.unlock()
			continue
		}
		var moved bool
		moved, err = f.Move(plugin.ViewTargets{FrameRegion: &r.id})
		if err != nil || !moved {
			a.unlock()
			return err
		}
		a.lastTourRow, a.haveTourRow = current, true
		now := ordered(a.rows)
		for i, v := range now {
			if v.id == r.id {
				err = a.status(fmt.Sprintf("Touring %s (%d of %d)", current.display(), i+1, len(now)))
				break
			}
		}
		a.unlock()
		if err != nil {
			return err
		}
		if !f.Wait(500 * time.Millisecond) {
			return nil
		}
	}
	if err = a.lock(ctx); err != nil {
		return err
	}
	defer a.unlock()
	if f.Active() {
		return a.status("Tour finished")
	}
	return nil // Returning asks the SDK to End exactly once.
}
