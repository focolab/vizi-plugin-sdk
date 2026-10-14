package control

import (
	"fmt"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

type RepeatedFieldError struct {
	Field string
	Limit int
}

func (e *RepeatedFieldError) Error() string {
	return fmt.Sprintf("%s exceeds %d entries", e.Field, e.Limit)
}

const MaxMessageBytes = 32 << 20
const MaxInlineBytes = 8 << 20
const MaxSeries = 1 << 22

// Keys are descriptor full names. TestRepeatedFieldBoundsResolve checks that
// every exception still names a repeated field after regenerating the IDL.
var repeatedFieldBounds = map[protoreflect.FullName]int{
	"vizi.plugin.v1.SeriesList.entries":                MaxSeries,
	"vizi.plugin.v1.TimeMapping.t":                     1048576,
	"pois_math.v1.Roi.pose_translation":                1048576,
	"pois_math.v1.Roi.pose_rotation":                   1048576,
	"pois_math.v1.Roi.pose_stretch":                    1048576,
	"vizi.plugin.v1.Geometry.levels":                   64,
	"vizi.plugin.v1.RoiPoseExtension.pose_translation": 1048576,
	"vizi.plugin.v1.RoiPoseExtension.pose_rotation":    1048576,
	"vizi.plugin.v1.RoiPoseExtension.pose_stretch":     1048576,
}

// ValidateSize checks both incoming and outgoing messages. Protobuf/gRPC also
// enforces the serialized message ceiling before decoding. Unknown fields are
// preserved for append safety and count toward that ceiling.
func ValidateSize(m proto.Message) error {
	if m == nil || !m.ProtoReflect().IsValid() {
		return fmt.Errorf("nil protocol message")
	}
	if size := proto.Size(m); size > MaxMessageBytes {
		return fmt.Errorf("message size %d bytes exceeds limit %d bytes (%d MiB)", size, MaxMessageBytes, MaxMessageBytes>>20)
	}
	return validateFields(m.ProtoReflect())
}

func validateFields(m protoreflect.Message) (err error) {
	m.Range(func(f protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		limit := 65536
		if bound, ok := repeatedFieldBounds[f.FullName()]; ok {
			limit = bound
		}
		check := func(v protoreflect.Value, k protoreflect.Kind) error {
			if (k == protoreflect.BytesKind && len(v.Bytes()) > MaxInlineBytes) ||
				(k == protoreflect.StringKind && len(v.String()) > MaxInlineBytes) {
				return fmt.Errorf("%s exceeds 8 MiB", f.FullName())
			}
			if k == protoreflect.MessageKind {
				return validateFields(v.Message())
			}
			return nil
		}
		switch {
		case f.IsMap():
			if v.Map().Len() > limit {
				err = &RepeatedFieldError{string(f.FullName()), limit}
				break
			}
			v.Map().Range(func(_ protoreflect.MapKey, x protoreflect.Value) bool {
				err = check(x, f.MapValue().Kind())
				return err == nil
			})
		case f.IsList():
			if v.List().Len() > limit {
				err = &RepeatedFieldError{string(f.FullName()), limit}
				break
			}
			for i := 0; i < v.List().Len() && err == nil; i++ {
				err = check(v.List().Get(i), f.Kind())
			}
		default:
			err = check(v, f.Kind())
		}
		return err == nil
	})
	return err
}
