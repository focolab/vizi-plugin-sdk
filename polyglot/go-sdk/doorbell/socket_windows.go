package doorbell

import (
	"errors"
	"io"
	"net"
	"os"
	"runtime"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

type socketHandle = windows.Handle

// Winsock constants omitted by x/sys/windows. These are Windows SDK values,
// not Unix poll flags: WinSock2.h / ws2def.h.
const (
	wsaFIONBIO  = 0x8004667e
	wsaSOType   = 0x1008
	wsaPollRead = 0x0100 // POLLRDNORM (not Unix POLLIN)
)

type wsaPollFD struct {
	fd              windows.Handle
	events, revents int16
}

var wsaPoll = windows.NewLazySystemDLL("ws2_32.dll").NewProc("WSAPoll")

// Go owns handle lifetime and bounded startup only. Once this returns, no Go
// net read/write operation is issued on the socket; records use synchronous
// Winsock calls with FIONBIO enabled. In particular no overlapped operation may
// retain a caller's record after sendSocket returns.
func prepareTCP(conn *net.TCPConn) (windows.Handle, SocketInfo, error) {
	var fd windows.Handle
	var info SocketInfo
	if err := conn.SetNoDelay(true); err != nil {
		return fd, info, err
	}
	if err := wsaPoll.Find(); err != nil {
		return fd, info, err
	}
	raw, err := conn.SyscallConn()
	if err != nil {
		return fd, info, err
	}
	var setupErr error
	err = raw.Control(func(handle uintptr) {
		fd = windows.Handle(handle)
		info.Type, setupErr = windows.GetsockoptInt(fd, windows.SOL_SOCKET, wsaSOType)
		if setupErr != nil {
			return
		}
		if info.Type != windows.SOCK_STREAM {
			setupErr = errors.New("doorbell requires SOCK_STREAM on Windows")
			return
		}
		if setupErr = windows.SetHandleInformation(fd, windows.HANDLE_FLAG_INHERIT, 0); setupErr != nil {
			return
		}
		mode := uint32(1)
		var returned uint32
		setupErr = windows.WSAIoctl(fd, wsaFIONBIO, (*byte)(unsafe.Pointer(&mode)), 4, nil, 0, &returned, nil, 0)
		if setupErr != nil {
			return
		}
		info.Nonblocking = true
		info.SendBuffer, setupErr = windows.GetsockoptInt(fd, windows.SOL_SOCKET, windows.SO_SNDBUF)
		if setupErr != nil {
			return
		}
		info.ReceiveBuffer, setupErr = windows.GetsockoptInt(fd, windows.SOL_SOCKET, windows.SO_RCVBUF)
	})
	if err != nil {
		return fd, info, err
	}
	return fd, info, setupErr
}

func sendSocket(fd windows.Handle, b []byte) (int, error) {
	var sent uint32
	buf := windows.WSABuf{Len: uint32(len(b)), Buf: &b[0]}
	err := windows.WSASend(fd, &buf, 1, &sent, 0, nil, nil)
	runtime.KeepAlive(b)
	return int(sent), err
}
func sendWake(fd windows.Handle, b []byte) (int, error) {
	n, err := sendSocket(fd, b)
	if !interrupted(err) && !wouldBlock(err) && (err != nil || n != len(b)) {
		// Would-block means queued wake bytes already make the reader ready.
		// Other failures (notably ENOBUFS) do not imply a queued byte. A FIN
		// makes the private wake reader ready without allocating another send
		// buffer; it will turn this internal failure into local teardown.
		windows.Shutdown(fd, windows.SHUT_WR)
	}
	return n, err
}
func recvSocket(fd windows.Handle, b []byte) (int, error) {
	var count, flags uint32
	buf := windows.WSABuf{Len: uint32(len(b)), Buf: &b[0]}
	err := windows.WSARecv(fd, &buf, 1, &count, &flags, nil, nil)
	runtime.KeepAlive(b)
	return int(count), err
}
func pollSockets(fd, wake windows.Handle) (bool, error) {
	p := [2]wsaPollFD{{fd: fd, events: wsaPollRead}, {fd: wake, events: wsaPollRead}}
	// Proc.Call captures GetLastError on the syscall thread, as the generated
	// x/sys Winsock wrappers do. A later WSAGetLastError call could move threads.
	r, _, err := wsaPoll.Call(uintptr(unsafe.Pointer(&p[0])), 2, uintptr(0xffffffff))
	runtime.KeepAlive(p)
	if int32(r) == -1 {
		return false, err
	}
	return p[1].revents != 0, nil
}
func drainWake(fd windows.Handle) error {
	var b [128]byte
	for {
		n, err := recvSocket(fd, b[:])
		if interrupted(err) {
			continue
		}
		if wouldBlock(err) {
			return nil
		}
		if err != nil {
			return err
		}
		if n == 0 {
			return io.ErrUnexpectedEOF
		}
	}
}
func shutdownSocket(fd windows.Handle) { windows.Shutdown(fd, windows.SHUT_RDWR) }
func (t *Transport) receivePacket() (receivedPacket, error) {
	n, err := recvSocket(t.fd, t.partial.tail())
	if err != nil {
		return receivedPacket{}, err
	}
	if n == 0 {
		return receivedPacket{eof: true}, nil
	}
	data, complete := t.partial.advance(n)
	return receivedPacket{data: data, complete: complete}, nil
}

func interrupted(err error) bool   { return errors.Is(err, windows.WSAEINTR) }
func wouldBlock(err error) bool    { return errors.Is(err, windows.WSAEWOULDBLOCK) }
func backpressure(err error) bool  { return wouldBlock(err) || errors.Is(err, windows.WSAENOBUFS) }
func locallyClosed(err error) bool { return errors.Is(err, windows.WSAESHUTDOWN) }
func peerGone(err error) bool {
	return errors.Is(err, windows.WSAECONNRESET) || errors.Is(err, windows.WSAECONNABORTED) ||
		errors.Is(err, windows.WSAECONNREFUSED) || errors.Is(err, windows.WSAEDESTADDRREQ) || errors.Is(err, windows.WSAENOTCONN)
}
func osError(err error) *int {
	var e syscall.Errno
	if errors.As(err, &e) {
		return ptr(int(e))
	}
	return nil
}

// Claim is unavailable on Windows: M carries Memory.Name, never an fd.
func (d *Descriptor) Claim() (*os.File, error) {
	return nil, errors.New("Windows grants use Memory.Name, not descriptors")
}
func (d *Descriptor) release() {}
