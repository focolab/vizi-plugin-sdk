package doorbell

import (
	"golang.org/x/sys/unix"
	"syscall"
)

const socketType = unix.SOCK_DGRAM
const transportName = "dgram"
const receiveFlags = 0
const sendFlags = unix.MSG_DONTWAIT

func configurePlatform(fd int) error {
	if err := unix.SetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_NOSIGPIPE, 1); err != nil {
		return err
	}
	// Never reduce a buffer already raised beyond the recommended minimum.
	n, err := unix.GetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_RCVBUF)
	if err != nil {
		return err
	}
	if n < 64*1024 {
		return unix.SetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_RCVBUF, 64*1024)
	}
	return nil
}

// Go's fork/exec cooperates with ForkLock. Native code spawning independently
// still has the documented Darwin recvmsg-to-CLOEXEC race.
func lockReceive()   { syscall.ForkLock.RLock() }
func unlockReceive() { syscall.ForkLock.RUnlock() }
