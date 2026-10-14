package plugin

import (
	"context"
	"errors"
	"google.golang.org/protobuf/proto"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
)

func TestStageDeclarationAndPlanning(t *testing.T) {
	d := definition()
	calls := 0
	d.Stage(StageSpec{ID: "crop"}, StageHandlers{Plan: func(_ context.Context, _ *control.Client, q *pb.PlanStage) (*pb.StagePlanned, error) {
		calls++
		return &pb.StagePlanned{Output: &pb.StageShape{Z: q.Input.Z / 2, Y: q.Input.Y, X: q.Input.X, Dtype: pb.DType_DTYPE_F32}}, nil
	}})
	a := buildDefinition(t, d)
	b, err := a.Manifest("./plugin")
	if err != nil {
		t.Fatal(err)
	}
	m := decodeManifest(t, b)
	if len(m.Contributions.Stages) != 1 || m.Contributions.Stages[0].Name != "crop" {
		t.Fatal(m.Contributions.Stages)
	}
	p, err := a.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	request := &pb.PlanStage{StageId: "crop", Input: &pb.StageShape{Channels: []uint32{65535}, Z: 4, Y: 3, X: 2, Dtype: pb.DType_DTYPE_U8}}
	invoke := func() (*pb.PluginMsg, error) {
		return p.Control.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_PlanStage{PlanStage: request}})
	}
	out, err := invoke()
	if err != nil || out.GetStagePlanned().GetOutput().GetZ() != 2 || calls != 1 {
		t.Fatal(out, err, calls)
	}
	request.Input.Channels = []uint32{65536}
	_, err = invoke()
	var hostErr *control.HostError
	if !errors.As(err, &hostErr) || hostErr.Kind != pb.Error_PROTOCOL || calls != 1 {
		t.Fatal("invalid input reached author", err, calls)
	}
	request.Input.Channels = []uint32{0}
	request.StageId = "absent"
	_, err = invoke()
	if !errors.As(err, &hostErr) || hostErr.Kind != pb.Error_NOT_FOUND {
		t.Fatal(err)
	}

	// The plan's output is checked against the offered snapshot, even if a
	// callback mutates its request while computing its answer.
	cfg, err := (Handlers{Stages: map[string]StageHandlers{"bad": {Plan: func(_ context.Context, _ *control.Client, q *pb.PlanStage) (*pb.StagePlanned, error) {
		q.Input.Z = 100
		return &pb.StagePlanned{Output: q.Input}, nil
	}}}}).Configure(control.Config{})
	if err != nil {
		t.Fatal(err)
	}
	request.StageId = "bad"
	_, err = cfg.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_PlanStage{PlanStage: request}})
	if !errors.As(err, &hostErr) || hostErr.Kind != pb.Error_INTERNAL {
		t.Fatal("growing plan accepted", err)
	}
}

func TestStageDeclarationRejectsMissingPlanAndDuplicates(t *testing.T) {
	d := definition()
	d.Stage(StageSpec{ID: "a"}, StageHandlers{})
	if _, err := d.Build(); err == nil {
		t.Fatal("missing Plan accepted")
	}
	d = definition()
	h := StageHandlers{Plan: func(context.Context, *control.Client, *pb.PlanStage) (*pb.StagePlanned, error) { return nil, nil }}
	d.Stage(StageSpec{ID: "a"}, h)
	d.Stage(StageSpec{ID: "a"}, h)
	if _, err := d.Build(); err == nil {
		t.Fatal("duplicate stage accepted")
	}
}

