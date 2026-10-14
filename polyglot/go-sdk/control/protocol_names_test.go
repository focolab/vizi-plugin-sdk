package control

import (
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/reflect/protoregistry"
)

func TestPublishedServiceName(t *testing.T) {
	// A peer using the same generated bindings would also pass a handshake
	// with an obsolete service path. Pin the published path independently.
	if got := pb.Plugin_Session_FullMethodName; got != "/vizi.plugin.v1.Plugin/Session" {
		t.Fatalf("session path = %q", got)
	}
	if got := pb.Plugin_ServiceDesc.ServiceName; got != "vizi.plugin.v1.Plugin" {
		t.Fatalf("registered service = %q", got)
	}
	if got := pb.File_vizi_plugin_proto.Package(); got != "vizi.plugin.v1" {
		t.Fatalf("descriptor package = %q", got)
	}
}

func TestRepeatedFieldBoundsResolve(t *testing.T) {
	for name := range repeatedFieldBounds {
		d, err := protoregistry.GlobalFiles.FindDescriptorByName(name)
		if err != nil {
			t.Errorf("bound %s does not resolve: %v", name, err)
			continue
		}
		f, ok := d.(protoreflect.FieldDescriptor)
		if !ok || f.Cardinality() != protoreflect.Repeated {
			t.Errorf("bound %s must name a repeated field", name)
		}
	}
}
