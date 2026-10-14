package control

import (
	"context"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// Typed helpers for resource-free host replies. Permission checks and application
// errors belong to the host and are returned as *HostError by Call.
// GetViewState exposes VisibleChannels, the ascending set the view composites.
// ViewState.C is deprecated and always 0; vizi has no active-channel control.
// DatasetSeries is nil for a single-image file; a nonnil pointer (even to "")
// names the reader's opaque series ID. Channels carries every channel's display
// name and source in index order, or is empty when no dataset is open.
func (c *Client) GetViewState(ctx context.Context) (*pb.ViewState, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_GetViewState{GetViewState: &pb.GetViewState{}}})
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return r.Message.GetViewState(), nil
}

// GetRois reads all regions, or just the supplied IDs. Missing IDs are omitted.
func (c *Client) GetRois(ctx context.Context, regionIDs ...string) (*pb.RoiList, error) {
	return c.getRois(ctx, &pb.GetRois{RegionIds: regionIDs})
}

// GetRoisWindow reads an inclusive pose window in the rest frame. The raw records
// retain pose_domain_t_start/end on every posed region, even in GetRois replies
// without a window. Compare them with roi.t_start/end to identify a partial span;
// writing back that partial span would truncate the other frames. Absent
// roi.t_start/end mean pose rows were cleared, not the entire domain. An oversized
// reply returns *HostError with OUT_OF_BOUNDS; callers can request fewer frames.
func (c *Client) GetRoisWindow(ctx context.Context, tStart, tEnd int32, regionIDs ...string) (*pb.RoiList, error) {
	return c.getRois(ctx, &pb.GetRois{PoseTStart: &tStart, PoseTEnd: &tEnd, RegionIds: regionIDs})
}
func (c *Client) getRois(ctx context.Context, request *pb.GetRois) (*pb.RoiList, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_GetRois{GetRois: request}})
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return r.Message.GetRois(), nil
}

// ContributeRois leaves store validation to the host, including finite position,
// direction, frame-basis and pose values, and pose domains inside [0,T). Host
// refusals are returned as *HostError with INVALID_ARGUMENT.
func (c *Client) ContributeRois(ctx context.Context, request *pb.ContributeRois) (*pb.RoisContributed, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_ContributeRois{ContributeRois: request}})
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return r.Message.GetRoisContributed(), nil
}
func (c *Client) CompileLabelVolume(ctx context.Context, request *pb.CompileLabelVolume) (*pb.LabelVolumeCompiled, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_CompileLabelVolume{CompileLabelVolume: request}})
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return r.Message.GetLabelVolumeCompiled(), nil
}
func (c *Client) ActivateTool(ctx context.Context, request *pb.ActivateTool) (*pb.ToolActivationChanged, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_ActivateTool{ActivateTool: request}})
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return r.Message.GetToolActivationChanged(), nil
}

// ExtendRoiPoses sends dense corrections/appends. The host validates the whole
// batch before writing, including ownership, finite rows, components and
// gap-free domains inside [0,T). Each pose component allows 1,048,576 values.
func (c *Client) ExtendRoiPoses(ctx context.Context, request *pb.ExtendRoiPoses) (*pb.RoiPosesExtended, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_ExtendRoiPoses{ExtendRoiPoses: request}})
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return r.Message.GetRoiPosesExtended(), nil
}

// RequestStatus queries host bookkeeping for a correlation ID from CallTracked.
// PENDING is not a completion promise; UNKNOWN is not evidence of failure.
func (c *Client) RequestStatus(ctx context.Context, subjectID string) (*pb.RequestStatus, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_RequestStatusQuery{RequestStatusQuery: &pb.RequestStatusQuery{SubjectId: subjectID}}})
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return r.Message.GetRequestStatus(), nil
}

