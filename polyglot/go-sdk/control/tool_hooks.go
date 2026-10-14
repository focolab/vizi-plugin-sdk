package control

import pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"

// The control dispatcher is serial. State.Apply installs the binding and admits
// its ack before this queues application work. The listener queue orders that
// work after retirement cancellations, even if a handler is still executing.
func (s *Session) applyToolBinding(c *Client, m *pb.HostMsg, cfg Config, dispatch func(func()) error) error {
	b := m.GetToolBinding()
	before := s.State.Tool(b.GetTool())
	if err := s.State.Apply(c, m); err != nil {
		return err
	}
	after := s.State.Tool(b.GetTool())
	hook := cfg.ToolActivated
	var binding *pb.ToolBinding
	switch {
	case before == nil && after != nil:
		binding = after
	case before != nil && after == nil:
		hook = cfg.ToolDeactivated
		binding = before // retirement may omit actions and even the manifest ID
		binding.Retired, binding.Revision = true, b.Revision
	default:
		return nil
	}
	if hook == nil {
		return nil
	}
	if err := dispatch(func() {
		if c.Context().Err() == nil {
			if err := hook(c.Context(), c, binding); err != nil {
				c.diagnostic(err)
			}
		}
	}); err != nil {
		// The arrangement is already acknowledged. Losing its notification
		// silently would leave the application in a false engagement state.
		c.Fail(err)
		return err
	}
	return nil
}
