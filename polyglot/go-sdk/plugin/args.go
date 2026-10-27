package plugin

import (
	"fmt"
	"slices"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// Args is the command's typed argument map. Accessors reject missing/wrong
// types, preserving the difference between absent values and 0/false/empty.
// Int and Float read only their respective wire types. Number accepts either
// numeric type, converting integers only when float64 can represent them exactly.
type Args map[string]*pb.Value

func (a Args) Has(name string) bool { v := a[name]; return v != nil && v.Kind != nil }
func argError(name, want string) error {
	return &control.HostError{Kind: pb.Error_INVALID_ARGUMENT, Message: fmt.Sprintf("argument %q requires %s", name, want)}
}
func (a Args) String(name string) (string, error) {
	if v, ok := a[name].GetKind().(*pb.Value_S); ok {
		return v.S, nil
	}
	return "", argError(name, "string")
}
func (a Args) Int(name string) (int64, error) {
	if v, ok := a[name].GetKind().(*pb.Value_I); ok {
		return v.I, nil
	}
	return 0, argError(name, "integer")
}
func (a Args) Float(name string) (float64, error) {
	if v, ok := a[name].GetKind().(*pb.Value_F); ok {
		return v.F, nil
	}
	return 0, argError(name, "float")
}

// Number accepts either numeric wire type. Floating-point values are returned
// unchanged, including NaN and infinities, just as with Float. An integer that
// would be rounded during conversion returns INVALID_ARGUMENT; use Int to read
// such values losslessly. Exactly representable integers beyond 2^53 are valid.
func (a Args) Number(name string) (float64, error) {
	switch v := a[name].GetKind().(type) {
	case *pb.Value_F:
		return v.F, nil
	case *pb.Value_I:
		f := float64(v.I)
		// MaxInt64 rounds up to 2^63. Guard the conversion back to int64
		// before using it to check exactness; an out-of-range cast is not
		// portable. MinInt64 itself is exactly representable.
		if f < -0x1p63 || f >= 0x1p63 || int64(f) != v.I {
			return 0, argError(name, "an integer exactly representable as float64 (use Int for the original value)")
		}
		return f, nil
	default:
		return 0, argError(name, "an integer or float")
	}
}

func (a Args) Bool(name string) (bool, error) {
	if v, ok := a[name].GetKind().(*pb.Value_B); ok {
		return v.B, nil
	}
	return false, argError(name, "boolean")
}

func String(value string) *pb.Value { return &pb.Value{Kind: &pb.Value_S{S: value}} }
func Int(value int64) *pb.Value     { return &pb.Value{Kind: &pb.Value_I{I: value}} }
func Float(value float64) *pb.Value { return &pb.Value{Kind: &pb.Value_F{F: value}} }
func Bool(value bool) *pb.Value     { return &pb.Value{Kind: &pb.Value_B{B: value}} }

// Ints reads a signed integer list, retaining empty lists as a present value.
// The returned slice is independent of the command's protobuf storage.
func (a Args) Ints(name string) ([]int64, error) {
	if v, ok := a[name].GetKind().(*pb.Value_Ints); ok {
		return slices.Clone(v.Ints.GetValues()), nil
	}
	return nil, argError(name, "integer list")
}

// Ints constructs a Value.ints, copying the supplied values.
func Ints(values ...int64) *pb.Value {
	return &pb.Value{Kind: &pb.Value_Ints{Ints: &pb.IntList{Values: slices.Clone(values)}}}
}

// Strings reads a string list (Value.strings), retaining an empty list as a
// present value: a selectable table's "rows". The returned slice is
// independent of the command's protobuf storage.
func (a Args) Strings(name string) ([]string, error) {
	if v, ok := a[name].GetKind().(*pb.Value_Strings); ok {
		return slices.Clone(v.Strings.GetValues()), nil
	}
	return nil, argError(name, "string list")
}

// Strings constructs a Value.strings, copying the supplied values: a
// multiple table's selection for Panel.SetValue.
func Strings(values ...string) *pb.Value {
	return &pb.Value{Kind: &pb.Value_Strings{Strings: &pb.StringList{Values: slices.Clone(values)}}}
}