// RequestAsset uses Config.PickerTimeout (default 30 minutes) as a dead-session
// backstop for a request answered by a person, not a work budget. A shorter caller
// context deadline still wins. An unsupervised host holds its command deadline
// during the picker; a supervised command has no deadline to hold.
// Calling and waiting inside a command is allowed. It holds the session's
// request slot while the person browses; plugins serving other work should use
// AfterReply or a background worker and return from the command promptly.
func (c *Client) RequestAsset(ctx context.Context, request *pb.RequestAsset) (*pb.AssetResult, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_RequestAsset{RequestAsset: request}})
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return r.Message.GetAssetResult(), nil
}

// Descriptor replies retain their Reply owner: defer Close, then copy/use the
// segment before returning. Shape, dtype and generation remain available on the
// generated message, without copying bulk data across the control plane.
type DataVolumeReply struct {
	*Reply
	Volume *pb.DataVolume
}
type MaskVoxelsReply struct {
	*Reply
	Voxels *pb.MaskVoxels
}
type RoiInclusionReply struct {
	*Reply
	Inclusion *pb.RoiInclusion
}

// ReadRoiInclusion asks the host for a region's level-0 voxel membership at T.
// Defer Close before using its optional deposit. Hidden regions (Present=false)
// and present regions covering zero voxels have no Segment and owe no release.
// Metadata is preserved even in those cases. Permission and rasterization errors
// are returned as *HostError. The host computes the posed shape and parent chain.
// Optional RoiMinZyx/RoiMaxZyx clip the answer in level-0 coordinates (inclusive
// min, exclusive max); both empty requests the full dataset. The host validates
// the clip box and returns INVALID_ARGUMENT or OUT_OF_BOUNDS as appropriate.
func (c *Client) ReadRoiInclusion(ctx context.Context, request *pb.ReadRoiInclusion) (*RoiInclusionReply, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_ReadRoiInclusion{ReadRoiInclusion: request}})
	if err != nil {
		return nil, err
	}
	return &RoiInclusionReply{r, r.Message.GetRoiInclusion()}, nil
}

func (c *Client) ReadDataVolume(ctx context.Context, request *pb.ReadDataVolume) (*DataVolumeReply, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_ReadDataVolume{ReadDataVolume: request}})
	if err != nil {
		return nil, err
	}
	return &DataVolumeReply{r, r.Message.GetDataVolume()}, nil
}
func (c *Client) ReadMaskVoxels(ctx context.Context, request *pb.ReadMaskVoxels) (*MaskVoxelsReply, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_ReadMaskVoxels{ReadMaskVoxels: request}})
	if err != nil {
		return nil, err
	}
	return &MaskVoxelsReply{r, r.Message.GetMaskVoxels()}, nil
}

// RequestSavePath asks for a destination; the host creates no file. It shares
// RequestAsset's one-picker-at-a-time lifecycle and Config.PickerTimeout (default
// 30 minutes): a dead-session backstop for a request answered by a person, not a
// work budget. A shorter caller context deadline still wins.
// SuggestedName must be a filename, not a path. Cancellation returns Declined.
// Calling from a command is allowed, with the same request-slot cost as
// RequestAsset; prefer AfterReply or a background worker when serving other work.
func (c *Client) RequestSavePath(ctx context.Context, request *pb.RequestSavePath) (*pb.SavePathResult, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_RequestSavePath{RequestSavePath: request}})
	if err != nil {
		return nil, err
	}
	defer r.Close()
	return r.Message.GetSavePathResult(), nil
}

// GetColormap returns the host's 256 RGBA entries (1024 bytes). No permission or
// open dataset is needed. Unknown names return HostError NOT_FOUND.
func (c *Client) GetColormap(ctx context.Context, name string) (*pb.ColormapTable, error) {
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_GetColormap{GetColormap: &pb.GetColormap{Name: name}}})
	if err != nil {
		return nil, err
	}
	defer r.Close()
	table := r.Message.GetColormapTable()
	if len(table.GetRgba()) != 1024 {
		return nil, &HostError{Kind: pb.Error_PROTOCOL, Message: "colormap table must contain 1024 RGBA bytes"}
	}
	return table, nil
}
