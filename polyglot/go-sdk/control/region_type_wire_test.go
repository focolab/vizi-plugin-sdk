package control

import (
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/proto"
	"strings"
	"testing"
)

func TestRegionTypeWireVocabulary(t *testing.T) {
	for _, tc := range []struct {
		msg   *pb.HostMsg
		class Class
	}{
		{&pb.HostMsg{Msg: &pb.HostMsg_ShapeDrag{ShapeDrag: &pb.ShapeEdit{}}}, HostCoalescing},
		{&pb.HostMsg{Msg: &pb.HostMsg_ShapeCommit{ShapeCommit: &pb.ShapeEdit{}}}, OneWay},
		{&pb.HostMsg{Msg: &pb.HostMsg_ShapeEditEnded{ShapeEditEnded: &pb.ShapeEditEnded{}}}, OneWay},
	} {
		if HostClass(tc.msg) != tc.class {
			t.Fatal(tc.msg)
		}
	}
	if pluginRoutes["shape_proposal"].class != CoalescingPush || pluginRoutes["shape_answer"].class != Push {
		t.Fatal("wrong shape output classes")
	}
	for _, site := range []violations.Site{violations.ShapeOutOfStep, violations.ShapeRefused} {
		if site.String() != strings.ToLower(strings.TrimPrefix(pb.ViolationSite_name[int32(site)], "VIOLATION_SITE_")) {
			t.Fatal(site)
		}
	}
	record := &pb.RoiRecord{TypeOwner: "other.plugin", Roi: &pb.Roi{AutoSurface: pb.AutoSurface_AUTO_SURFACE_STAR, Inclusion: &pb.Inclusion{Mode: pb.InclusionMode_INCLUDE_INTERSECTED}}}
	encoded, err := proto.Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	var decoded pb.RoiRecord
	if err := proto.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(record, &decoded) {
		t.Fatal(&decoded)
	}
	if pb.InclusionMode_INCLUDE_INTERSECTED != 4 || violations.ShapeOutOfStep != 33 || violations.ShapeRefused != 34 {
		t.Fatal("changed additive enum numbers")
	}
}
