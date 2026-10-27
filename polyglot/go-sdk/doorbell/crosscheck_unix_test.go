//go:build linux || darwin

package doorbell

// Cross-implementation check against a corpus produced by another SDK.
//
// The corpus carries, for every scenario, the lifecycle calls, the record bytes
// and one verdict column per implementation that has replayed them. This
// replays the same records here and compares against every other column.
// Neither implementation is the oracle: a disagreement is a finding against the
// specification, and the questions the documents leave
// open travel with the corpus so all four SDKs treat them the same way.
//
//	VIZI_V2_CORPUS=/path/to/corpus.json go test -run TestCrossCheck ./...
//
// Setting VIZI_V2_VERDICTS also writes this SDK's own column, in the corpus's
// verdict shape, for scripts/compare_v2_verdicts.py to fold back in.

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"sync"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

type verdict struct {
	Type            string `json:"type"`
	Disposition     string `json:"disposition,omitempty"`
	Where           string `json:"where,omitempty"`
	GrantFailure    bool   `json:"grant_failure,omitempty"`
	ImplicitlyClose uint32 `json:"implicitly_closed,omitempty"`
	Interaction     uint32 `json:"interaction,omitempty"`
	Class           string `json:"class,omitempty"`
}

// marshal writes the corpus's verdict shape, where an absent `where` is an
// explicit null rather than a missing key.
func (v verdict) marshal() map[string]any {
	switch v.Type {
	case "frame":
		var where any
		if v.Where != "" {
			where = v.Where
		}
		return map[string]any{"type": "frame", "disposition": v.Disposition, "where": where,
			"grant_failure": v.GrantFailure, "implicitly_closed": v.ImplicitlyClose}
	case "cancellation":
		return map[string]any{"type": "cancellation", "interaction": v.Interaction}
	default:
		return map[string]any{"type": "fault", "class": v.Class}
	}
}

type scenario struct {
	Name         string          `json:"name"`
	Lifecycle    [][]interface{} `json:"lifecycle"`
	Records      []string        `json:"records"`
	FDsPerRecord int             `json:"fds_per_record"`
	// One entry per implementation that has replayed this scenario, this one
	// excluded. Everything in the object that is not a structural key.
	columns map[string][]verdict
}

// structure names the keys that describe a scenario rather than grade it.
var structure = map[string]bool{
	"name": true, "lifecycle": true, "records": true, "fds_per_record": true, "terminal": true,
}

// canonical folds the one label the specification does not fix: it names the
// disposition "unknown opcode" in prose and neither SDK's spelling is wire
// visible, so comparing them would report a divergence that does not exist.
func canonical(v verdict) verdict {
	if v.Disposition == "unknown_op" {
		v.Disposition = "unknown_opcode"
	}
	return v
}

func normalise(e Event) (verdict, bool) {
	switch {
	case e.Frame != nil:
		v := verdict{Type: "frame", Disposition: string(e.Frame.Disposition),
			ImplicitlyClose: e.Frame.ImplicitlyClosed}
		if e.Frame.Violation != nil {
			v.GrantFailure = e.Frame.Violation.Route == "grant_ack"
			if !v.GrantFailure {
				v.Where = e.Frame.Violation.Where
			}
		}
		return v, true
	case e.Cancellation != nil:
		return verdict{Type: "cancellation", Interaction: e.Cancellation.Opening.Interaction}, true
	case e.Terminal != nil && e.Terminal.Fault != nil:
		return verdict{Type: "fault", Class: e.Terminal.Fault.Class}, true
	}
	return verdict{}, false // an ordinary teardown is scenario shutdown, not a verdict
}

