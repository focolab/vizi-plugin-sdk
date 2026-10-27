// Package manifest describes and validates generated vizi-plugin.json files.
// It validates typed declarations, not arbitrary JSON documents. The normative
// schema is docs/spec/manifest_schema.md. Unknown future JSON fields must not be
// round-tripped through these types as a way of validating an existing manifest.
package manifest

import (
	"encoding/json"
	"errors"
	"strconv"
	"strings"
)

type Manifest struct {
	ManifestVersion int     `json:"manifest_version"`
	ID              string  `json:"id"`
	Name            string  `json:"name"`
	Version         string  `json:"version"`
	Engines         Engines `json:"engines"`
	// MinVizi is the oldest vizi application release the plugin runs in, a
	// PEP 440 version ("1.0.0"); empty omits it.
	MinVizi string `json:"min_vizi,omitempty"`
	// SDKVersion is the SDK release that built the plugin; the plugin
	// package writes control.SDKVersion here.
	SDKVersion    string        `json:"sdk_version,omitempty"`
	Entry         Entry         `json:"entry"`
	Contributions Contributions `json:"contributions"`
	Assets        []Asset       `json:"assets,omitempty"`
	Permissions   []string      `json:"permissions,omitempty"`
	Subscriptions []string      `json:"subscriptions,omitempty"`
}
type Engines struct {
	Vizi string `json:"vizi"`
}
type Entry struct {
	Command []string `json:"command"`
	// StartupSeconds is an optional advisory estimate from spawn to Ready.
	// Hosts may honor, cap or ignore it; nil omits the estimate.
	StartupSeconds *float64 `json:"startup_s,omitempty"`
}
type RegionType struct {
	ID   string `json:"id"`
	Name string `json:"name,omitempty"`
}

type Contributions struct {
	RegionTypes []RegionType `json:"region_types,omitempty"`
	Readers     []Reader     `json:"readers,omitempty"`
	Commands    []Command    `json:"commands,omitempty"`
	Panels      []Panel      `json:"panels,omitempty"`
	Exporters   []Exporter   `json:"exporters,omitempty"`
	Overlays    []Overlay    `json:"overlays,omitempty"`
	Tools       []Tool       `json:"tools,omitempty"`
	Stages      []Stage      `json:"stages,omitempty"`
}

// Stage declares a processing contribution; the host defaults an omitted Name to ID.
type Stage struct {
	ID     string       `json:"id"`
	Name   string       `json:"name,omitempty"`
	Params []StageParam `json:"params,omitempty"`
}

// MenuTools lists a contribution in the Tools menu. Nil hides it.
func MenuTools() *string { s := "tools"; return &s }

type Reader struct {
	ID                 string   `json:"id"`
	Name               string   `json:"name,omitempty"`
	Extensions         []string `json:"extensions,omitempty"`
	FallbackExtensions []string `json:"fallback_extensions,omitempty"`
}
type Command struct {
	ID                string      `json:"id"`
	Title             string      `json:"title"`
	Description       string      `json:"description,omitempty"`
	DefaultKeybinding string      `json:"default_keybinding,omitempty"`
	Menu              *string     `json:"menu"`
	Pane              *PaneButton `json:"pane,omitempty"`
}
type Panel struct {
	FollowTargets []string `json:"follow_targets,omitempty"`
	ViewTargets   []string `json:"view_targets,omitempty"`
	ID            string   `json:"id"`
	Title         string   `json:"title"`
	MenuLabel     string   `json:"menu_label,omitempty"`
	Root          Widget   `json:"root"`
}

