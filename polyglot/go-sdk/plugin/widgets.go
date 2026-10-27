package plugin

import (
	"fmt"
	"slices"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
)

// Widget is a declaration, not session state. Use the same initial values in
// application state when needed; a future stateful binding layer can own those.
// Interactive widgets retain a CommandRef, never an independently authored ID.
type Widget struct {
	spec      manifest.Widget
	command   CommandRef
	children  []Widget
	row       []Widget
	clickable bool
}

func (w Widget) ID() string { return w.spec.ID }
func (w Widget) declaration(owner *definitionID, depth int) (manifest.Widget, error) {
	if depth > 8 {
		return manifest.Widget{}, fmt.Errorf("panel tree deeper than 8")
	}
	out := w.spec
	if out.Kind == "list" {
		if out.Selectable != nil && *out.Selectable {
			if w.command.owner != owner || w.command.id == "" {
				return out, fmt.Errorf("selectable list %q needs a command from this definition", out.ID)
			}
			out.Command = w.command.id
		} else if w.command != (CommandRef{}) {
			return out, fmt.Errorf("list %q binds a command only when selectable", out.ID)
		}
	}
	if out.Kind == "table" {
		if out.Selectable != nil && *out.Selectable {
			if w.command.owner != owner || w.command.id == "" {
				return out, fmt.Errorf("selectable table %q needs a command from this definition", out.ID)
			}
			out.Command = w.command.id
		} else if w.command != (CommandRef{}) {
			return out, fmt.Errorf("table '%s' binds a command only when selectable", out.ID)
		}
	}
	if out.Kind == "image" && w.clickable {
		if w.command.owner != owner || w.command.id == "" {
			return out, fmt.Errorf("image %q needs a command from this definition", out.ID)
		}
		out.Command = w.command.id
	}
	switch out.Kind {
	case "button", "slider", "dropdown", "checkbox", "line_edit", "channel_picker", "color_picker", "colormap_picker", "roi_picker":
		if w.command.owner != owner || w.command.id == "" {
			return out, fmt.Errorf("widget %q needs a command from this definition", out.ID)
		}
		out.Command = w.command.id
	}
	for _, c := range w.children {
		decl, err := c.declaration(owner, depth+1)
		if err != nil {
			return out, err
		}
		out.Children = append(out.Children, decl)
	}
	for _, cell := range w.row {
		decl, err := cell.declaration(owner, depth+1)
		if err != nil {
			return out, err
		}
		out.Row = append(out.Row, decl)
	}
	return out, nil
}

// Label displays text literally, including any markup, for both its initial
// text and later SetValue updates. Use LabelWithOptions to opt into rich text.
func Label(id, text string) Widget {
	return Widget{spec: manifest.Widget{Kind: "label", ID: id, Text: text}}
}

// LabelOptions selects the host's text interpretation. Empty TextFormat uses
// the default "plain"; "rich" opts into Qt's rich-text subset. Links are styled
// but never opened. Definition.Build rejects other values.
type LabelOptions struct {
	TextFormat string
}

func LabelWithOptions(id, text string, options LabelOptions) Widget {
	w := Label(id, text)
	if options.TextFormat != "" {
		w.spec.TextFormat = &options.TextFormat
	}
	return w
}

// Progress declares a noninteractive indicator. Value must be within [0, 1];
// indeterminate takes display precedence. No command is registered or echoed.
func Progress(id string, value float64, indeterminate bool) Widget {
	return Widget{spec: manifest.Widget{Kind: "progress", ID: id, Value: value, Indeterminate: indeterminate}}
}
func Image(id string) Widget { return Widget{spec: manifest.Widget{Kind: "image", ID: id}} }
func Button(id, text string, command CommandRef) Widget {
	return Widget{spec: manifest.Widget{Kind: "button", ID: id, Text: text}, command: command}
}
func Checkbox(id, text string, value bool, command CommandRef) Widget {
	return Widget{spec: manifest.Widget{Kind: "checkbox", ID: id, Text: text, Value: value}, command: command}
}
func LineEdit(id, value, placeholder string, command CommandRef) Widget {
	return Widget{spec: manifest.Widget{Kind: "line_edit", ID: id, Value: value, Placeholder: placeholder}, command: command}
}
func Dropdown(id string, options []string, value string, command CommandRef) Widget {
	return Widget{spec: manifest.Widget{Kind: "dropdown", ID: id, Options: slices.Clone(options), Value: value}, command: command}
}

type SliderSpec struct {
	ID       string
	Min, Max float64
	Step     float64 // 0 declares no step: a float slider then gets the host's 1000 positions (schema §3.3.1, has_step), an integer one steps by 1; an integer slider's step must be whole
	Value    float64
	Float    bool
}

func Slider(spec SliderSpec, command CommandRef) Widget {
	var step *float64
	if spec.Step != 0 {
		step = &spec.Step
	}
	return Widget{spec: manifest.Widget{Kind: "slider", ID: spec.ID, Min: &spec.Min, Max: &spec.Max, Step: step, Value: spec.Value, Float: spec.Float}, command: command}
}
func Row(id string, children ...Widget) Widget {
	return Widget{spec: manifest.Widget{Kind: "row", ID: id}, children: slices.Clone(children)}
}
func Column(id string, children ...Widget) Widget {
	return Widget{spec: manifest.Widget{Kind: "column", ID: id}, children: slices.Clone(children)}
}

// ChannelPicker offers the host's channels and fires Value.i.
func ChannelPicker(id string, value int64, command CommandRef) Widget {
	return Widget{spec: manifest.Widget{Kind: "channel_picker", ID: id, Value: value}, command: command}
}

