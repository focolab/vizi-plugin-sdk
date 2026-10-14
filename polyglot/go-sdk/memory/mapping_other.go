//go:build !linux && !darwin && !windows

package memory

import (
	"errors"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"os"
)

func mapGrant(_ doorbell.Memory, file *os.File) ([]byte, *GrantFailure) {
	if file != nil {
		file.Close()
	}
	return nil, failure(Failed, "native shared-memory mapping is implemented only on Linux, macOS and Windows")
}
func unmap([]byte) error { return errors.New("no native mapping backend") }