// Widget is the serialized tree. The plugin package offers constructors that
// bind interactive widgets to registered CommandRefs instead of string IDs.
// Value is nil, a string, bool, finite number or channel-index list, according to Kind.
type Widget struct {
	textFormatNull   bool
	MovesView        bool     `json:"moves_view,omitempty"`
	FollowsSelection bool     `json:"follows_selection,omitempty"`
	Filter           *string  `json:"filter,omitempty"`
	Kind             string   `json:"kind"`
	ID               string   `json:"id"`
	Text             string   `json:"text,omitempty"`
	TextFormat       *string  `json:"text_format,omitempty"` // label only; nil selects plain
	Command          string   `json:"command,omitempty"`
	Min              *float64 `json:"min,omitempty"`
	Max              *float64 `json:"max,omitempty"`
	Step             *float64 `json:"step,omitempty"`
	Value            any      `json:"value,omitempty"`
	Float            bool     `json:"float,omitempty"`
	Multiple         *bool    `json:"multiple,omitempty"` // channel_picker, selectable table; nil selects false
	Indeterminate    bool     `json:"indeterminate,omitempty"`
	Options          []string `json:"options,omitempty"`
	Placeholder      string   `json:"placeholder,omitempty"`
	Children         []Widget `json:"children,omitempty"`
	Row              []Widget `json:"row,omitempty"`
	MaxRows          *int     `json:"max_rows,omitempty"`     // list only; nil selects 256
	VisibleRows      *int     `json:"visible_rows,omitempty"` // list, table; nil selects 8
	Selectable       *bool    `json:"selectable,omitempty"`   // list, table; nil selects false
	// Columns are a table's, 1 to TableMaxColumns of them, in cell order.
	Columns []TableColumn `json:"columns,omitempty"`
	// Sortable lets a header click sort a table's rows on the host's side;
	// nil selects false.
	Sortable *bool `json:"sortable,omitempty"`
}

// TableColumn is one column of a `table` widget (manifest_schema.md §3.3.1):
// the cell at its position in every row.
type TableColumn struct {
	ID string `json:"id"`
	// Header is the column's heading; empty omits it and the host shows ID.
	Header string `json:"header,omitempty"`
	// Type is "string" (the default when empty), "number" or "bool".
	Type string `json:"type,omitempty"`
	// Precision is a number column's decimals shown, 0 to 15; nil selects 3.
	Precision *int `json:"precision,omitempty"`
	// Unit is shown in a number column's header; nil omits it.
	Unit *string `json:"unit,omitempty"`
}

// Table bounds (manifest_schema.md §3.3.1, control_plane_protocol.md §8.4).
// A table's rows are data, not widgets, so they do not count against a
// list's 4096 cells. A statement or patch past TableMaxRows is refused whole
// by the host, and one PanelPatch carrying a table op is at most
// TableMaxPatchBytes encoded.
const (
	TableMaxRows       = 100_000
	TableMaxColumns    = 32
	TableMaxPatchBytes = 1 << 20
	// TableMaxRowsPerMessage caps a table message's larger repeated field --
	// its rows, or its deleted keys, each counted on its own -- beside
	// TableMaxPatchBytes: 100,000 short-key rows fit in 1 MiB but pass §9's
	// repeated-field bound (65,536).
	TableMaxRowsPerMessage = 32768
	TableDefaultPrecision  = 3
	TableMaxKeyChars       = 128
)

// TableColumnTypes are the column types a table declares, the first the default.
var TableColumnTypes = []string{"string", "number", "bool"}

// ColumnType is the column's type, "string" when undeclared.
func (c TableColumn) ColumnType() string {
	if c.Type == "" {
		return "string"
	}
	return c.Type
}

// ColumnHeader is the heading the host shows: Header, or ID when empty.
func (c TableColumn) ColumnHeader() string {
	if c.Header == "" {
		return c.ID
	}
	return c.Header
}

// ColumnPrecision is a number column's decimals, TableDefaultPrecision when
// undeclared.
func (c TableColumn) ColumnPrecision() int {
	if c.Precision == nil {
		return TableDefaultPrecision
	}
	return *c.Precision
}

type Exporter struct {
	ID        string `json:"id"`
	Name      string `json:"name,omitempty"`
	Extension string `json:"extension"`
}
type Overlay struct {
	ID         string `json:"id"`
	Name       string `json:"name,omitempty"`
	SegmentMiB int    `json:"segment_mib,omitempty"` // zero selects the host default (32)
	Chunked    bool   `json:"chunked,omitempty"`
	Time       string `json:"time,omitempty"`
}