func replay(t *testing.T, s scenario) []verdict {
	t.Helper()
	pair, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_SEQPACKET, 0)
	if err != nil {
		t.Skipf("SOCK_SEQPACKET socketpair unavailable: %v", err)
	}
	host := pair[1]
	defer unix.Close(host)

	var mu sync.Mutex
	var got []verdict
	last := time.Now()
	tr, err := Adopt(pair[0], Config{Handler: func(e Event) {
		mu.Lock()
		defer mu.Unlock()
		last = time.Now()
		if v, ok := normalise(e); ok {
			got = append(got, canonical(v))
		}
	}})
	if err != nil {
		unix.Close(pair[0])
		t.Fatalf("adopt: %v", err)
	}
	defer func() { tr.Stop(); _ = tr }()

	for _, call := range s.Lifecycle {
		name := call[0].(string)
		switch name {
		case "tool_activated":
			if err := tr.ToolActivated(uint32(call[1].(float64))); err != nil {
				t.Fatalf("%s: tool_activated: %v", s.Name, err)
			}
		case "epoch_acknowledged":
			if err := tr.EpochAcknowledged(uint8(call[1].(float64)), uint32(call[2].(float64))); err != nil {
				t.Fatalf("%s: epoch_acknowledged: %v", s.Name, err)
			}
		default:
			t.Fatalf("%s: unknown lifecycle call %q", s.Name, name)
		}
	}

	var spare []int
	for _, h := range s.Records {
		data, err := hex.DecodeString(h)
		if err != nil {
			t.Fatalf("%s: bad hex: %v", s.Name, err)
		}
		if s.FDsPerRecord == 1 {
			p, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_DGRAM, 0)
			if err != nil {
				t.Fatalf("%s: fd pair: %v", s.Name, err)
			}
			spare = append(spare, p[0], p[1])
			rights := unix.UnixRights(p[1])
			if _, err := unix.SendmsgN(host, data, rights, nil, 0); err != nil {
				t.Fatalf("%s: sendmsg: %v", s.Name, err)
			}
		} else if _, err := unix.SendmsgN(host, data, nil, nil, 0); err != nil {
			t.Fatalf("%s: send: %v", s.Name, err)
		}
		time.Sleep(3 * time.Millisecond)
	}
	// Settle: stop once nothing new has arrived for a while.
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		quiet := time.Since(last)
		mu.Unlock()
		if quiet > 250*time.Millisecond {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	for _, fd := range spare {
		unix.Close(fd)
	}
	mu.Lock()
	defer mu.Unlock()
	return append([]verdict{}, got...)
}

// byKind splits a column into one subsequence per verdict kind, preserving order.
func byKind(column []verdict) map[string][]verdict {
	kinds := map[string][]verdict{}
	for _, v := range column {
		kinds[v.Type] = append(kinds[v.Type], v)
	}
	return kinds
}

func same(a, b []verdict) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// report describes what separates two columns under a scenario's comparison
// mode, which is "strict" unless the corpus lists the scenario as unsettled.
func report(mode string, mine, theirs []verdict) []string {
	if mode == "interleaving" {
		// The documents fix each kind's own order and not the interleaving
		// between kinds, so compare the kinds separately.
		left, right := byKind(mine), byKind(theirs)
		kinds := map[string]bool{}
		for k := range left {
			kinds[k] = true
		}
		for k := range right {
			kinds[k] = true
		}
		var names []string
		for k := range kinds {
			names = append(names, k)
		}
		sort.Strings(names)
		var trouble []string
		for _, k := range names {
			if !same(left[k], right[k]) {
				trouble = append(trouble, "kind "+k+": "+dump(left[k])+" | "+dump(right[k]))
			}
		}
		return trouble
	}
	var trouble []string
	if len(mine) != len(theirs) {
		trouble = append(trouble, "verdict count differs")
	}
	for i := range mine {
		if i >= len(theirs) {
			trouble = append(trouble, dumpOne(i, mine[i], verdict{}))
			continue
		}
		if mine[i] != theirs[i] {
			trouble = append(trouble, dumpOne(i, mine[i], theirs[i]))
		}
	}
	for i := len(mine); i < len(theirs); i++ {
		trouble = append(trouble, dumpOne(i, verdict{}, theirs[i]))
	}
	return trouble
}

