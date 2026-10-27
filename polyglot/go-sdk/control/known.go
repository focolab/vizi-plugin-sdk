package control

import (
	"fmt"
	"sort"
	"strconv"
	"strings"

	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// knownOnly returns a copy of m without the fields this build does not know,
// at every depth. An arrangement's content and a plan's identity are the
// fields the receiver knows (control_plane_protocol.md §5.5 rule 4): a field
// a later minor adds is skipped, so a restatement that differs only by one is
// the same content, and an arm that differs from its plan only by one matches.
func knownOnly[M proto.Message](m M) M {
	c := proto.Clone(m).(M)
	discardUnknown(c.ProtoReflect())
	return c
}

func discardUnknown(m protoreflect.Message) {
	if !m.IsValid() {
		return
	}
	m.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		switch {
		case fd.IsMap():
			if fd.MapValue().Message() != nil {
				v.Map().Range(func(_ protoreflect.MapKey, e protoreflect.Value) bool {
					discardUnknown(e.Message())
					return true
				})
			}
		case fd.IsList():
			if fd.Message() != nil {
				l := v.List()
				for i := 0; i < l.Len(); i++ {
					discardUnknown(l.Get(i).Message())
				}
			}
		case fd.Message() != nil:
			discardUnknown(v.Message())
		}
		return true
	})
	if len(m.GetUnknown()) > 0 {
		m.SetUnknown(nil)
	}
}

// sameKnown reports whether a and b carry the same known fields.
func sameKnown[M proto.Message](a, b M) bool {
	return proto.Equal(knownOnly(a), knownOnly(b))
}

// unknownOneofArm names the first oneof at or under m that is unset while its
// message carries fields this build does not know: a later minor's arm in
// place of a known one, as "Value.kind (field 7)". It returns "" when there is
// none. A StageArm whose input or params hold one cannot be used, and is
// refused like one naming an unknown dtype.
func unknownOneofArm(m protoreflect.Message) string {
	if !m.IsValid() {
		return ""
	}
	if raw := m.GetUnknown(); len(raw) > 0 {
		oneofs := m.Descriptor().Oneofs()
		for i := 0; i < oneofs.Len(); i++ {
			o := oneofs.Get(i)
			if o.IsSynthetic() || m.WhichOneof(o) != nil {
				continue
			}
			var nums []string
			for b := raw; len(b) > 0; {
				num, _, n := protowire.ConsumeField(b)
				if n < 0 {
					break
				}
				nums = append(nums, strconv.Itoa(int(num)))
				b = b[n:]
			}
			return fmt.Sprintf("%s.%s (field %s)", m.Descriptor().Name(), o.Name(), strings.Join(nums, ", "))
		}
	}
	found := ""
	m.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		switch {
		case fd.IsMap():
			if fd.MapValue().Message() != nil {
				var keys []protoreflect.MapKey
				v.Map().Range(func(k protoreflect.MapKey, _ protoreflect.Value) bool {
					keys = append(keys, k)
					return true
				})
				sort.Slice(keys, func(i, j int) bool { return keys[i].String() < keys[j].String() })
				for _, k := range keys {
					if s := unknownOneofArm(v.Map().Get(k).Message()); s != "" {
						found = fmt.Sprintf("%s[%q] %s", fd.Name(), k.String(), s)
						break
					}
				}
			}
		case fd.IsList():
			if fd.Message() != nil {
				for i := 0; i < v.List().Len() && found == ""; i++ {
					if s := unknownOneofArm(v.List().Get(i).Message()); s != "" {
						found = fmt.Sprintf("%s[%d] %s", fd.Name(), i, s)
					}
				}
			}
		case fd.Message() != nil:
			if s := unknownOneofArm(v.Message()); s != "" {
				found = fmt.Sprintf("%s %s", fd.Name(), s)
			}
		}
		return found == ""
	})
	return found
}
