//go:build (linux || darwin || windows) && (amd64 || arm64)

package memory

import (
	"sync/atomic"
	"unsafe"
)

// These little-endian 64-bit targets lower sync/atomic to hardware operations,
// without a process-local lock table. Go's SC operations are stronger than the
// acquire/release minimum required by §6.6. Mapping and offsets are 8-aligned.
const sharedAtomics = true

func statePointer(data []byte, offset uint64) *uint64 {
	return (*uint64)(unsafe.Pointer(&data[offset]))
}
func loadState(data []byte, offset uint64) uint64 {
	return atomic.LoadUint64(statePointer(data, offset))
}
func casState(data []byte, offset, old, next uint64) bool {
	return atomic.CompareAndSwapUint64(statePointer(data, offset), old, next)
}
func storeState(data []byte, offset, value uint64) {
	atomic.StoreUint64(statePointer(data, offset), value)
}
