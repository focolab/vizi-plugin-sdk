//go:build linux || darwin

package doorbell

import (
	"errors"
	"fmt"
	"os"
	"syscall"

	"golang.org/x/sys/unix"
)

func closeOnExec(fd int) error {
	_, err := unix.FcntlInt(uintptr(fd), unix.F_SETFD, unix.FD_CLOEXEC)
	return err
}
func prepareSocket(fd int) (SocketInfo, error) {
	var info SocketInfo
	if err := closeOnExec(fd); err != nil {
		return info, err
	}
	if err := unix.SetNonblock(fd, true); err != nil {
		return info, err
	}
	typ, err := unix.GetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_TYPE)
	if err != nil {
		return info, err
	}
	if typ != socketType {
		return info, fmt.Errorf("doorbell SO_TYPE %d, require %d on this platform", typ, socketType)
	}
	local, err := unix.Getsockname(fd)
	if err != nil {
		return info, err
	}
	if _, ok := local.(*unix.SockaddrUnix); !ok {
		return info, errors.New("doorbell is not AF_UNIX")
	}
	if _, err = unix.Getpeername(fd); err != nil {
		return info, fmt.Errorf("doorbell is not connected: %w", err)
	}
	if err = configurePlatform(fd); err != nil {
		return info, err
	}
	info.Type = typ
	info.Nonblocking = true
	info.SendBuffer, err = unix.GetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_SNDBUF)
	if err != nil {
		return info, err
	}
	info.ReceiveBuffer, err = unix.GetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_RCVBUF)
	return info, err
}

func wakePair() ([2]int, error) {
	syscall.ForkLock.RLock()
	defer syscall.ForkLock.RUnlock()
	p, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_DGRAM, 0)
	if err != nil {
		return p, err
	}
	for _, fd := range p {
		if err = closeOnExec(fd); err == nil {
			err = unix.SetNonblock(fd, true)
		}
		if err != nil {
			unix.Close(p[0])
			unix.Close(p[1])
			return p, err
		}
	}
	return p, nil
}

func wouldBlock(err error) bool {
	return errors.Is(err, unix.EAGAIN) || errors.Is(err, unix.EWOULDBLOCK)
}
func backpressure(err error) bool {
	return wouldBlock(err) || errors.Is(err, unix.ENOBUFS) || errors.Is(err, unix.ETOOMANYREFS)
}
func peerGone(err error) bool {
	return errors.Is(err, unix.EPIPE) || errors.Is(err, unix.ECONNREFUSED) ||
		errors.Is(err, unix.ECONNRESET) || errors.Is(err, unix.EDESTADDRREQ) || errors.Is(err, unix.ENOTCONN)
}
func osError(err error) *int {
	if err == nil {
		return nil
	}
	var e syscall.Errno
	if errors.As(err, &e) {
		return ptr(int(e))
	}
	return nil
}

// Claim transfers the fd exactly once. Calling it after the handler returned
// fails. The returned file belongs to the caller, including responsibility for
// closing it after mapping/installation or any installation failure.
func (d *Descriptor) Claim() (*os.File, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if !d.available {
		return nil, errors.New("descriptor already claimed or released")
	}
	d.available = false
	return os.NewFile(uintptr(d.fd), "vizi-grant"), nil
}
func (d *Descriptor) release() {
	if d == nil {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.available {
		d.available = false
		unix.Close(d.fd)
	}
}

// receive always uses recvmsg, with room for oversize detection and two fds.
// Parsing/cleanup precedes all framing checks, even for a zero-byte datagram.
func receive(fd int) (data []byte, fds []int, flags int, err error) {
	var b [120]byte
	oob := make([]byte, unix.CmsgSpace(8))
	lockReceive()
	defer unlockReceive()
	n, on, flags, _, err := unix.Recvmsg(fd, b[:], oob, receiveFlags)
	if err != nil {
		return nil, nil, flags, err
	}
	data = append([]byte{}, b[:n]...)
	msgs, parseErr := unix.ParseSocketControlMessage(oob[:on])
	for _, msg := range msgs {
		if msg.Header.Level != unix.SOL_SOCKET || msg.Header.Type != unix.SCM_RIGHTS {
			continue
		}
		rights, e := unix.ParseUnixRights(&msg)
		if e != nil {
			parseErr = e
			continue
		}
		fds = append(fds, rights...)
	}
	// Check even on Linux, where the atomic receive flag already established it.
	for _, f := range fds {
		if e := closeOnExec(f); e != nil {
			parseErr = e
		}
	}
	if parseErr != nil {
		for _, f := range fds {
			unix.Close(f)
		}
		if flags&unix.MSG_CTRUNC != 0 {
			// Truncation IS the error here, so report it as truncation.
			// Linux cuts the control buffer on a descriptor boundary and the
			// remainder still parses; Darwin can cut it mid-header, and the
			// parse errno would then classify one platform's ctrunc as the
			// other's recv_error. MSG_CTRUNC is unconditionally fatal either
			// way (protocol section 7 rule 3), and the decoder raises it.
			return data, nil, flags, nil
		}
		return data, nil, flags, parseErr
	}
	return data, fds, flags, nil
}
