package doorbell

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"time"
)

const socketIsStream = true
const platformWindows = true
const transportName = "tcp"

// FromEnv connects to VIZI_PLUGIN_DOORBELL_ADDR and writes VIZI_PLUGIN_TOKEN.
// The supervisor owns gRPC binding and atomic VIZI_PLUGIN_PORT_FILE publication,
// which must happen only after this function has returned with a live listener.
func FromEnv(cfg Config) (*Transport, error) {
	address, token := os.Getenv("VIZI_PLUGIN_DOORBELL_ADDR"), os.Getenv("VIZI_PLUGIN_TOKEN")
	return Dial(context.Background(), address, token, cfg)
}

// Dial connects the Windows doorbell, writes the token, configures nonblocking
// mode, and starts the listener. SetupTimeout (default 5 s) bounds the whole
// setup; ctx can shorten/cancel it. After return ctx no longer owns the session.
// Complete the handshake with HandshakeComplete(major, minor, Hello.session_token).
// No transport event is emitted for a launch failure. Tokens never enter errors.
func Dial(ctx context.Context, address, token string, cfg Config) (*Transport, error) {
	if err := cfg.defaults(); err != nil {
		return nil, err
	}
	if err := validateLaunch(address, token); err != nil {
		return nil, err
	}
	if cfg.SetupTimeout == 0 {
		cfg.SetupTimeout = 5 * time.Second
	}
	if cfg.SetupTimeout < 0 {
		return nil, errors.New("doorbell setup timeout must be positive")
	}
	ctx, cancel := context.WithTimeout(ctx, cfg.SetupTimeout)
	defer cancel()
	conn, err := dialTCP(ctx, address)
	if err != nil {
		return nil, fmt.Errorf("connect doorbell: %w", err)
	}
	owned := true
	defer func() {
		if owned {
			conn.Close()
		}
	}()
	stopCancel := context.AfterFunc(ctx, func() { conn.Close() })
	defer stopCancel()
	deadline, _ := ctx.Deadline()
	if err = conn.SetWriteDeadline(deadline); err != nil {
		return nil, fmt.Errorf("token deadline: %w", err)
	}
	if err = writeToken(conn, token); err != nil {
		return nil, fmt.Errorf("write doorbell token: %w", err)
	}
	if err = conn.SetWriteDeadline(time.Time{}); err != nil {
		return nil, err
	}
	fd, info, err := prepareTCP(conn)
	if err != nil {
		return nil, fmt.Errorf("configure doorbell: %w", err)
	}
	wake, releaseWake, err := tcpWakePair(ctx)
	if err != nil {
		return nil, fmt.Errorf("doorbell wake pair: %w", err)
	}
	// A cancellation that won the race owns conn.Close. Do not hand it to a
	// running transport, even when socket setup itself happened to succeed.
	if !stopCancel() {
		releaseWake()
		return nil, ctx.Err()
	}
	if err = ctx.Err(); err != nil {
		releaseWake()
		return nil, err
	}
	t := newTransport(fd, wake, info, cfg)
	t.expectedToken = token
	t.closeSocket = func() { shutdownSocket(fd); conn.Close() }
	t.closeWake = releaseWake
	owned = false
	t.start()
	return t, nil
}

func dialTCP(ctx context.Context, address string) (*net.TCPConn, error) {
	conn, err := (&net.Dialer{}).DialContext(ctx, "tcp4", address)
	if err != nil {
		return nil, err
	}
	return conn.(*net.TCPConn), nil
}

// The private wake endpoint is TCP because Windows readiness waits on sockets.
// Accept and connect are bounded by the same setup context. Peer matching keeps
// an unrelated connection from accidentally becoming the wake receiver.
func tcpWakePair(ctx context.Context) ([2]socketHandle, func(), error) {
	var handles [2]socketHandle
	listener, err := net.ListenTCP("tcp4", &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		return handles, nil, err
	}
	defer listener.Close()
	stopCancel := context.AfterFunc(ctx, func() { listener.Close() })
	defer stopCancel()
	deadline, _ := ctx.Deadline()
	if err = listener.SetDeadline(deadline); err != nil {
		return handles, nil, err
	}
	writer, err := dialTCP(ctx, listener.Addr().String())
	if err != nil {
		return handles, nil, err
	}
	owned := true
	defer func() {
		if owned {
			writer.Close()
		}
	}()
	var reader *net.TCPConn
	for {
		reader, err = listener.AcceptTCP()
		if err != nil {
			return handles, nil, err
		}
		if reader.RemoteAddr().String() == writer.LocalAddr().String() {
			break
		}
		reader.Close()
	}
	defer func() {
		if owned {
			reader.Close()
		}
	}()
	handles[0], _, err = prepareTCP(reader)
	if err != nil {
		return handles, nil, err
	}
	handles[1], _, err = prepareTCP(writer)
	if err != nil {
		return handles, nil, err
	}
	if err = ctx.Err(); err != nil {
		return handles, nil, err
	}
	owned = false
	return handles, func() { reader.Close(); writer.Close() }, nil
}
