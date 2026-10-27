package plugin

import (
	"errors"
	"math"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestNumberAcceptsExactlyRepresentableIntegers(t *testing.T) {
	for _, value := range []int64{0, 3, -3, 1<<53 - 1, 1 << 53, 1<<53 + 2, -(1<<53 + 2), 1 << 60, math.MinInt64, math.MaxInt64 - 1023} {
		got, err := (Args{"value": Int(value)}).Number("value")
		if err != nil || got != float64(value) {
			t.Fatalf("Number(%d) = %v, %v", value, got, err)
		}
	}
	for _, value := range []int64{1<<53 + 1, -(1<<53 + 1), 1<<60 + 1, math.MaxInt64, math.MinInt64 + 1} {
		args := Args{"value": Int(value)}
		_, err := args.Number("value")
		var hostError *control.HostError
		if !errors.As(err, &hostError) || hostError.Kind != pb.Error_INVALID_ARGUMENT || !strings.Contains(hostError.Message, "value") {
			t.Fatalf("Number(%d) should refuse rounding: %v", value, err)
		}
		if got, err := args.Int("value"); err != nil || got != value {
			t.Fatal("Int must still return the exact original value", got, err)
		}
	}
}

func TestNumberPreservesFloatBitsAndRejectsOtherTypes(t *testing.T) {
	for _, value := range []float64{0, 3.0, 3.5, math.Copysign(0, -1), math.Inf(1), math.Inf(-1), math.Float64frombits(0x7ff8000000000012)} {
		got, err := (Args{"value": Float(value)}).Number("value")
		if err != nil || math.Float64bits(got) != math.Float64bits(value) {
			t.Fatalf("Number(%v) changed the floating-point value: %v, %v", value, got, err)
		}
	}
	for _, args := range []Args{nil, {}, {"value": nil}, {"value": {}}, {"value": Bool(true)}, {"value": String("3")}} {
		_, err := args.Number("value")
		var hostError *control.HostError
		if !errors.As(err, &hostError) || hostError.Kind != pb.Error_INVALID_ARGUMENT {
			t.Fatalf("accepted absent/nonnumeric argument %v: %v", args, err)
		}
	}
	if _, err := (Args{"value": Int(3)}).Float("value"); err == nil {
		t.Fatal("Float must remain strict")
	}
	if _, err := (Args{"value": Float(3)}).Int("value"); err == nil {
		t.Fatal("Int must remain strict")
	}
}
