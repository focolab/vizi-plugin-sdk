package peakroi

import (
	"context"
	"errors"
	"fmt"
	"math"
	"runtime"
	"sync"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// Slice/address arithmetic bounds, not a volume-size policy.
const maxAddressBytes = int(^uint(0) >> 1)
const maxFloat64Values = maxAddressBytes / 8

// control_plane_protocol.md §9 bounds pose_translation at 1,048,576 scalars;
// the contribution needs three scalars per frame.
const maxPoseFrames = (1 << 20) / 3

func validateExtents(extent [3]float64) error {
	for a, e := range extent {
		if e <= 0 || math.IsNaN(e) || math.IsInf(e, 0) {
			return fmt.Errorf("%s extent must be finite and strictly positive", []string{"Z", "Y", "X"}[a])
		}
	}
	return validateKernelExtents(extent)
}

// Zero sigma is defined for the numerical kernel, but a zero ROI semi-axis
// is singular and must be rejected by the application input validation.
func validateKernelExtents(extent [3]float64) error {
	for a, e := range extent {
		if math.IsNaN(e) || math.IsInf(e, 0) || e < 0 || math.Ceil(1.5*e) >= float64((maxFloat64Values-1)/2) {
			return fmt.Errorf("%s extent must be finite, nonnegative, and its Gaussian kernel must fit an addressable float64 slice", []string{"Z", "Y", "X"}[a])
		}
	}
	return nil
}

func decodeAsFloat64[T payload.Scalar](tensor *payload.Tensor) ([]float64, error) {
	values, err := payload.Decode[T](tensor)
	if err != nil {
		return nil, err
	}
	out := make([]float64, len(values))
	for i, v := range values {
		out[i] = float64(v)
	}
	return out, nil
}

func intensities(tensor *payload.Tensor) ([]float64, error) {
	if tensor == nil {
		return nil, errors.New("missing volume tensor")
	}
	size, err := payload.ByteLen(tensor.Shape(), pb.DType_DTYPE_F64)
	if err != nil || len(tensor.Shape()) != 3 || size > uint64(maxAddressBytes) {
		return nil, errors.New("volume must have three positive dimensions and an addressable float64 workspace")
	}
	switch tensor.DType() {
	case pb.DType_DTYPE_U8:
		return decodeAsFloat64[uint8](tensor)
	case pb.DType_DTYPE_U16:
		return decodeAsFloat64[uint16](tensor)
	case pb.DType_DTYPE_F32:
		return decodeAsFloat64[float32](tensor)
	case pb.DType_DTYPE_F64:
		return payload.Decode[float64](tensor)
	default:
		return nil, fmt.Errorf("unsupported volume dtype: %v", tensor.DType())
	}
}

func gaussian(sigma float64) []float64 {
	radius := int(math.Ceil(3 * sigma))
	kernel := make([]float64, 2*radius+1)
	total := 0.0
	for i := -radius; i <= radius; i++ {
		d := float64(i) / sigma
		v := math.Exp(-0.5 * d * d)
		kernel[i+radius] = v
		total += v
	}
	for i := range kernel {
		kernel[i] /= total
	}
	return kernel
}

// blur takes ownership of values and applies the specified Z, Y, X passes.
func blur(ctx context.Context, values []float64, shape [3]int, extent [3]float64) ([]float64, error) {
	if err := validateKernelExtents(extent); err != nil {
		return nil, err
	}
	n := 1
	for _, d := range shape {
		if d <= 0 || d > maxFloat64Values/n {
			return nil, errors.New("invalid or oversized volume shape")
		}
		n *= d
	}
	if len(values) != n {
		return nil, errors.New("volume length does not match shape")
	}
	minimum := math.Inf(1)
	for i, v := range values {
		if i%1024 == 0 && ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if !math.IsInf(v, 0) && v < minimum {
			minimum = v // NaN also fails this comparison.
		}
	}
	if math.IsInf(minimum, 1) {
		return nil, errors.New("volume contains no finite intensity")
	}
	// Sanitize once, before any axis pass. Positive infinity remains a valid
	// maximum and propagates through taps with positive weight.
	for i, v := range values {
		if i%1024 == 0 && ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if math.IsNaN(v) || math.IsInf(v, -1) {
			values[i] = minimum
		}
	}
	strides := [3]int{shape[1] * shape[2], shape[2], 1}
	for axis, e := range extent {
		if e/2 == 0 {
			continue
		}
		if err := blurAxis(ctx, values, shape[axis], strides[axis], gaussian(e/2)); err != nil {
			return nil, err
		}
	}
	return values, ctx.Err()
}

// Each block owns independent axis lines. Save those lines, then accumulate
// taps over contiguous spans in place. This removes the second volume-sized
// float64 buffer and keeps tap accumulation in cache. Block size is only a
// working-set target: it never limits the volume or kernel accepted.
func blurAxis(ctx context.Context, values []float64, length, stride int, kernel []float64) error {
	block := min(stride, max(1, (32<<10)/length))
	blocksPerOuter := (stride + block - 1) / block
	tasks := len(values) / (length * stride) * blocksPerOuter
	workers := min(runtime.GOMAXPROCS(0), tasks)
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	var wg sync.WaitGroup
	var once sync.Once
	var failure error
	for worker := 0; worker < workers; worker++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			if err := ctx.Err(); err != nil {
				once.Do(func() { failure = err })
				return
			}
			saved := make([]float64, length*block)
			for task := worker; task < tasks; task += workers {
				if err := ctx.Err(); err != nil {
					once.Do(func() { failure = err })
					return
				}
				outer, inner := task/blocksPerOuter, task%blocksPerOuter*block
				width := min(block, stride-inner)
				base := outer*length*stride + inner
				for row := 0; row < length; row++ {
					copy(saved[row*width:(row+1)*width], values[base+row*stride:base+row*stride+width])
				}
				radius := len(kernel) / 2
				for row := 0; row < length; row++ {
					if err := ctx.Err(); err != nil {
						once.Do(func() { failure = err })
						return
					}
					dst := values[base+row*stride : base+row*stride+width]
					clear(dst)
					for tap, weight := range kernel {
						if weight == 0 {
							continue
						} // 0 * +Inf must not produce NaN.
						source := min(max(row+tap-radius, 0), length-1) * width
						src := saved[source : source+width]
						for i, v := range src {
							dst[i] += weight * v
						}
					}
					for _, v := range dst {
						if math.IsNaN(v) || math.IsInf(v, -1) {
							once.Do(func() { failure = errors.New("blur produced an invalid intensity"); cancel() })
							return
						}
					}
				}
			}
		}(worker)
	}
	wg.Wait()
	return failure
}

func findPeak(ctx context.Context, tensor *payload.Tensor, extent [3]float64) ([3]int, error) {
	var peak [3]int
	values, err := intensities(tensor)
	if err != nil {
		return peak, err
	}
	dims := tensor.Shape()
	shape := [3]int{int(dims[0]), int(dims[1]), int(dims[2])}
	values, err = blur(ctx, values, shape, extent)
	if err != nil {
		return peak, err
	}
	best := 0
	for i, v := range values {
		if i%1024 == 0 && ctx.Err() != nil {
			return peak, ctx.Err()
		}
		if v > values[best] { // Strict comparison preserves the first raster tie.
			best = i
		}
	}
	return [3]int{best / (shape[1] * shape[2]), best / shape[2] % shape[1], best % shape[2]}, nil
}
