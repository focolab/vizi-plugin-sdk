//go:build linux || darwin

package memory

import (
	"errors"
	"os"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"golang.org/x/sys/unix"
)

// The descriptor is closed as soon as mmap succeeds (or fails). Only the mapping
// remains owned by the registry. MAP_SHARED is essential for claim visibility.
func mapGrant(g doorbell.Memory, file *os.File) ([]byte, *GrantFailure) {
	if file == nil {
		return nil, failure(Failed, "Unix grant has no descriptor")
	}
	defer file.Close()
	st, err := file.Stat()
	if err != nil {
		return nil, mappingFailure(err)
	}
	if st.Size() < 0 || uint64(st.Size()) != g.Size {
		return nil, failure(Malformed, "object size does not match M.size")
	}
	prot := unix.PROT_READ
	if g.Permissions&doorbell.PluginWrite != 0 {
		prot |= unix.PROT_WRITE
	}
	data, err := unix.Mmap(int(file.Fd()), 0, int(g.Size), prot, unix.MAP_SHARED)
	if err != nil {
		if errors.Is(err, unix.EACCES) {
			return nil, failure(Malformed, "cannot obtain declared mapping permissions: "+err.Error())
		}
		return nil, mappingFailure(err)
	}
	return data, nil
}
func unmap(data []byte) error { return unix.Munmap(data) }
func mappingFailure(err error) *GrantFailure {
	class := Failed
	if errors.Is(err, unix.ENOMEM) || errors.Is(err, unix.EMFILE) || errors.Is(err, unix.ENFILE) {
		class = Retryable
	}
	return failure(class, err.Error())
}
