package stage

import (
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func inputShape() *pb.StageShape {
	return &pb.StageShape{Channels: []uint32{0, 65535}, Z: 256, Y: 3840, X: 1920, Dtype: pb.DType_DTYPE_U8}
}
func plan() *pb.StagePlanned {
	return &pb.StagePlanned{Output: &pb.StageShape{Z: 256, Y: 256, X: 256, Dtype: pb.DType_DTYPE_F32}, Modifies: []*pb.StageChannelNeed{{Channel: 65535, Needs: []uint32{0, 65535}}}, Adds: []*pb.StageNewChannel{{Key: "flow", Name: "Flow", Needs: []uint32{65535}}}}
}
func TestPlans(t *testing.T) {
	in := inputShape()
	if err := ValidatePlan(in, plan()); err != nil {
		t.Fatal(err)
	}
	p := plan()
	p.Modifies, p.Adds = nil, nil // Extent/origin/pose-only stages are legal.
	p.Reads = []uint32{0, 65535}
	if err := ValidatePlan(in, p); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name   string
		change func(*pb.StagePlanned)
	}{
		{"zero z", func(p *pb.StagePlanned) { p.Output.Z = 0 }},
		{"zero y", func(p *pb.StagePlanned) { p.Output.Y = 0 }},
		{"zero x", func(p *pb.StagePlanned) { p.Output.X = 0 }},
		{"read-only missing", func(p *pb.StagePlanned) { p.Reads = []uint32{1} }},
		{"read-only index", func(p *pb.StagePlanned) { p.Reads = []uint32{65536} }},
		{"growing", func(p *pb.StagePlanned) { p.Output.Z = 257 }},
		{"unknown dtype", func(p *pb.StagePlanned) { p.Output.Dtype = 99 }},
		{"missing output", func(p *pb.StagePlanned) { p.Output = nil }},
		{"output channel range", func(p *pb.StagePlanned) { p.Output.Channels = []uint32{65536} }},
		{"modified channel range", func(p *pb.StagePlanned) { p.Modifies[0].Channel = 65536 }},
		{"dependency range", func(p *pb.StagePlanned) { p.Modifies[0].Needs = []uint32{65536} }},
		{"missing input", func(p *pb.StagePlanned) { p.Adds[0].Needs = []uint32{1} }},
		{"empty key", func(p *pb.StagePlanned) { p.Adds[0].Key = "" }},
		{"duplicate key", func(p *pb.StagePlanned) { p.Adds = append(p.Adds, p.Adds[0]) }},
		{"nil modify", func(p *pb.StagePlanned) { p.Modifies = []*pb.StageChannelNeed{nil} }},
		{"nil addition", func(p *pb.StagePlanned) { p.Adds = []*pb.StageNewChannel{nil} }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := plan()
			tc.change(p)
			if err := ValidatePlan(in, p); err == nil {
				t.Fatal("accepted invalid plan")
			}
		})
	}
	in.Channels = []uint32{65536}
	if err := ValidateShape(in); err == nil {
		t.Fatal("truncated channel index")
	}
	in.Channels = make([]uint32, 65536)
	for i := range in.Channels {
		in.Channels[i] = uint32(i)
	}
	if err := ValidateShape(in); err != nil {
		t.Fatal("rejected full u16 range", err)
	}
}
