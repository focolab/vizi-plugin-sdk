package memory

// DepositClaim records descriptor arrival, including arrival before a grant is
// complete. Keep it until the message is disposed (Reply.Close for replies).
// Copy and Close are serialized with retirement and native unmap.
type DepositClaim struct {
	registry *Registry
	entry    *entry
	closed   bool // registry.mu
}

func (r *Registry) ClaimDeposit(id uint64) (*DepositClaim, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return nil, ErrClosed
	}
	e := r.get(id)
	if e.state == Released {
		return nil, ErrRetired
	}
	if e.state == FailedState {
		return nil, e.ack.Failure
	}
	if e.state == Installed {
		s := e.segment
		s.mu.Lock()
		s.beginUse()
		if s.retiring.Load() || s.data == nil {
			s.finishUse()
			s.mu.Unlock()
			return nil, ErrRetired
		}
		s.mu.Unlock()
	}
	e.depositPins++
	return &DepositClaim{registry: r, entry: e}, nil
}

func (c *DepositClaim) Close() {
	r := c.registry
	r.mu.Lock()
	defer r.mu.Unlock()
	if c.closed {
		return
	}
	c.closed = true
	c.entry.depositPins--
	if c.entry.state == Installed && c.entry.segment != nil {
		s := c.entry.segment
		s.mu.Lock()
		s.finishUse()
		s.mu.Unlock()
	}
}

func (c *DepositClaim) Resolve(offset, length uint64) (Deposit, error) {
	r := c.registry
	r.mu.Lock()
	defer r.mu.Unlock()
	s, err := c.segment()
	if err != nil {
		return Deposit{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.resolveDeposit(offset, length, true)
}

func (c *DepositClaim) Copy(offset, length uint64, dst []byte) error {
	r := c.registry
	r.mu.Lock()
	s, err := c.segment()
	if err != nil {
		r.mu.Unlock()
		return err
	}
	s.mu.Lock()
	r.mu.Unlock() // independent segments may copy concurrently
	defer s.mu.Unlock()
	d, err := s.resolveDeposit(offset, length, true)
	if err != nil {
		return err
	}
	h := handoff{s: s, index: d.Region, offset: offset, length: length, held: true}
	return h.copy(dst, Loaned)
}

// Caller holds registry.mu; a held descriptor may read while retiring.
func (c *DepositClaim) segment() (*Segment, error) {
	if c.closed || c.registry.closed || c.entry.state == Released {
		return nil, ErrRetired
	}
	if c.entry.state != Installed {
		return nil, ErrNotInstalled
	}
	return c.entry.segment, nil
}
