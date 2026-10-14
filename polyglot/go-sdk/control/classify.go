// Package control implements the plugin side of the reliable 1.0 session.
// The generated wire API lives in protocol. See README.md for lifecycle and
// resource ownership, and for the deliberately explicit integration hooks.
package control

import (
	"fmt"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/reflect/protoreflect"
)

type Class uint8

const (
	Unknown Class = iota
	Handshake
	Request
	ReplyClass
	Arrangement
	OneWay
	Push // one-shot push
	CoalescingPush
	HostCoalescing // Folded/applied in receive order; application callbacks run separately.
)

type route struct {
	class Class
	reply string
}

// Deliberately exhaustive against both published oneofs. A new IDL arm makes
// NewServer fail until its delivery and ownership contract has been reviewed.
var hostRoutes = map[string]route{
	"shape_drag": {HostCoalescing, ""}, "shape_commit": {OneWay, ""}, "shape_edit_ended": {OneWay, ""},
	"cancel_work":    {OneWay, ""},
	"follow_started": {OneWay, ""}, "follow_ended": {OneWay, ""},
	"view_request_result": {ReplyClass, ""},
	"plan_stage":          {Request, "stage_planned"}, "stage_arm": {Arrangement, ""},
	"chunks_wanted": {HostCoalescing, ""},
	"view_changed":  {HostCoalescing, ""}, "regions_changed": {HostCoalescing, ""},
	"save_path_result": {ReplyClass, ""}, "colormap_table": {ReplyClass, ""},
	"hello": {Handshake, ""}, "shutdown": {OneWay, ""},
	"open_reader": {Request, "reader_opened"}, "read_volume": {Request, "segment"},
	"read_plane": {Request, "segment"}, "metadata": {Request, "metadata"},
	"close_reader": {Request, "closed"}, "invoke_command": {Request, "command_done"},
	"open_export": {Request, "export_opened"}, "export_chunk": {Request, "chunk_written"},
	"finish_export": {Request, "export_done"}, "abort_export": {Request, "export_done"},
	"view_state": {ReplyClass, ""}, "data_volume": {ReplyClass, ""},
	"host_error": {ReplyClass, ""}, "rois": {ReplyClass, ""},
	"label_volume_compiled": {ReplyClass, ""}, "mask_voxels": {ReplyClass, ""},
	"tool_activation_changed": {ReplyClass, ""}, "asset_result": {ReplyClass, ""},
	"rois_contributed":   {ReplyClass, ""},
	"roi_inclusion":      {ReplyClass, ""},
	"roi_poses_extended": {ReplyClass, ""}, "request_status": {ReplyClass, ""},
	"install_segment": {Arrangement, ""}, "tool_binding": {Arrangement, ""},
	"epoch_change": {Arrangement, ""}, "time_mapping": {Arrangement, ""},
	"grant_state_query": {OneWay, ""}, "retire_segment": {OneWay, ""},
	"panel_opened": {OneWay, ""}, "doorbell_shutdown": {OneWay, ""},
	"release_loan": {Push, ""}, "contract_violation": {Push, ""},
}

var pluginRoutes = map[string]route{
	"shape_proposal": {CoalescingPush, ""}, "shape_answer": {Push, ""},
	"work_progress": {CoalescingPush, ""},
	"series_list":   {ReplyClass, ""},
	"offer_follow":  {CoalescingPush, ""}, "follow_state": {CoalescingPush, ""}, "end_follow": {Push, ""},
	"view_request":  {Request, "view_request_result"},
	"stage_planned": {ReplyClass, ""}, "stage_arm_ack": {Push, ""},
	"overlay_chunk": {CoalescingPush, ""}, "overlay_label_table": {CoalescingPush, ""},
	"ready": {Handshake, ""}, "reader_opened": {ReplyClass, ""},
	"segment": {ReplyClass, ""}, "metadata": {ReplyClass, ""}, "closed": {ReplyClass, ""},
	"error": {ReplyClass, ""}, "command_done": {ReplyClass, ""},
	"export_opened": {ReplyClass, ""}, "chunk_written": {ReplyClass, ""}, "export_done": {ReplyClass, ""},
	"request_save_path": {Request, "save_path_result"}, "get_colormap": {Request, "colormap_table"},
	"get_view_state": {Request, "view_state"}, "read_data_volume": {Request, "data_volume"},
	"read_roi_inclusion": {Request, "roi_inclusion"},
	"extend_roi_poses":   {Request, "roi_poses_extended"}, "request_status_query": {Request, "request_status"},
	"contribute_rois": {Request, "rois_contributed"}, "get_rois": {Request, "rois"},
	"compile_label_volume": {Request, "label_volume_compiled"}, "read_mask_voxels": {Request, "mask_voxels"},
	"activate_tool": {Request, "tool_activation_changed"}, "request_asset": {Request, "asset_result"},
	"panel_patch": {CoalescingPush, ""}, "overlay_update": {CoalescingPush, ""}, "overlay_remove": {Push, ""},
	"release_deposit": {Push, ""}, "grant_ack": {Push, ""}, "tool_binding_ack": {Push, ""},
	"epoch_ack": {Push, ""}, "time_mapping_ack": {Push, ""}, "grant_state": {Push, ""},
	"segment_released": {Push, ""}, "release_request": {Push, ""}, "doorbell_fault": {Push, ""},
	"shutdown_report": {Push, ""}, "doorbell_final_report": {Push, ""}, "contract_violation": {Push, ""}, "segment_shortfall": {CoalescingPush, ""},
}

func kind(m protoreflect.Message) string {
	f := m.WhichOneof(m.Descriptor().Oneofs().ByName("msg"))
	if f == nil {
		return ""
	}
	return string(f.Name())
}

func HostClass(m *pb.HostMsg) Class {
	if m == nil {
		return Unknown
	}
	return hostRoutes[kind(m.ProtoReflect())].class
}

func checkRoutes(m protoreflect.Message, routes map[string]route) error {
	fields := m.Descriptor().Oneofs().ByName("msg").Fields()
	if fields.Len() != len(routes) {
		return fmt.Errorf("%s routing table differs from published IDL", m.Descriptor().FullName())
	}
	for i := 0; i < fields.Len(); i++ {
		if routes[string(fields.Get(i).Name())].class == Unknown {
			return fmt.Errorf("unclassified IDL arm %s", fields.Get(i).FullName())
		}
	}
	return nil
}
