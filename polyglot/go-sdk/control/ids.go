package control

import (
	"crypto/sha256"
	"fmt"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// IDWindow is how many received ids a session remembers: the last 65,536,
// the host's own bound. An id older than the window is not checked.
const IDWindow = 65536

// idLedger enforces that ids are never reused. It records the ids of the
// messages this end receives that deserve a reply (host requests, a
// GrantStateQuery, and an unknown kind carrying an id) and, separately, the
// ids of the replies it receives. A second message under a recorded id is a
// byte-identical re-send, deduplicated, or a protocol error that faults the
// session. The two tables are kept apart because a reply carries the id of
// the request this end sent, which is not an id the host chose for itself.
// Together they hold the last IDWindow ids received.
type idLedger struct {
	requests map[string][sha256.Size]byte
	replies  map[string][sha256.Size]byte
	window   []ledgerEntry // ring of the ids recorded, oldest at next once full
	next     int
}

type ledgerEntry struct {
	replies bool
	id      string
}

// ErrIDReused is the protocol error for an id used again with different content.
type ErrIDReused struct{ ID string }

func (e *ErrIDReused) Error() string {
	return fmt.Sprintf("id %q reused with different content", e.ID)
}

// observe reports whether m repeats a message already received under its id
// (a re-send to drop), or an *ErrIDReused when the id was used for different
// content. Messages that neither deserve nor are a reply are not tracked.
func (l *idLedger) observe(m *pb.HostMsg) (duplicate bool, err error) {
	if m.Id == "" {
		return false, nil
	}
	var replies bool
	switch class := HostClass(m); {
	case class == Request || class == Unknown || kind(m.ProtoReflect()) == "grant_state_query":
	case class == ReplyClass:
		replies = true
	default:
		return false, nil
	}
	// Received unknown fields are retained, so this re-encoding has the
	// bytes the host sent for every field.
	b, err := proto.MarshalOptions{Deterministic: true}.Marshal(m)
	if err != nil {
		return false, err
	}
	digest := sha256.Sum256(b)
	if l.requests == nil {
		l.requests = make(map[string][sha256.Size]byte)
		l.replies = make(map[string][sha256.Size]byte)
	}
	table := l.table(replies)
	if seen, ok := table[m.Id]; ok {
		if seen == digest {
			return true, nil
		}
		return false, &ErrIDReused{m.Id}
	}
	if len(l.window) < IDWindow {
		l.window = append(l.window, ledgerEntry{replies, m.Id})
	} else {
		old := l.window[l.next]
		delete(l.table(old.replies), old.id)
		l.window[l.next] = ledgerEntry{replies, m.Id}
		l.next = (l.next + 1) % IDWindow
	}
	table[m.Id] = digest
	return false, nil
}

func (l *idLedger) table(replies bool) map[string][sha256.Size]byte {
	if replies {
		return l.replies
	}
	return l.requests
}
