module github.com/focolab/vizi-plugin-sdk/examples/tetra_shape/go

go 1.25.0

require (
	github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk v0.0.0
	google.golang.org/grpc v1.83.0
	google.golang.org/protobuf v1.36.12
)

require (
	golang.org/x/net v0.55.0 // indirect
	golang.org/x/sys v0.45.0 // indirect
	golang.org/x/text v0.37.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260526163538-3dc84a4a5aaa // indirect
)

replace github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk => ../../../polyglot/go-sdk
