package control

import (
	"context"
	"errors"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestHostHelpersAndMetadata(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	done := make(chan error, 1)
	for _, declined := range []bool{false, true} {
		go func() {
			r, err := p.client.RequestSavePath(p.ctx, &pb.RequestSavePath{SuggestedName: "measure.csv", Extension: ".csv", Title: "Save measurements"})
			if err == nil && (r.Declined != declined || !declined && r.Path != "/tmp/measure.csv") {
				err = errors.New("wrong save result")
			}
			done <- err
		}()
		q := p.recv(t)
		if !proto.Equal(q.GetRequestSavePath(), &pb.RequestSavePath{SuggestedName: "measure.csv", Extension: ".csv", Title: "Save measurements"}) {
			t.Fatal(q)
		}
		r := &pb.SavePathResult{Declined: declined}
		if !declined {
			r.Path = "/tmp/measure.csv"
		}
		p.send(t, &pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_SavePathResult{SavePathResult: r}})
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{"gray", "short", "missing"} {
		go func() {
			r, err := p.client.GetColormap(p.ctx, name)
			if name == "gray" {
				if err == nil && (r.Name != "gray" || len(r.Rgba) != 1024 || r.Rgba[1023] != 255) {
					err = errors.New("wrong table")
				}
			} else {
				var he *HostError
				want := pb.Error_PROTOCOL
				if name == "missing" {
					want = pb.Error_NOT_FOUND
				}
				if errors.As(err, &he) && he.Kind == want {
					err = nil
				} else {
					err = errors.New("missing expected colormap error")
				}
			}
			done <- err
		}()
		q := p.recv(t)
		if q.GetGetColormap().GetName() != name {
			t.Fatal(q)
		}
		if name == "missing" {
			p.send(t, &pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_HostError{HostError: &pb.Error{Kind: pb.Error_NOT_FOUND}}})
		} else {
			rgba := make([]byte, 1024)
			for i := range 256 {
				copy(rgba[i*4:], []byte{byte(i), byte(i), byte(i), 255})
			}
			if name == "short" {
				rgba = rgba[:3]
			}
			p.send(t, &pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_ColormapTable{ColormapTable: &pb.ColormapTable{Name: name, Rgba: rgba}}})
		}
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	want := &pb.RoiList{Rois: []*pb.RoiRecord{{RegionId: "a", Revision: 99, ContentHash: "0123456789abcdef"}}}
	go func() {
		r, err := p.client.GetRoisWindow(p.ctx, 2, 4, "a", "absent")
		if err == nil && !proto.Equal(r, want) {
			err = errors.New("region identity changed")
		}
		done <- err
	}()
	q := p.recv(t)
	if r := q.GetGetRois(); r == nil || r.GetPoseTStart() != 2 || r.GetPoseTEnd() != 4 || len(r.RegionIds) != 2 || r.RegionIds[1] != "absent" {
		t.Fatal(q)
	}
	p.send(t, &pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_Rois{Rois: want}})
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	view := &pb.ViewState{SelectedRegionId: "a", TimeIncrement: 0.25, TimeUnit: "s"}
	go func() {
		r, err := p.client.GetViewState(context.Background())
		if err == nil && !proto.Equal(r, view) {
			err = errors.New("view metadata changed")
		}
		done <- err
	}()
	q = p.recv(t)
	p.send(t, &pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_ViewState{ViewState: view}})
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestIntegerListPanelPatch(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	values := &pb.Value{Kind: &pb.Value_Ints{Ints: &pb.IntList{Values: []int64{-1, 0, 2}}}}
	patch := &pb.PluginMsg{Msg: &pb.PluginMsg_PanelPatch{PanelPatch: &pb.PanelPatch{PanelId: "p", Ops: []*pb.PatchOp{{WidgetId: "channels", Op: &pb.PatchOp_SetValue{SetValue: values}}}}}}
	if err := p.client.Send(patch); err != nil {
		t.Fatal(err)
	}
	if got := p.recv(t); !proto.Equal(got, patch) {
		t.Fatal(got)
	}
}
