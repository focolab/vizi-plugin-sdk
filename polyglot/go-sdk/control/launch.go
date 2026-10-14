//go:build linux || darwin || windows

package control

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"sync"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
)

// Plugin connects the three specified protocol layers. Application handlers use
// Control and receive its Client. DoorbellHandler runs on the doorbell listener
// after SDK grant/hint/violation handling; retain opening PaneState snapshots for
// gesture cancellation, and keep this callback short as doorbell.Config requires.
type Plugin struct {
	Control         Config
	Memory          memory.Config // nil Purposes selects the specified G/V/O/P/X/S set
	Doorbell        doorbell.Config
	Violations      violations.Config
	DoorbellHandler func(*Session, doorbell.Event)
	Started         func(*Session)
}

type Session struct {
	Client     *Client
	Memory     *Memory
	State      *State
	Doorbell   *doorbell.Transport
	Violations *violations.Reporter
	Shortfalls *Shortfalls
}

// WriteLoan uses the shared arena allocator and coalesces its exact shortfall.
// ErrArenaFull still refuses immediately; reporting failure faults the session.
// Success immediately reserves bytes, without admitting their descriptor. The
// caller must reclaim a never-admitted reservation with Segment.ReleaseLoan on
// build/send failure, or retry admission. Once any use is admitted, retain it
// until host release or retirement; a Call timeout is not proof of non-admission.
// PublishLoan performs the single-use push transaction and rollback for you.
// See control/README.md for reader replies and partially admitted shared loans.
func (s *Session) WriteLoan(segment uint64, region uint32, payload []byte) (memory.Deposit, error) {
	seg, err := s.Memory.Registry.Segment(segment)
	if errors.Is(err, memory.ErrNotInstalled) && s.Memory.Registry.Status(segment).State == memory.Released {
		err = memory.ErrRetired
	}
	if err != nil {
		return memory.Deposit{}, err
	}
	d, f, err := seg.WriteLoanReport(region, payload)
	if f != nil {
		if reportErr := s.Shortfalls.Observe(*f); reportErr != nil {
			s.Client.Fail(reportErr)
			return d, errors.Join(err, reportErr)
		}
	}
	return d, err
}

