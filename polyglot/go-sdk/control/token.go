package control

import (
	"context"
	"crypto/subtle"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// SessionTokenMetadataKey is the gRPC call-metadata key a host MAY also use
// for the Windows session token (control_plane_protocol.md §2.2). A 1.0 host
// sends the token in Hello.session_token; a stream presenting this key as well
// is checked by it before its handler runs (a wrong one is refused), and a
// stream without it is authenticated by Hello.session_token, which a plugin
// MUST accept.
const SessionTokenMetadataKey = "vizi-session-token"

type tokenAuthKey struct{}

// tokensEqual compares in constant time with respect to the token's contents.
func tokensEqual(got, want string) bool {
	return subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}

// errBadToken is the refusal for a stream that does not prove it is the host.
// It never names either token.
var errBadToken = status.Error(codes.Unauthenticated, "session token missing or mismatched")

// authenticateStream runs before the Session handler. A stream presenting a
// token in call metadata is refused here unless it matches; a refused stream
// never reaches the handler, so it neither claims nor ends the session.
func (s *Server) authenticateStream(srv any, ss grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
	if s.cfg.SessionToken == "" {
		return handler(srv, ss)
	}
	md, _ := metadata.FromIncomingContext(ss.Context())
	values := md.Get(SessionTokenMetadataKey)
	if len(values) == 0 {
		// A host that sends the token only in Hello; Session checks it there.
		return handler(srv, ss)
	}
	if len(values) != 1 || !tokensEqual(values[0], s.cfg.SessionToken) {
		return errBadToken
	}
	return handler(srv, &authenticatedStream{ServerStream: ss, ctx: context.WithValue(ss.Context(), tokenAuthKey{}, true)})
}

type authenticatedStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (a *authenticatedStream) Context() context.Context { return a.ctx }

// streamAuthenticated reports whether the stream proved itself: by metadata
// before the handler ran, or by the token its Hello carries.
func (s *Server) streamAuthenticated(ctx context.Context, helloToken string) bool {
	if s.cfg.SessionToken == "" {
		return true
	}
	if ok, _ := ctx.Value(tokenAuthKey{}).(bool); ok {
		return true
	}
	return tokensEqual(helloToken, s.cfg.SessionToken)
}
