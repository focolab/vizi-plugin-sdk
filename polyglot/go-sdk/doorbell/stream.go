package doorbell

// A window only ever asks the socket for the bytes left in one record. The
// kernel retains any coalesced following records, so no unbounded queue forms.
type streamWindow struct {
	bytes RawRecord
	used  int
}

func (s *streamWindow) tail() []byte    { return s.bytes[s.used:] }
func (s *streamWindow) capture() []byte { return append([]byte{}, s.bytes[:s.used]...) }
func (s *streamWindow) advance(n int) (data []byte, complete bool) {
	s.used += n
	if s.used != RecordSize {
		return nil, false
	}
	data = append([]byte{}, s.bytes[:]...)
	s.used = 0
	return data, true
}

type receivedPacket struct {
	data                  []byte
	descriptors           []*Descriptor
	ctrunc, complete, eof bool
}
