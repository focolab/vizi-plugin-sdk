package stage

import (
	"fmt"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// Params is an owned snapshot: float64 for float, int64 for int, bool for bool,
// and string for choice/string. The host supplies every key; defaults are never filled
// by the SDK. A callback may change its copy without affecting another turn.
type Params map[string]any

// CheckParams is shared by planning and arming. Declarations must already have
// passed manifest.ValidateStageParams. Wire kinds are exact, without coercion.
func CheckParams(declared []manifest.StageParam, wire map[string]*pb.Value) (Params, error) {
	if len(declared) != len(wire) {
		return nil, fmt.Errorf("stage params must contain exactly the declared keys")
	}
	out := make(Params, len(declared))
	for _, p := range declared {
		w, ok := wire[p.Key]
		if !ok || w == nil {
			return nil, fmt.Errorf("missing parameter %q", p.Key)
		}
		var v any
		switch k := w.Kind.(type) {
		case *pb.Value_F:
			if k != nil && p.Type == "float" {
				v = k.F
			}
		case *pb.Value_I:
			if k != nil && p.Type == "int" {
				v = k.I
			}
		case *pb.Value_B:
			if k != nil && p.Type == "bool" {
				v = k.B
			}
		case *pb.Value_S:
			if k != nil && (p.Type == "choice" || p.Type == "string") {
				v = k.S
			}
		}
		if v == nil {
			return nil, fmt.Errorf("parameter %q has wrong Value kind for %s", p.Key, p.Type)
		}
		if err := p.CheckValue(v); err != nil {
			return nil, fmt.Errorf("parameter %q: %w", p.Key, err)
		}
		out[p.Key] = v
	}
	return out, nil
}
