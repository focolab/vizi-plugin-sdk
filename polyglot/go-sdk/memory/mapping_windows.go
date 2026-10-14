//go:build windows

package memory

import (
	"errors"
	"os"
	"reflect"
	"strings"
	"unicode/utf8"
	"unsafe"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"golang.org/x/sys/windows"
)

var openFileMappingW = windows.NewLazySystemDLL("kernel32.dll").NewProc("OpenFileMappingW")

// Open-only avoids CreateFileMapping's silent creation of a private section.
// The common registry path still validates preamble identity, padding and CRC.
// As on Unix, the capability handle closes promptly after mapping. Windows keeps
// the section alive through its view; retirement releases that final local view.
func mapGrant(g doorbell.Memory, file *os.File) ([]byte, *GrantFailure) {
	if file != nil {
		file.Close()
		return nil, failure(Malformed, "Windows grant unexpectedly carries a descriptor")
	}
	if len(g.Name) == 0 || len(g.Name) > 80 || !utf8.ValidString(g.Name) || strings.ContainsRune(g.Name, 0) {
		return nil, failure(Malformed, "invalid Windows section name")
	}
	name, err := windows.UTF16PtrFromString(g.Name)
	if err != nil {
		return nil, failure(Malformed, err.Error())
	}
	access := uint32(windows.FILE_MAP_READ)
	if g.Permissions&doorbell.PluginWrite != 0 {
		access |= windows.FILE_MAP_WRITE
	}
	handle, _, callErr := openFileMappingW.Call(uintptr(access), 0, uintptr(unsafe.Pointer(name)))
	if handle == 0 {
		return nil, mappingFailure(callErr)
	}
	h := windows.Handle(handle)
	addr, err := windows.MapViewOfFile(h, access, 0, 0, uintptr(g.Size))
	closeErr := windows.CloseHandle(h)
	if err != nil {
		return nil, mappingFailure(errors.Join(err, closeErr))
	}
	if closeErr != nil {
		return nil, mappingFailure(errors.Join(closeErr, windows.UnmapViewOfFile(addr)))
	}
	return mappedBytes(addr, int(g.Size)), nil
}

// The uintptr is an OS mapping address, not a Go heap pointer. Modify an actual
// slice header (never a standalone header cast to a slice); its storage is owned
// by Windows until UnmapViewOfFile, with no Go object needing to be kept alive.
func mappedBytes(addr uintptr, size int) []byte {
	var data []byte
	header := (*reflect.SliceHeader)(unsafe.Pointer(&data))
	header.Data, header.Len, header.Cap = addr, size, size
	return data
}

func unmap(data []byte) error {
	return windows.UnmapViewOfFile(uintptr(unsafe.Pointer(&data[0])))
}

func mappingFailure(err error) *GrantFailure {
	class := Failed
	switch {
	case errors.Is(err, windows.ERROR_ACCESS_DENIED):
		class = Malformed
	case errors.Is(err, windows.ERROR_NOT_ENOUGH_MEMORY), errors.Is(err, windows.ERROR_OUTOFMEMORY),
		errors.Is(err, windows.ERROR_TOO_MANY_OPEN_FILES), errors.Is(err, windows.ERROR_NO_SYSTEM_RESOURCES):
		class = Retryable
	}
	return failure(class, err.Error())
}