func dump(column []verdict) string {
	out := ""
	for _, v := range column {
		out += " " + v.Type + "/" + v.Disposition + v.Where + v.Class
	}
	return out
}

func dumpOne(i int, mine, theirs verdict) string {
	return fmt.Sprintf("verdict %d:\n    go     %+v\n    other  %+v", i, mine, theirs)
}

func TestCrossCheckAgainstOtherSDKs(t *testing.T) {
	path := os.Getenv("VIZI_V2_CORPUS")
	if path == "" {
		t.Skip("set VIZI_V2_CORPUS to the corpus from scripts/gen_v2_crosscheck.py")
	}
	blob, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read corpus: %v", err)
	}
	var corpus struct {
		Scenarios []json.RawMessage `json:"scenarios"`
		Unsettled []struct {
			Scenario string `json:"scenario"`
			Compare  string `json:"compare"`
			Question string `json:"question"`
		} `json:"unsettled"`
	}
	if err := json.Unmarshal(blob, &corpus); err != nil {
		t.Fatalf("parse corpus: %v", err)
	}
	// A question the documents leave open is data the corpus carries, so every
	// implementation relaxes exactly the same comparison and no more.
	modes := map[string]string{}
	for _, u := range corpus.Unsettled {
		modes[u.Scenario] = u.Compare
	}

	emitted := make([]map[string]any, 0, len(corpus.Scenarios))
	compared := 0
	for _, raw := range corpus.Scenarios {
		var s scenario
		if err := json.Unmarshal(raw, &s); err != nil {
			t.Fatalf("parse scenario: %v", err)
		}
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(raw, &fields); err != nil {
			t.Fatalf("parse scenario fields: %v", err)
		}
		s.columns = map[string][]verdict{}
		for key, value := range fields {
			if structure[key] || key == "go" {
				continue
			}
			var column []verdict
			if err := json.Unmarshal(value, &column); err != nil {
				t.Fatalf("%s: parse %q column: %v", s.Name, key, err)
			}
			for i := range column {
				column[i] = canonical(column[i])
			}
			s.columns[key] = column
		}

		t.Run(s.Name, func(t *testing.T) {
			got := replay(t, s)
			emitted = append(emitted, map[string]any{"name": s.Name, "go": marshalAll(got)})
			mode := modes[s.Name]
			var others []string
			for who := range s.columns {
				others = append(others, who)
			}
			sort.Strings(others)
			for _, who := range others {
				trouble := report(mode, got, s.columns[who])
				if len(trouble) == 0 {
					compared++
					continue
				}
				if mode == "report" {
					// A content divergence with an open question against the
					// documents; printing it is the whole point, failing is not.
					for _, line := range trouble {
						t.Logf("%s: go vs %s (unsettled): %s", s.Name, who, line)
					}
					continue
				}
				for _, line := range trouble {
					t.Errorf("%s: go vs %s: %s", s.Name, who, line)
				}
			}
		})
	}
	if out := os.Getenv("VIZI_V2_VERDICTS"); out != "" {
		blob, err := json.MarshalIndent(map[string]any{"scenarios": emitted}, "", " ")
		if err != nil {
			t.Fatalf("encode verdicts: %v", err)
		}
		if err := os.WriteFile(out, append(blob, '\n'), 0o644); err != nil {
			t.Fatalf("write verdicts: %v", err)
		}
		t.Logf("wrote %s", out)
	}
	if compared == 0 && os.Getenv("VIZI_V2_VERDICTS") == "" {
		// Writing the first column of a --scenarios-only corpus is a legitimate
		// run; grading against nothing is not.
		t.Error("the corpus carried no other SDK's verdicts, and none were written")
	}
}

func marshalAll(column []verdict) []map[string]any {
	out := make([]map[string]any, len(column))
	for i, v := range column {
		out[i] = v.marshal()
	}
	return out
}
