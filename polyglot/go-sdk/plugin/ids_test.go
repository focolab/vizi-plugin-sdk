package plugin

import (
	"fmt"
	"sync/atomic"
)

var testIDs atomic.Uint64

// freshID is a host request id never used before in this test binary: a
// session faults on an id reused with different content.
func freshID(prefix string) string {
	return fmt.Sprintf("%s-%d", prefix, testIDs.Add(1))
}
