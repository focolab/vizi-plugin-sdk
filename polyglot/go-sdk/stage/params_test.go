package stage

import (
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"math"
	"testing"
)

func TestParameterExactWireKindsAndBounds(t *testing.T) {
	for _, tc := range []struct {
		spec      manifest.StageParam
		good, bad *pb.Value
		want      any
	}{
		{manifest.StageParam{Key: "p", Type: "float", Value: 0, Min: 0, Max: 2}, &pb.Value{Kind: &pb.Value_F{F: 2}}, &pb.Value{Kind: &pb.Value_I{I: 2}}, float64(2)},
		{manifest.StageParam{Key: "p", Type: "int", Value: int64(math.MaxInt64), Min: int64(math.MaxInt64), Max: int64(math.MaxInt64)}, &pb.Value{Kind: &pb.Value_I{I: math.MaxInt64}}, &pb.Value{Kind: &pb.Value_I{I: math.MaxInt64 - 1}}, int64(math.MaxInt64)},
		{manifest.StageParam{Key: "p", Type: "bool", Value: false}, &pb.Value{Kind: &pb.Value_B{B: false}}, &pb.Value{Kind: &pb.Value_I{I: 0}}, false},
		{manifest.StageParam{Key: "p", Type: "choice", Value: "A", Options: []string{"A", ""}}, &pb.Value{Kind: &pb.Value_S{S: ""}}, &pb.Value{Kind: &pb.Value_S{S: "a"}}, ""},
		{manifest.StageParam{Key: "p", Type: "string", Value: "é", MaxLength: 2}, &pb.Value{Kind: &pb.Value_S{S: "é"}}, &pb.Value{Kind: &pb.Value_S{S: "é!"}}, "é"},
		{manifest.StageParam{Key: "p", Type: "string", Value: ""}, &pb.Value{Kind: &pb.Value_S{S: " --Run=É  "}}, &pb.Value{Kind: &pb.Value_I{I: 1}}, " --Run=É  "},
	} {
		t.Run(tc.spec.Type, func(t *testing.T) {
			specs := []manifest.StageParam{tc.spec}
			got, err := CheckParams(specs, map[string]*pb.Value{"p": tc.good})
			if err != nil || got["p"] != tc.want {
				t.Fatal(got, err)
			}
			for _, wire := range []map[string]*pb.Value{nil, {"p": nil}, {"p": {}}, {"other": tc.good}, {"p": tc.good, "extra": tc.good}, {"p": tc.bad}} {
				if _, err := CheckParams(specs, wire); err == nil {
					t.Fatal("invalid params accepted", wire)
				}
			}
		})
	}
	spec := []manifest.StageParam{{Key: "p", Type: "float", Value: 0}}
	for _, v := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		if _, err := CheckParams(spec, map[string]*pb.Value{"p": {Kind: &pb.Value_F{F: v}}}); err == nil {
			t.Fatal("nonfinite accepted", v)
		}
	}
}