// MultiChannelPicker fires Value.ints; its initial selection is copied.
func MultiChannelPicker(id string, values []int64, command CommandRef) Widget {
	multiple := true
	owned := append([]int64{}, values...)
	return Widget{spec: manifest.Widget{Kind: "channel_picker", ID: id, Multiple: &multiple, Value: owned}, command: command}
}
func ColorPicker(id, value string, command CommandRef) Widget {
	return Widget{spec: manifest.Widget{Kind: "color_picker", ID: id, Value: value}, command: command}
}
func ColormapPicker(id, value string, command CommandRef) Widget {
	return Widget{spec: manifest.Widget{Kind: "colormap_picker", ID: id, Value: value}, command: command}
}

// RoiPicker chooses a region for the plugin, without changing host selection.
func RoiPicker(id, value string, command CommandRef) Widget {
	return Widget{spec: manifest.Widget{Kind: "roi_picker", ID: id, Value: value}, command: command}
}

// ListSpec declares a dynamic list's fixed template and bounds. Zero MaxRows
// and VisibleRows select 256 and 8. Command is required exactly when Selectable.
type ListSpec struct {
	ID                   string
	MaxRows, VisibleRows int
	Selectable           bool
	Command              CommandRef
}

// ListRows declares a nonempty template of leaf cells. Cell IDs share the
// panel's namespace; runtime row identity comes from Panel.SetRows and Row.
func ListRows(spec ListSpec, row ...Widget) Widget {
	w := manifest.Widget{Kind: "list", ID: spec.ID, Selectable: &spec.Selectable}
	if spec.MaxRows != 0 {
		w.MaxRows = &spec.MaxRows
	}
	if spec.VisibleRows != 0 {
		w.VisibleRows = &spec.VisibleRows
	}
	return Widget{spec: w, command: spec.Command, row: slices.Clone(row)}
}

// TableColumn is one declared table column. Build it with StringColumn,
// NumberColumn or BoolColumn; WithPrecision and WithUnit refine a number column.
type TableColumn = manifest.TableColumn

// StringColumn declares a text column. An empty header shows the id.
func StringColumn(id, header string) TableColumn {
	return TableColumn{ID: id, Header: header}
}

// NumberColumn declares a right-aligned number column shown with 3 decimals
// unless refined with WithPrecision; WithUnit adds a unit to its header.
func NumberColumn(id, header string) TableColumn {
	return TableColumn{ID: id, Header: header, Type: "number"}
}

// BoolColumn declares a column of flags.
func BoolColumn(id, header string) TableColumn {
	return TableColumn{ID: id, Header: header, Type: "bool"}
}

// WithPrecision returns a copy of a number column showing decimals places
// (0 to 15). Build refuses it on any other column type.
func WithPrecision(c TableColumn, decimals int) TableColumn {
	c.Precision = &decimals
	return c
}

// WithUnit returns a copy of a number column whose header shows unit. Build
// refuses it on any other column type.
func WithUnit(c TableColumn, unit string) TableColumn {
	c.Unit = &unit
	return c
}

// TableSpec declares a table's options. Zero VisibleRows selects 8. Command
// is required exactly when Selectable; Multiple only with Selectable.
type TableSpec struct {
	ID          string
	VisibleRows int
	Selectable  bool
	Multiple    bool
	Sortable    bool
	Command     CommandRef
}

// Table declares rows of typed cells under 1 to 32 columns, supplied at
// runtime with Panel.SetTable and changed by key with Panel.PatchTable; up to
// 100,000 rows. A selectable table fires its command with Args.Strings("rows"),
// one key unless Multiple; Sortable sorts on the host's side on a header click.
// MovesView applies to a selectable table.
func Table(spec TableSpec, columns ...TableColumn) Widget {
	w := manifest.Widget{Kind: "table", ID: spec.ID, Columns: slices.Clone(columns)}
	if spec.Selectable {
		w.Selectable = &spec.Selectable
	}
	if spec.Multiple {
		w.Multiple = &spec.Multiple
	}
	if spec.Sortable {
		w.Sortable = &spec.Sortable
	}
	if spec.VisibleRows != 0 {
		w.VisibleRows = &spec.VisibleRows
	}
	for i := range w.Columns {
		// The declaration owns its pointers, not the caller's column values.
		if p := w.Columns[i].Precision; p != nil {
			n := *p
			w.Columns[i].Precision = &n
		}
		if u := w.Columns[i].Unit; u != nil {
			s := *u
			w.Columns[i].Unit = &s
		}
	}
	return Widget{spec: w, command: spec.Command}
}

// MovesView marks an interactive widget as a source of view causes. Its panel
// must declare ViewTargets. This returns a copy and leaves the original alone.
func (w Widget) MovesView() Widget { w.spec.MovesView = true; return w }

// ClickableImage binds image-pixel events to this definition's command.
// Args includes x, y, button, modifiers and image (the displayed ImageRef offset).
func ClickableImage(id string, command CommandRef) Widget {
	return Widget{spec: manifest.Widget{Kind: "image", ID: id}, command: command, clickable: true}
}

// RoiPickerOptions controls host-owned selection following and region choices.
type RoiPickerOptions struct {
	FollowsSelection bool
	Filter           string // empty defaults to all; otherwise all or mine
}

func RoiPickerWithOptions(id, value string, command CommandRef, options RoiPickerOptions) Widget {
	w := RoiPicker(id, value, command)
	w.spec.FollowsSelection = options.FollowsSelection
	if options.Filter != "" {
		w.spec.Filter = &options.Filter
	}
	return w
}
