package plugin

import (
	"errors"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/reflect/protoreflect"
)

func inlineItem(item *pb.OverlayItem) error { return inlineMessage(item.ProtoReflect()) }

func inlineMessage(m protoreflect.Message) (err error) {
	if !m.IsValid() {
		return errors.New("nil geometry submessage")
	}
	if len(m.GetUnknown()) != 0 {
		return errors.New("unknown geometry fields require the raw control API")
	}
	switch m.Descriptor().FullName() {
	case "vizi.plugin.v1.SegmentRef", "vizi.plugin.v1.ImageRef":
		return errors.New("overlay helper does not transfer segment ownership")
	case "vizi.plugin.v1.GeomBuffer":
		return checkBuffer(m.Interface().(*pb.GeomBuffer))
	}
	m.Range(func(f protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		if f.Message() == nil {
			return true
		}
		if f.IsMap() {
			if f.MapValue().Message() != nil {
				v.Map().Range(func(_ protoreflect.MapKey, x protoreflect.Value) bool {
					err = inlineMessage(x.Message())
					return err == nil
				})
			}
			return err == nil
		}
		if f.IsList() {
			for i := 0; i < v.List().Len() && err == nil; i++ {
				err = inlineMessage(v.List().Get(i).Message())
			}
		} else {
			err = inlineMessage(v.Message())
		}
		return err == nil
	})
	return err
}
