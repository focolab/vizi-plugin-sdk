package protocol

import (
	"google.golang.org/protobuf/proto"
	"testing"
)

func TestChannelNameSourceAcrossMessages(t *testing.T) {
	for _, source := range []ChannelNameSource{0, 1, 2, 3, 987} {
		c := &ChannelName{Index: 7, Name: "channel", Source: source}
		for _, message := range []proto.Message{&Geometry{Channels: []*ChannelInfo{c}}, &ViewState{Channels: []*ChannelInfo{c}}, &OpenExport{Channels: []*ChannelInfo{c}}} {
			encoded, err := proto.Marshal(message)
			if err != nil {
				t.Fatal(err)
			}
			decoded := message.ProtoReflect().New().Interface()
			if err = proto.Unmarshal(encoded, decoded); err != nil {
				t.Fatal(err)
			}
			got := decoded.(interface{ GetChannels() []*ChannelInfo }).GetChannels()[0]
			if got.Index != 7 || got.Name != "channel" || got.Source != source || got.Declared() != (source == 1 || source == 3) {
				t.Fatal(got)
			}
			if source == 987 && got.Source.String() != "987" {
				t.Fatal("unknown source lost", got)
			}
		}
	}
}