func TestStagePlanningErrorKinds(t *testing.T) {
	for _, tc := range []struct {
		name  string
		input func(*pb.StageShape)
		plan  func(*pb.StagePlanned)
		kind  pb.Error_Kind
	}{
		{name: "input dtype", input: func(s *pb.StageShape) { s.Dtype = 99 }, kind: pb.Error_PROTOCOL},
		{name: "input index", input: func(s *pb.StageShape) { s.Channels = []uint32{65536} }, kind: pb.Error_PROTOCOL},
		{name: "output dtype", plan: func(p *pb.StagePlanned) { p.Output.Dtype = 99 }, kind: pb.Error_INTERNAL},
		{name: "read-only index", plan: func(p *pb.StagePlanned) { p.Reads = []uint32{65536} }, kind: pb.Error_PROTOCOL},
		{name: "read-only absent", plan: func(p *pb.StagePlanned) { p.Reads = []uint32{1} }, kind: pb.Error_INTERNAL},
		{name: "zero output axis", plan: func(p *pb.StagePlanned) { p.Output.X = 0 }, kind: pb.Error_INTERNAL},
		{name: "growing", plan: func(p *pb.StagePlanned) { p.Output.X++ }, kind: pb.Error_INTERNAL},
		{name: "missing dependency", plan: func(p *pb.StagePlanned) { p.Modifies = []*pb.StageChannelNeed{{Channel: 0, Needs: []uint32{1}}} }, kind: pb.Error_INTERNAL},
		{name: "undeclared modified input", plan: func(p *pb.StagePlanned) { p.Modifies = []*pb.StageChannelNeed{{Channel: 1}} }, kind: pb.Error_INTERNAL},
		{name: "output channel bound", plan: func(p *pb.StagePlanned) { p.Output.Channels = []uint32{65536} }, kind: pb.Error_PROTOCOL},
		{name: "modified channel bound", plan: func(p *pb.StagePlanned) { p.Modifies = []*pb.StageChannelNeed{{Channel: 65536}} }, kind: pb.Error_PROTOCOL},
		{name: "addition dependency bound", plan: func(p *pb.StagePlanned) { p.Adds = []*pb.StageNewChannel{{Key: "added", Needs: []uint32{65536}}} }, kind: pb.Error_PROTOCOL},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			cfg, err := (Handlers{Stages: map[string]StageHandlers{"s": {Plan: func(_ context.Context, _ *control.Client, q *pb.PlanStage) (*pb.StagePlanned, error) {
				calls++
				p := &pb.StagePlanned{Output: proto.Clone(q.Input).(*pb.StageShape)}
				if tc.plan != nil {
					tc.plan(p)
				}
				return p, nil
			}}}}).Configure(control.Config{})
			if err != nil {
				t.Fatal(err)
			}
			input := &pb.StageShape{Channels: []uint32{0}, Dtype: pb.DType_DTYPE_U8, Z: 1, Y: 1, X: 1}
			if tc.input != nil {
				tc.input(input)
			}
			_, err = cfg.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: input}}})
			var hostErr *control.HostError
			if !errors.As(err, &hostErr) || hostErr.Kind != tc.kind {
				t.Fatal(err, "want", tc.kind)
			}
			if tc.input != nil && calls != 0 {
				t.Fatal("invalid input reached author")
			}
		})
	}
	// An author's declared refusal uses the existing HostError contract unchanged.
	want := &control.HostError{Kind: pb.Error_UNSUPPORTED, Message: "requires two channels"}
	cfg, err := (Handlers{Stages: map[string]StageHandlers{"s": {Plan: func(context.Context, *control.Client, *pb.PlanStage) (*pb.StagePlanned, error) { return nil, want }}}}).Configure(control.Config{})
	if err != nil {
		t.Fatal(err)
	}
	_, err = cfg.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: &pb.StageShape{Dtype: pb.DType_DTYPE_U8}}}})
	if err != want {
		t.Fatal("author refusal kind lost", err)
	}
}

func TestStageParametersHaveOneDeclaration(t *testing.T) {
	params := []StageParam{{Key: "mode", Type: "choice", Value: "u8", Options: []string{"u8", "f32"}}, {Key: "text", Type: "string", Value: "é", MaxLength: 2}}
	d := definition()
	calls := 0
	handler := StageHandlers{PlanWithParams: func(_ context.Context, _ *control.Client, q *pb.PlanStage, params stage.Params) (*pb.StagePlanned, error) {
		calls++
		if params["text"] != "é" {
			return nil, errors.New("string changed")
		}
		out := proto.Clone(q.Input).(*pb.StageShape)
		if params["mode"].(string) == "f32" {
			out.Dtype = pb.DType_DTYPE_F32
		}
		return &pb.StagePlanned{Output: out}, nil
	}}
	d.Stage(StageSpec{ID: "s", Params: params}, handler)
	params[0].Options[1] = "changed"
	params[1].MaxLength = 1
	a := buildDefinition(t, d)
	b, err := a.Manifest("./plugin")
	if err != nil {
		t.Fatal(err)
	}
	m := decodeManifest(t, b)
	if got := m.Contributions.Stages[0].Params[0]; got.Options[1] != "f32" || got.Value != "u8" {
		t.Fatal(got)
	}
	if got := m.Contributions.Stages[0].Params[1]; got.MaxLength != 2 || got.Value != "é" {
		t.Fatal(got)
	}
	cfg, err := a.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	q := &pb.PlanStage{StageId: "s", Input: &pb.StageShape{Z: 1, Y: 1, X: 1, Dtype: pb.DType_DTYPE_U8}}
	call := func() (*pb.PluginMsg, error) {
		return cfg.Control.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_PlanStage{PlanStage: q}})
	}
	_, err = call()
	var hostErr *control.HostError
	if !errors.As(err, &hostErr) || hostErr.Kind != pb.Error_INVALID_ARGUMENT || calls != 0 {
		t.Fatal(err, calls)
	}
	q.Params = map[string]*pb.Value{"mode": {Kind: &pb.Value_S{S: "f32"}}}
	q.Params["text"] = &pb.Value{Kind: &pb.Value_S{S: "é"}}
	out, err := call()
	if err != nil || out.GetStagePlanned().GetOutput().GetDtype() != pb.DType_DTYPE_F32 || calls != 1 {
		t.Fatal(out, err, calls)
	}
	for _, double := range []bool{false, true} {
		d := definition()
		h := handler
		if double {
			h.Params = params
		} else {
			h.Plan = func(context.Context, *control.Client, *pb.PlanStage) (*pb.StagePlanned, error) { return nil, nil }
		}
		d.Stage(StageSpec{ID: "s", Params: params}, h)
		if _, err := d.Build(); err == nil {
			t.Fatal("ambiguous stage declaration accepted")
		}
	}
}
