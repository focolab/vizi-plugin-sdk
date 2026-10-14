package control

import (
	"fmt"
	"slices"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type exportState struct {
	filtered bool
	rejected bool
}

// checkExport runs before raw or typed author callbacks. Rejection leaves the
// handle available for AbortExport cleanup, but prevents further pixel writes
// or finalization. The request worker still releases a rejected descriptor.
func (c *Client) checkExport(m *pb.HostMsg) error {
	if q := m.GetAbortExport(); q != nil {
		delete(c.exports, q.Handle)
	}
	if q := m.GetFinishExport(); q != nil {
		if c.exports[q.Handle].rejected {
			return fmt.Errorf("export %q rejected contradictory filtered_channels; use AbortExport", q.Handle)
		}
		// Terminal callbacks may release resources even when they fail.
		delete(c.exports, q.Handle)
	}
	q := m.GetExportChunk()
	if q == nil {
		return nil
	}
	state, known := c.exports[q.Handle]
	if state.rejected {
		return fmt.Errorf("export %q rejected contradictory filtered_channels; use AbortExport", q.Handle)
	}
	var err error
	if len(q.FilteredChannels) != 0 && !state.filtered {
		err = fmt.Errorf("export %q: filtered_channels requires OpenExport.filter", q.Handle)
	} else {
		for _, channel := range q.FilteredChannels {
			if !slices.Contains(q.Channels, channel) {
				err = fmt.Errorf("export %q: filtered_channels contains channel %d absent from chunk channels", q.Handle, channel)
				break
			}
		}
	}
	if err != nil && known {
		state.rejected = true
		c.exports[q.Handle] = state
	}
	return err
}