// ServeEnv consumes the host launch environment, starts the doorbell BEFORE
// publishing the gRPC endpoint, and serves exactly one 1.0 session. Returning
// ends the plugin's work; the executable should print a non-nil error to stderr.
// It returns after Shutdown completes or its window expires, a session fault,
// or control-stream EOF/error without Shutdown. The executable should then exit.
// It never removes an existing Unix socket to make a launch appear successful.
func ServeEnv(ctx context.Context, cfg Plugin) (result error) {
	if _, _, err := buildIdentity(cfg.Control); err != nil {
		return err
	}
	registry, err := memory.New(cfg.Memory)
	if err != nil {
		return err
	}
	defer func() { result = errors.Join(result, registry.Close()) }()
	reporter, err := violations.New(cfg.Violations)
	if err != nil {
		return err
	}
	defer reporter.Close()
	session := &Session{Memory: NewMemory(registry), Violations: reporter, Shortfalls: NewShortfalls()}
	var gate sync.RWMutex
	var active *Client
	var handshaken bool
	failed := make(chan error, 1)
	fail := func(err error) {
		if err != nil {
			select {
			case failed <- err:
			default:
			}
		}
	}
	doorCfg := cfg.Doorbell
	if doorCfg.Handler != nil {
		return errors.New("use Plugin.DoorbellHandler for application events")
	}
	doorCfg.Handler = func(event doorbell.Event) {
		gate.RLock()
		c := active
		ready := handshaken
		gate.RUnlock()
		if c == nil || !ready {
			if event.Frame != nil {
				fail(errors.New("doorbell record before control handshake"))
			}
			return
		}
		if f := event.Frame; f != nil {
			if err := reporter.ObserveFrame(*f); err != nil {
				fail(err)
				return
			}
			if f.Violation != nil && f.Violation.Route == "grant_ack" {
				if err := session.Memory.Grant(c, f); err != nil {
					fail(err)
					return
				}
			}
			if f.Disposition == doorbell.Decoded {
				if f.Input != nil {
					f.InputContextError = session.State.CheckInput(*f.Input)
					if f.InputContextError != nil {
						if err := reporter.ObserveRecord(f.Raw, violations.Finding{Where: violations.UnusableContext, Detail: f.InputContextError.Error()}); err != nil {
							fail(err)
						}
					}
				}
				if f.Memory != nil {
					if err := session.Memory.Grant(c, f); err != nil {
						fail(err)
						return
					}
				}
				if f.Notification != nil {
					n := *f.Notification
					c.HintStages(n)
					if seg, err := registry.Segment(n.SegmentID); err == nil {
						epoch := session.State.AcknowledgedEpoch(n.ViewID)
						plan := seg.Hint(n, epoch)
						if err := reporter.ObserveHint(f.Raw, plan); err != nil {
							fail(err)
							return
						}
					}
				}
			}
		}
		if cfg.DoorbellHandler != nil {
			cfg.DoorbellHandler(session, event)
		}
	}
	db, err := doorbell.FromEnv(doorCfg)
	if err != nil {
		return err
	}
	defer db.Stop()
	session.Doorbell = db
	session.State = NewState(db, reporter)
	control := cfg.Control
	ended := make(chan struct{})
	var endErr error
	var endOnce sync.Once
	control.End = func(err error) {
		endOnce.Do(func() { endErr = err; close(ended) })
		db.Stop()
		if cfg.Control.End != nil {
			cfg.Control.End(err)
		}
	}
	control.Attach = func(c *Client) error {
		session.Client = c
		gate.Lock()
		active = c
		gate.Unlock()
		if cfg.Control.Attach != nil {
			return cfg.Control.Attach(c)
		}
		return nil
	}
	control.Handshake = func(h *pb.Hello) error {
		var err error
		if runtime.GOOS == "windows" {
			err = db.HandshakeComplete(h.ProtocolMajor, h.ProtocolMinor, h.SessionToken)
		} else {
			err = db.HandshakeComplete(h.ProtocolMajor, h.ProtocolMinor)
		}
		if err == nil && cfg.Control.Handshake != nil {
			err = cfg.Control.Handshake(h)
		}
		if err == nil {
			gate.Lock()
			handshaken = true
			gate.Unlock()
		}
		return err
	}
	control.Reporter = reporter
	control.Admit = func(c *Client, m *pb.HostMsg) error {
		err := session.Memory.Admit(c, m)
		if err != nil {
			if e := reporter.ObserveError(err); e != nil {
				fail(e)
			}
		}
		return err
	}
	control.ReaderMemory = session.Memory
	control.StageMemory = session.Memory
	control.StageNotify = func(n doorbell.Notification) error { _, err := db.Notify(n); return err }
	control.Prepare = session.Memory.WaitDescriptor
	control.Release = session.Memory.Release
	control.AdmitOutbound = session.Memory.AdmitOutbound
	control.SentOutbound = session.Memory.SentOutbound
	var doorbellStopped bool // owned by the serial control dispatch loop
	control.Arrange = func(c *Client, m *pb.HostMsg) error {
		var err error
		switch m.Msg.(type) {
		case *pb.HostMsg_ToolBinding:
			err = session.applyToolBinding(c, m, cfg.Control, db.AfterPending)
		case *pb.HostMsg_EpochChange, *pb.HostMsg_TimeMapping:
			err = session.State.Apply(c, m)
		case *pb.HostMsg_DoorbellShutdown:
			if doorbellStopped {
				c.diagnostic(errors.New("repeated DoorbellShutdown"))
				return nil
			}
			doorbellStopped = true
			// The host may release its socket as soon as it receives the report.
			// Stop first; this cancels gestures and releases our socket without
			// waiting for application callbacks or ending the control session.
			db.Stop()
			notice := m.GetDoorbellShutdown()
			failure := "absent"
			if notice.FailureClass != nil {
				failure = notice.FailureClass.String()
			}
			err = c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_DoorbellFinalReport{DoorbellFinalReport: &pb.DoorbellFinalReport{Detail: fmt.Sprintf("doorbell stopped: reason=%s; failure_class=%s; %s; stats=%+v", notice.Reason, failure, notice.Detail, db.Stats())}}})
		default:
			err = session.Memory.Apply(c, m)
		}
		if err != nil {
			if e := reporter.ObserveError(err); e != nil {
				fail(e)
			}
		}
		return err
	}
	control.Connected = func(c *Client) {
		go func() {
			if err := session.Shortfalls.Run(c.Context(), c); err != nil && c.Context().Err() == nil {
				fail(err)
			}
		}()
		if cfg.Started != nil {
			cfg.Started(session)
		}
		if cfg.Control.Connected != nil {
			cfg.Control.Connected(c)
		}
	}
	srv, err := NewServer(control)
	if err != nil {
		return err
	}
	// The independent mailbox is the sole source of fault reports. The ordered
	// callback still reaches applications, but never emits a second report.
	go func() {
		select {
		case terminal, ok := <-db.Terminal():
			if !ok {
				return
			}
			gate.RLock()
			c := active
			gate.RUnlock()
			if c == nil {
				select {
				case <-ended:
					return
				default:
				}
				fail(fmt.Errorf("doorbell ended before handshake: %s", terminal.Reason))
				return
			}
			if terminal.Fault != nil {
				msg, err := FaultMessage(terminal.Fault, c.faultContext())
				if err == nil {
					err = c.Send(msg)
				}
				fail(err)
			} else {
				c.diagnostic(fmt.Errorf("doorbell ended: %s", terminal.Reason))
			}
		case <-ctx.Done():
		case <-ended:
		}
	}()
	listener, err := listenEnv()
	if err != nil {
		return err
	}
	defer listener.Close()
	g := srv.GRPCServer()
	defer g.Stop()
	serveErr := make(chan error, 1)
	go func() { serveErr <- g.Serve(listener) }()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case err := <-failed:
		return err
	case err := <-serveErr:
		return err
	case <-ended:
		g.GracefulStop()
		return endErr
	}
}

func listenEnv() (net.Listener, error) {
	if runtime.GOOS != "windows" {
		path := os.Getenv("VIZI_PLUGIN_UDS")
		if path == "" {
			return nil, errors.New("VIZI_PLUGIN_UDS is required")
		}
		return net.Listen("unix", path)
	}
	path := os.Getenv("VIZI_PLUGIN_PORT_FILE")
	if path == "" {
		return nil, errors.New("VIZI_PLUGIN_PORT_FILE is required")
	}
	l, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	if err = publishPort(path, l.Addr().(*net.TCPAddr).Port); err != nil {
		l.Close()
		return nil, err
	}
	return l, nil
}

func publishPort(path string, port int) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".vizi-port-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	_, writeErr := f.WriteString(strconv.Itoa(port))
	closeErr := f.Close()
	if err := errors.Join(writeErr, closeErr); err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}
