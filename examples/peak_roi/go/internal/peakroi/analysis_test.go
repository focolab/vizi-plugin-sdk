package peakroi

import (
	"context"
	"math"
	"runtime"
	"slices"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestBlurClampedImpulse(t *testing.T) {
	// sigma=0.5, radius=2: an impulse at the left boundary is replicated
	// for all negative samples, rather than reflected, wrapped or zeroed.
	a, b := math.Exp(-2), math.Exp(-8)
	sum := 1 + 2*a + 2*b
	want := []float64{(1 + a + b) / sum, (a + b) / sum, b / sum}
	for axis := 0; axis < 3; axis++ {
		shape, extent := [3]int{1, 1, 1}, [3]float64{}
		shape[axis], extent[axis] = 3, 1
		got, err := blur(context.Background(), []float64{1, 0, 0}, shape, extent)
		if err != nil {
			t.Fatal(err)
		}
		for i := range want {
			if math.Abs(got[i]-want[i]) > 1e-14 {
				t.Fatalf("axis %d: got %v, want %v", axis, got, want)
			}
		}
	}
}

func TestBlurAgainstDirectConvolution(t *testing.T) {
	// An independent full 3D convolution checks strides and anisotropy.
	shape := [3]int{3, 4, 5}
	values := make([]float64, 60)
	for i := range values {
		values[i] = math.Sin(float64(i)) + float64(i%7)
	}
	extent := [3]float64{0.6, 1.1, 0.4}
	got, err := blur(context.Background(), slices.Clone(values), shape, extent)
	if err != nil {
		t.Fatal(err)
	}
	for z := 0; z < 3; z++ {
		for y := 0; y < 4; y++ {
			for x := 0; x < 5; x++ {
				sum, weights := 0.0, 0.0
				for dz := -1; dz <= 1; dz++ {
					for dy := -2; dy <= 2; dy++ {
						for dx := -1; dx <= 1; dx++ {
							w := math.Exp(-0.5 * (math.Pow(float64(dz)/0.3, 2) + math.Pow(float64(dy)/0.55, 2) + math.Pow(float64(dx)/0.2, 2)))
							zz, yy, xx := min(max(z+dz, 0), 2), min(max(y+dy, 0), 3), min(max(x+dx, 0), 4)
							sum += w * values[(zz*4+yy)*5+xx]
							weights += w
						}
					}
				}
				if math.Abs(got[(z*4+y)*5+x]-sum/weights) > 1e-12 {
					t.Fatalf("convolution mismatch at %d,%d,%d", z, y, x)
				}
			}
		}
	}
}

func TestPeakGaussianAndTie(t *testing.T) {
	values := make([]float64, 15*17*19)
	for z := 0; z < 15; z++ {
		for y := 0; y < 17; y++ {
			for x := 0; x < 19; x++ {
				values[(z*17+y)*19+x] = math.Exp(-float64((z-6)*(z-6)+(y-9)*(y-9)+(x-11)*(x-11)) / 4)
			}
		}
	}
	tensor, err := payload.Encode(values, []uint32{15, 17, 19}, true)
	if err != nil {
		t.Fatal(err)
	}
	p, err := findPeak(context.Background(), tensor, [3]float64{2, 3, 4})
	if err != nil || p != [3]int{6, 9, 11} {
		t.Fatalf("peak %v, err %v", p, err)
	}
	for _, extent := range [][3]float64{{}, {2, 4, 1}} {
		tensor, _ = payload.Encode([]uint16{42, 42, 42, 42, 42, 42, 42, 42}, []uint32{2, 2, 2}, true)
		p, err = findPeak(context.Background(), tensor, extent)
		if err != nil || p != [3]int{} {
			t.Fatalf("tie: %v, %v", p, err)
		}
	}
}

func TestDecodeDtypesAndByteOrder(t *testing.T) {
	for _, order := range []bool{false, true} {
		u8, _ := payload.Encode([]uint8{1, 255}, []uint32{1, 1, 2}, order)
		u16, _ := payload.Encode([]uint16{1, 50000}, []uint32{1, 1, 2}, order)
		f32, _ := payload.Encode([]float32{-4, 1.25}, []uint32{1, 1, 2}, order)
		f64, _ := payload.Encode([]float64{-4, 1.25}, []uint32{1, 1, 2}, order)
		for _, tensor := range []*payload.Tensor{u8, u16, f32, f64} {
			p, err := findPeak(context.Background(), tensor, [3]float64{})
			if err != nil || p != [3]int{0, 0, 1} {
				t.Fatalf("dtype %v: %v, %v", tensor.DType(), p, err)
			}
		}
	}
}

func TestAnalysisRefusals(t *testing.T) {
	for _, v := range []float64{0, math.Copysign(0, -1), -1, math.NaN(), math.Inf(1), 1e300} {
		if validateExtents([3]float64{v, 1, 1}) == nil {
			t.Fatalf("accepted extent %v", v)
		}
	}
	for _, values := range [][]float64{{math.NaN()}, {math.Inf(-1)}, {math.Inf(1)}, {math.NaN(), math.Inf(-1), math.Inf(1)}} {
		if _, err := blur(context.Background(), values, [3]int{1, 1, len(values)}, [3]float64{}); err == nil || !strings.Contains(err.Error(), "no finite") {
			t.Fatalf("expected no-finite-data refusal, got %v", err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := blur(ctx, []float64{1}, [3]int{1, 1, 1}, [3]float64{1, 1, 1}); err != context.Canceled {
		t.Fatal(err)
	}
	if _, err := blur(context.Background(), []float64{1}, [3]int{1, 2, 1}, [3]float64{}); err == nil {
		t.Fatal("accepted bad shape")
	}
	tensor, _ := payload.New([]byte{0}, []uint32{1}, pb.DType_DTYPE_U8, true)
	if _, err := intensities(tensor); err == nil {
		t.Fatal("accepted non-ZYX tensor")
	}
}

func TestNonFiniteReplacementBeforeBlur(t *testing.T) {
	// The negative finite minimum prevents a mistaken zero replacement from
	// turning an invalid voxel into the maximum. Check all axis passes too.
	dirty := []float64{math.NaN(), -5, -2, math.Inf(-1), -4, -3, -1, -5}
	clean := []float64{-5, -5, -2, -5, -4, -3, -1, -5}
	for _, extent := range [][3]float64{{}, {1, 2, 3}} {
		got, err := blur(context.Background(), slices.Clone(dirty), [3]int{2, 2, 2}, extent)
		if err != nil {
			t.Fatal(err)
		}
		want, err := blur(context.Background(), slices.Clone(clean), [3]int{2, 2, 2}, extent)
		if err != nil {
			t.Fatal(err)
		}
		if !slices.Equal(got, want) {
			t.Fatalf("got %v, want %v", got, want)
		}
	}
	tensor, err := payload.Encode([]float32{float32(math.NaN()), -3, -1, float32(math.Inf(-1))}, []uint32{1, 1, 4}, true)
	if err != nil {
		t.Fatal(err)
	}
	peak, err := findPeak(context.Background(), tensor, [3]float64{})
	if err != nil || peak != [3]int{0, 0, 2} {
		t.Fatalf("peak %v, error %v", peak, err)
	}
}

func TestPositiveInfinityPropagationAndTies(t *testing.T) {
	values := []float32{-1, -1, -1, -1, float32(math.Inf(1)), -1, -1, -1, -1}
	tensor, err := payload.Encode(values, []uint32{1, 1, 9}, true)
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		extent [3]float64
		want   int
	}{
		{[3]float64{}, 4},
		{[3]float64{1, 1, 1}, 2},          // radius 2: indices 2..6 tie at +Inf
		{[3]float64{0.02, 0.02, 0.02}, 4}, // off-centre taps underflow to zero
	} {
		peak, err := findPeak(context.Background(), tensor, tc.extent)
		if err != nil || peak != [3]int{0, 0, tc.want} {
			t.Fatalf("extent %v: peak %v, error %v", tc.extent, peak, err)
		}
	}
	got, err := blur(context.Background(), []float64{math.NaN(), -2, math.Inf(-1), math.Inf(1)}, [3]int{1, 1, 4}, [3]float64{})
	if err != nil || !slices.Equal(got, []float64{-2, -2, -2, math.Inf(1)}) {
		t.Fatalf("got %v, error %v", got, err)
	}
}

func TestBlockedBlurAcrossWorkersAndPartialBlocks(t *testing.T) {
	// More than one cache block on Z and Y, with a partial final block. A serial
	// per-voxel reference preserves tap order and catches overwrite/stride bugs.
	shape := [3]int{7, 37, 1001}
	extent := [3]float64{2, 3, 1}
	values := make([]float64, shape[0]*shape[1]*shape[2])
	for i := range values {
		values[i] = math.Sin(float64(i))
	}
	want := slices.Clone(values)
	strides := [3]int{shape[1] * shape[2], shape[2], 1}
	for axis, e := range extent {
		next := make([]float64, len(want))
		kernel := gaussian(e / 2)
		radius := len(kernel) / 2
		for i := range want {
			coordinate := i / strides[axis] % shape[axis]
			for tap, w := range kernel {
				sample := min(max(coordinate+tap-radius, 0), shape[axis]-1)
				next[i] += w * want[i+(sample-coordinate)*strides[axis]]
			}
		}
		want = next
	}
	for _, workers := range []int{1, 4} {
		previous := runtime.GOMAXPROCS(workers)
		got, err := blur(context.Background(), slices.Clone(values), shape, extent)
		runtime.GOMAXPROCS(previous)
		if err != nil {
			t.Fatal(err)
		}
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("workers=%d i=%d got=%g want=%g", workers, i, got[i], want[i])
			}
		}
	}
}

func TestLargeExtentUsesAddressBounds(t *testing.T) {
	if err := validateExtents([3]float64{3000, 1, 1}); err != nil {
		t.Fatal("old radius cap remains", err)
	}
	if _, err := blur(context.Background(), []float64{1}, [3]int{maxFloat64Values, 2, 1}, [3]float64{}); err == nil {
		t.Fatal("overflowing workspace accepted")
	}
}
