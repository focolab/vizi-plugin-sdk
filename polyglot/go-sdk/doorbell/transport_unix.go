//go:build linux || darwin

package doorbell

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"syscall"

	"golang.org/x/sys/unix"
)

type socketHandle = int

const socketIsStream = false
const platformWindows = false

// Adopt takes ownership of fd on entry, including setup failure, and starts the
// listener. Do not retain aliases or change its blocking flags afterwards.
// Setup errors are launch failures, not DoorbellFaults. Available on Unix only.
func Adopt(fd int, cfg Config) (*Transport, error) {
	if fd < 0 {
		return nil, errors.New("invalid doorbell fd")
	}
	owned := true
	defer func() {
		if owned {
			unix.Close(fd)
		}
	}()
	if err := cfg.defaults(); err != nil {
		return nil, err
	}
	syscall.ForkLock.RLock()
	info, err := prepareSocket(fd)
	syscall.ForkLock.RUnlock()
	if err != nil {
		if errors.Is(err, unix.EBADF) || errors.Is(err, unix.ENOTSOCK) {
			return nil, fmt.Errorf("adopt doorbell fd %d: descriptor is closed or is not a socket; the doorbell socket may not have been inherited because a launcher closed non-stdio descriptors; exec the plugin with the descriptor open, or explicitly pass it through to the child: %w", fd, err)
		}
		return nil, fmt.Errorf("adopt doorbell: %w", err)
	}
	wake, err := wakePair()
	if err != nil {
		return nil, fmt.Errorf("doorbell wake pair: %w", err)
	}
	t := newTransport(fd, wake, info, cfg)
	t.closeSocket = func() { unix.Shutdown(fd, unix.SHUT_RDWR); unix.Close(fd) }
	t.closeWake = func() { unix.Close(wake[0]); unix.Close(wake[1]) }
	owned = false
	t.start()
	return t, nil
}

// FromEnv adopts VIZI_PLUGIN_DOORBELL_FD. The supervisor owns VIZI_PLUGIN_UDS.
func FromEnv(cfg Config) (*Transport, error) {
	v, ok := os.LookupEnv("VIZI_PLUGIN_DOORBELL_FD")
	if !ok || v == "" {
		return nil, errors.New("missing VIZI_PLUGIN_DOORBELL_FD")
	}
	for _, c := range v {
		if c < '0' || c > '9' {
			return nil, errors.New("VIZI_PLUGIN_DOORBELL_FD must be a nonnegative decimal fd")
		}
	}
	fd, err := strconv.Atoi(v)
	if err != nil {
		return nil, fmt.Errorf("VIZI_PLUGIN_DOORBELL_FD: %w", err)
	}
	return Adopt(fd, cfg)
}

func interrupted(err error) bool               { return errors.Is(err, unix.EINTR) }
func locallyClosed(error) bool                 { return false } // Unix cannot distinguish it from peer-gone.
func sendSocket(fd int, b []byte) (int, error) { return unix.SendmsgN(fd, b, nil, nil, sendFlags) }
func sendWake(fd int, b []byte) (int, error)   { return sendSocket(fd, b) }
func pollSockets(fd, wake int) (bool, error) {
	p := []unix.PollFd{{Fd: int32(fd), Events: unix.POLLIN}, {Fd: int32(wake), Events: unix.POLLIN}}
	_, err := unix.Poll(p, -1)
	return p[1].Revents != 0, err
}
func drainWake(fd int) error {
	var b [128]byte
	for {
		_, _, _, _, err := unix.Recvmsg(fd, b[:], nil, 0)
		if interrupted(err) {
			continue
		}
		if wouldBlock(err) {
			return nil
		}
		if err != nil {
			return err
		}
	}
}
func (t *Transport) receivePacket() (receivedPacket, error) {
	b, fds, flags, err := receive(t.fd)
	if err != nil {
		return receivedPacket{}, err
	}
	p := receivedPacket{data: b, complete: true, ctrunc: flags&unix.MSG_CTRUNC != 0}
	for _, fd := range fds {
		p.descriptors = append(p.descriptors, &Descriptor{fd: fd, available: true})
	}
	// Empty records carrying rights or truncated control data are frames, not
	// EOF: the decoder applies ctrunc -> wrong_fd_count -> bad_length, and
	// acceptLocked closes all received descriptors. Darwin's empty datagram
	// without rights is also bad_length.
	if len(b) == 0 && socketType == unix.SOCK_SEQPACKET && !p.ctrunc && len(fds) == 0 {
		p.eof = true
	}
	return p, nil
}
