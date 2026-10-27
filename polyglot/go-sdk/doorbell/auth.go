package doorbell

import (
	"errors"
	"io"
	"net"
	"strconv"
)

func validateLaunch(address, token string) error {
	// v2 specifies exactly an IPv4 loopback literal; no DNS or remote fallback.
	host, port, err := net.SplitHostPort(address)
	if err != nil || host != "127.0.0.1" {
		return errors.New("doorbell address must be 127.0.0.1:<port>")
	}
	for _, c := range port {
		if c < '0' || c > '9' {
			return errors.New("invalid doorbell TCP port")
		}
	}
	n, err := strconv.Atoi(port)
	if err != nil || n < 1 || n > 65535 {
		return errors.New("invalid doorbell TCP port")
	}
	if len(token) != 32 {
		return errors.New("doorbell token must be 32 ASCII hex characters")
	}
	for _, c := range token {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F') {
			return errors.New("doorbell token must be 32 ASCII hex characters")
		}
	}
	return nil
}

// Only authentication may loop on short writes. The caller establishes a
// deadline/cancellation path; record writes never pass through this helper.
func writeToken(w io.Writer, token string) error {
	b := []byte(token)
	for len(b) > 0 {
		n, err := w.Write(b)
		if err != nil {
			return err
		}
		if n <= 0 || n > len(b) {
			return io.ErrNoProgress
		}
		b = b[n:]
	}
	return nil
}
