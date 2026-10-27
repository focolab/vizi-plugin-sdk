package control

import (
	"bytes"
	"context"
	"fmt"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
)

func TestInlineExportDiscardsInstallAndStillWrites(t *testing.T) {
	for _, hooks := range []string{"none", "split", "combined"} {
		t.Run(hooks, func(t *testing.T) {
			r, err := memory.New(memory.Config{})
			if err != nil {
				t.Fatal(err)
			}
			defer r.Close()
			m := NewMemory(r)
			payload := []byte{0, 1, 127, 255}
			cfg := Config{HandleRequest: func(_ context.Context, _ *Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
				chunk := in.GetExportChunk()
				if chunk == nil || chunk.Install != nil || !bytes.Equal(chunk.GetInline(), payload) {
					return nil, fmt.Errorf("inline request changed or retained install: %v", in)
				}
				return &pb.PluginMsg{Msg: &pb.PluginMsg_ChunkWritten{ChunkWritten: &pb.ChunkWritten{}}}, nil
			}}
			if hooks == "split" {
				cfg.Admit, cfg.Prepare, cfg.Release = m.Admit, m.WaitDescriptor, m.Release
			} else if hooks == "combined" {
				cfg.Prepare, cfg.Release = m.Prepare, m.Release
			}
			p := openPeer(t, cfg, nil)
			p.send(t, &pb.HostMsg{Id: "chunk", Msg: &pb.HostMsg_ExportChunk{ExportChunk: &pb.ExportChunk{
				Source:  &pb.ExportChunk_Inline{Inline: payload},
				Install: &pb.InstallSegment{SegmentId: 17, Purpose: memory.PurposeExport},
			}}})
			var reported, written bool
			for range 2 {
				out := p.recv(t)
				if report := out.GetContractViolation(); report != nil {
					if reported || out.Id != "" || report.Where != pb.ViolationSite_VIOLATION_SITE_INSTALL_WITHOUT_SEGMENT || report.Count != 1 {
						t.Fatal("incorrect or duplicate report", out)
					}
					reported = true
				} else if out.GetChunkWritten() != nil && out.Id == "chunk" {
					if written {
						t.Fatal("duplicate reply", out)
					}
					written = true
				} else {
					t.Fatal("expected violation and successful inline reply", out)
				}
			}
			if !reported || !written || r.Status(17).State != memory.NotPresent || m.DroppedDescriptors() != 0 {
				t.Fatal("inline chunk must be written without installing or disposing a descriptor")
			}
			if count := p.client.cfg.Reporter.Stats()[violations.InstallWithoutSegment].Observed; count != 1 {
				t.Fatal("install reported more than once", count)
			}
		})
	}
}

func TestMemoryPrepareInlineDiscardsInstallWithoutWaiting(t *testing.T) {
	c := stateClient(t)
	reporter, err := violations.New(violations.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer reporter.Close()
	c.cfg.Reporter = reporter
	// A canceled wait context and no registry prove inline data needs no
	// descriptor access, even when the adapter is used directly.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	m := NewMemory(nil)
	chunk := &pb.ExportChunk{Source: &pb.ExportChunk_Inline{Inline: []byte{1, 2}}, Install: &pb.InstallSegment{SegmentId: 17}}
	msg := &pb.HostMsg{Msg: &pb.HostMsg_ExportChunk{ExportChunk: chunk}}
	for range 2 {
		if err := m.Prepare(ctx, c, msg); err != nil {
			t.Fatal("inline chunk required descriptor preparation", err)
		}
		if chunk.Install != nil || !bytes.Equal(chunk.GetInline(), []byte{1, 2}) {
			t.Fatal("inline chunk not preserved", chunk)
		}
		if err := m.Release(c, msg); err != nil {
			t.Fatal(err)
		}
	}
	if count := reporter.Stats()[violations.InstallWithoutSegment].Observed; count != 1 {
		t.Fatal("install must be reported only once", count)
	}
}
