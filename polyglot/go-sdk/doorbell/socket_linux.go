package doorbell

import "golang.org/x/sys/unix"

const socketType = unix.SOCK_SEQPACKET
const transportName = "seqpacket"
const receiveFlags = unix.MSG_CMSG_CLOEXEC
const sendFlags = unix.MSG_DONTWAIT | unix.MSG_NOSIGNAL

func configurePlatform(fd int) error { return nil }
func lockReceive()                   {}
func unlockReceive()                 {}