// errSegmentMiB is the reference's text for a segment_mib that is not an
// integer in range.
var errSegmentMiB = errors.New("segment_mib must be an int in [1, 512]")

// UnmarshalJSON refuses a stated segment_mib that is not a JSON integer in
// [1, 512]: a boolean, a string, null, a fraction or an exponent is not an int.
func (o *Overlay) UnmarshalJSON(data []byte) error {
	type plain Overlay
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	if raw, ok := fields["segment_mib"]; ok {
		text := strings.TrimSpace(string(raw))
		n, err := strconv.Atoi(text)
		if err != nil || n < 1 || n > 512 {
			return errSegmentMiB
		}
	}
	return json.Unmarshal(data, (*plain)(o))
}

type Tool struct {
	ViewTargets        []string `json:"view_targets,omitempty"`
	ID                 string   `json:"id"`
	Name               string   `json:"name,omitempty"`
	Views              []string `json:"views,omitempty"` // nil selects both views
	Menu               *string  `json:"menu"`
	Mode               string   `json:"mode,omitempty"`
	CancelOnCameraMove *bool    `json:"cancel_on_camera_move,omitempty"` // nil means true
	// WantsPick says whether the tool wants the host's surface pick on its 3D
	// presses; nil means true, which is every tool before the field existed.
	// False saves the host a trace per press for a tool that resolves its own
	// anchor from the ray. It is a request about what the host computes, not a
	// promise about what arrives: handle Ray and RayAndPicked whichever is
	// declared.
	WantsPick *bool       `json:"wants_pick,omitempty"`
	Triggers  []Trigger   `json:"triggers,omitempty"`
	Actions   []Action    `json:"actions,omitempty"`
	Pane      *PaneButton `json:"pane,omitempty"`
}
type Trigger struct {
	Key       string   `json:"key,omitempty"`
	Button    string   `json:"button,omitempty"`
	Modifiers []string `json:"modifiers,omitempty"`
	Hold      string   `json:"hold,omitempty"`
}
type Action struct {
	ID   string      `json:"id"`
	Name string      `json:"name,omitempty"`
	Key  string      `json:"key,omitempty"`
	Pane *PaneButton `json:"pane,omitempty"`
}
type PaneButton struct {
	Icon    string `json:"icon"`
	Tooltip string `json:"tooltip,omitempty"`
	Hold    string `json:"hold,omitempty"`
}
type Asset struct {
	ID       string      `json:"id"`
	Name     string      `json:"name,omitempty"`
	Filename string      `json:"filename"`
	Size     int64       `json:"size"`
	SHA256   string      `json:"sha256"`
	Source   AssetSource `json:"source"`
}
type AssetSource struct {
	Kind string `json:"kind"`
	URL  string `json:"url,omitempty"`
}

// JSON returns formatted UTF-8 JSON with a final newline after validating the
// declaration. ValidateFiles additionally checks the files named by pane icons.
func (m Manifest) JSON() ([]byte, error) {
	if err := m.Validate(); err != nil {
		return nil, err
	}
	b, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return nil, err
	}
	return append(b, '\n'), nil
}

// UnmarshalJSON retains explicit null, which is not an omitted text_format.
func (w *Widget) UnmarshalJSON(data []byte) error {
	type plain Widget
	var decoded plain
	if err := json.Unmarshal(data, &decoded); err != nil {
		return err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	*w = Widget(decoded)
	if value, ok := fields["text_format"]; ok && string(value) == "null" {
		w.textFormatNull = true
	}
	return nil
}

// HasStep reports whether a slider's manifest entry declares step
// (manifest_schema.md §3.3.1: a declared step is remembered as such, so a
// declared 1.0 is not the default). A float slider with a step moves by it and
// reads out at its precision; one without has 1000 positions and three
// decimals.
func (w Widget) HasStep() bool { return w.Step != nil }
