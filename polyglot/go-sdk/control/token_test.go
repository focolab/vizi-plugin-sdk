package control

import (
	"context"
	"net"
	"sync/atomic"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const testSessionToken = "0123456789abcdef0123456789abcdef"

// tokenServer serves Session on loopback TCP, the Windows transport, with a
// session token, and counts handshakes and session ends.
func tokenServer(t *testing.T) (pb.PluginClient, *atomic.Int32, *atomic.Int32) {
	t.Helper()
	var handshakes, ends atomic.Int32
	s, err := NewServer(Config{
		PluginID:      "test.plugin",
		BuildIdentity: "test.plugin test build",
		SessionToken:  testSessionToken,
		Handshake:     func(*pb.Hello) error { handshakes.Add(1); return nil },
		End:           func(error) { ends.Add(1) },
		Diagnostic:    func(error) {},
	})
	if err != nil {
		t.Fatal(err)
	}
	l, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	g := s.GRPCServer()
	go g.Serve(l)
	t.Cleanup(g.Stop)
	conn, err := grpc.NewClient(l.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithAuthority("localhost"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return pb.NewPluginClient(conn), &handshakes, &ends
}

func helloWith(token string) *pb.HostMsg {
	return &pb.HostMsg{Id: "Hello from test host 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1, SessionToken: token}}}
}

// attempt opens a stream with optional metadata token, sends Hello and returns
// the first message or the stream's error.
func attempt(t *testing.T, c pb.PluginClient, metaToken *string, helloToken string) (*pb.PluginMsg, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	if metaToken != nil {
		ctx = metadata.AppendToOutgoingContext(ctx, SessionTokenMetadataKey, *metaToken)
	}
	stream, err := c.Session(ctx)
	if err != nil {
		return nil, err
	}
	_ = stream.Send(helloWith(helloToken))
	return stream.Recv()
}

func wantUnauthenticated(t *testing.T, m *pb.PluginMsg, err error) {
	t.Helper()
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("want UNAUTHENTICATED, got %v, %v", m, err)
	}
}

func TestSessionTokenInMetadataRefusesBadStreamsWithoutEndingTheSession(t *testing.T) {
	c, handshakes, ends := tokenServer(t)
	m, err := attempt(t, c, nil, "")
	wantUnauthenticated(t, m, err)
	wrong := "fedcba9876543210fedcba9876543210"
	m, err = attempt(t, c, &wrong, testSessionToken)
	wantUnauthenticated(t, m, err)
	if handshakes.Load() != 0 || ends.Load() != 0 {
		t.Fatalf("a refused stream reached the session: handshakes=%d ends=%d", handshakes.Load(), ends.Load())
	}
	right := testSessionToken
	m, err = attempt(t, c, &right, "")
	if err != nil || m.GetReady() == nil {
		t.Fatalf("authenticated stream: %v, %v", m, err)
	}
	if handshakes.Load() != 1 {
		t.Fatalf("handshakes=%d", handshakes.Load())
	}
	// The session is taken; a later stream with the right token is not a second one.
	m, err = attempt(t, c, &right, "")
	if status.Code(err) != codes.AlreadyExists {
		t.Fatalf("second session: %v, %v", m, err)
	}
}

func TestSessionTokenInHelloIsStillAccepted(t *testing.T) {
	c, handshakes, ends := tokenServer(t)
	m, err := attempt(t, c, nil, "fedcba9876543210fedcba9876543210")
	wantUnauthenticated(t, m, err)
	empty := ""
	m, err = attempt(t, c, &empty, testSessionToken)
	wantUnauthenticated(t, m, err)
	if handshakes.Load() != 0 || ends.Load() != 0 {
		t.Fatalf("a refused stream reached the session: handshakes=%d ends=%d", handshakes.Load(), ends.Load())
	}
	m, err = attempt(t, c, nil, testSessionToken)
	if err != nil || m.GetReady() == nil {
		t.Fatalf("Hello-authenticated stream: %v, %v", m, err)
	}
}
