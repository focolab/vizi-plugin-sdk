package manifest

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"math"
	"net/url"
	"path"
	"reflect"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"
)

func nonblank(s string) bool { return strings.TrimSpace(s) != "" && utf8.ValidString(s) }
func unique(seen map[string]bool, kind, id string) error {
	if !nonblank(id) {
		return fmt.Errorf("%s id must be non-empty UTF-8", kind)
	}
	if seen[id] {
		return fmt.Errorf("duplicate %s id %q", kind, id)
	}
	seen[id] = true
	return nil
}
func menu(m *string) error {
	if m != nil && *m != "tools" {
		return fmt.Errorf("menu %q not allowed (expected tools or null)", *m)
	}
	return nil
}

// LocalPath accepts portable slash-separated paths strictly inside a package.
// It deliberately excludes Windows drive, stream, separator and device names.
func LocalPath(s string) bool {
	if !fs.ValidPath(s) || s == "." || strings.ContainsAny(s, "\\:\x00") {
		return false
	}
	for _, p := range strings.Split(s, "/") {
		if strings.ContainsAny(p, "<>\"|?*") || strings.HasSuffix(p, ".") || strings.HasSuffix(p, " ") {
			return false
		}
		for _, r := range p {
			if r < 32 {
				return false
			}
		}
		stem := strings.ToUpper(strings.SplitN(p, ".", 2)[0])
		if slices.Contains([]string{"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}, stem) {
			return false
		}
		if len(stem) == 4 && (strings.HasPrefix(stem, "COM") || strings.HasPrefix(stem, "LPT")) && stem[3] >= '1' && stem[3] <= '9' {
			return false
		}
	}
	return utf8.ValidString(s)
}
func pane(p *PaneButton, tool bool) error {
	if p == nil {
		return nil
	}
	if !nonblank(p.Icon) {
		return fmt.Errorf("pane.icon must be non-empty")
	}
	if !LocalPath(p.Icon) {
		return fmt.Errorf("pane.icon %q must be a path inside the plugin directory", p.Icon)
	}
	if ext := strings.ToLower(path.Ext(p.Icon)); ext != ".svg" && ext != ".png" {
		return fmt.Errorf("pane.icon %q is not one of .svg or .png", p.Icon)
	}
	if !utf8.ValidString(p.Tooltip) || utf8.RuneCountInString(p.Tooltip) > 128 {
		return fmt.Errorf("pane.tooltip longer than 128 characters or invalid UTF-8")
	}
	if !tool && p.Hold == "toggle" {
		return fmt.Errorf("pane.hold %q: an action or command button fires once on click", p.Hold)
	}
	if p.Hold != "" && p.Hold != "momentary" && !(tool && p.Hold == "toggle") {
		return fmt.Errorf("pane.hold %q is invalid for this contribution", p.Hold)
	}
	return nil
}

// Validate checks the generated schema-0/1 document, including cross-references.
// It does not read files, normalize caller-owned data or infer permissions.
func (m Manifest) Validate() error {
	if m.ManifestVersion != 0 && m.ManifestVersion != 1 {
		return fmt.Errorf("manifest_version %d unsupported", m.ManifestVersion)
	}
	if !nonblank(m.ID) {
		return fmt.Errorf("id must be non-empty UTF-8")
	}
	if !nonblank(m.Name) {
		return fmt.Errorf("missing required field 'name' or blank name")
	}
	if !nonblank(m.Version) {
		return fmt.Errorf("missing required field 'version' or blank version")
	}
	if m.Engines.Vizi == "" {
		return fmt.Errorf("missing required field 'vizi'")
	}
	version := strings.TrimSpace(m.Engines.Vizi)
	for _, prefix := range []string{">=", "^", "~"} {
		version = strings.TrimSpace(strings.TrimPrefix(version, prefix))
	}
	parts := strings.Split(version, ".")
	if len(parts) < 1 || len(parts) > 2 {
		return fmt.Errorf("engines.vizi %q is not a version", m.Engines.Vizi)
	}
	for i, p := range parts {
		if i == 1 && p == "x" {
			continue
		}
		if n, e := strconv.Atoi(p); e == nil && n < 0 {
			return fmt.Errorf("engines.vizi %q has a negative component", m.Engines.Vizi)
		} else if e != nil {
			return fmt.Errorf("engines.vizi %q is not a version", m.Engines.Vizi)
		}
	}
	if len(m.Entry.Command) == 0 || !nonblank(m.Entry.Command[0]) {
		return fmt.Errorf("entry.command must be a non-empty list of strings")
	}
	for _, s := range m.Entry.Command {
		if !utf8.ValidString(s) || strings.ContainsRune(s, 0) {
			return fmt.Errorf("entry.command contains invalid UTF-8 or NUL")
		}
	}
	if err := validateStartupSeconds(m.Entry.StartupSeconds); err != nil {
		return err
	}
	if len(m.Contributions.RegionTypes) > 0 && !slices.Contains(m.Permissions, "contribute_rois") {
		return fmt.Errorf("region_types need the contribute_rois permission")
	}
	regionTypes := map[string]bool{}
	for _, r := range m.Contributions.RegionTypes {
		if !regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`).MatchString(r.ID) {
			return fmt.Errorf("region type id must start with a letter or digit and contain only letters, digits, '.', '_' or '-'")
		}
		if err := unique(regionTypes, "region type", r.ID); err != nil {
			return err
		}
		if slices.Contains([]string{"box", "ellipsoid", "cylinder", "surfq6"}, r.ID) {
			return fmt.Errorf("region type id '%s' is a built-in type", r.ID)
		}
		if strings.HasPrefix(r.ID, "vizi.") {
			return fmt.Errorf("region type id '%s' is reserved: ids beginning 'vizi.' are the host's", r.ID)
		}
		if !utf8.ValidString(r.Name) {
			return fmt.Errorf("region type name must be UTF-8")
		}
	}
	commands := map[string]bool{}
	for _, c := range m.Contributions.Commands {
		if err := unique(commands, "command", c.ID); err != nil {
			return err
		}
		if !nonblank(c.Title) {
			return fmt.Errorf("command %q: missing required field 'title'", c.ID)
		}
		if err := menu(c.Menu); err != nil {
			return err
		}
		if c.DefaultKeybinding != "" && !nonblank(c.DefaultKeybinding) {
			return fmt.Errorf("command %q: default_keybinding must be a non-empty string", c.ID)
		}
		if err := pane(c.Pane, false); err != nil {
			return err
		}
		if m.ManifestVersion == 0 && c.Pane != nil {
			return fmt.Errorf("pane needs manifest_version 1 (this manifest declares 0)")
		}
	}
	seen := map[string]bool{}
	for _, s := range m.Contributions.Stages {
		if err := ValidateStageParams(s.Params); err != nil {
			return fmt.Errorf("stage %q: %w", s.ID, err)
		}
		if err := unique(seen, "stage", s.ID); err != nil {
			return err
		}
		if !utf8.ValidString(s.Name) {
			return fmt.Errorf("stage name must be UTF-8")
		}
	}
	seen = map[string]bool{}
	for _, r := range m.Contributions.Readers {
		if err := unique(seen, "reader", r.ID); err != nil {
			return err
		}
		if err := r.validateSuffixes(); err != nil {
			return fmt.Errorf("reader %q: %w", r.ID, err)
		}
	}
	seen = map[string]bool{}
	for _, e := range m.Contributions.Exporters {
		if err := unique(seen, "exporter", e.ID); err != nil {
			return err
		}
		if !validSuffix(e.Extension) {
			return fmt.Errorf("exporter %q: extension must be non-empty", e.ID)
		}
	}
	seen = map[string]bool{}
	hasViewPanel := false
	for _, p := range m.Contributions.Panels {
		if err := validateViewTargets(p.FollowTargets); err != nil {
			return fmt.Errorf("panel %q: follow_targets must be distinct names from t, z, select_region, frame_region, look_at, orbit_point: %w", p.ID, err)
		}
		if err := validateViewTargets(p.ViewTargets); err != nil {
			return fmt.Errorf("panel %q: %w", p.ID, err)
		}
		if err := unique(seen, "panel", p.ID); err != nil {
			return err
		}
		if !nonblank(p.Title) {
			return fmt.Errorf("panel %q needs a title", p.ID)
		}
		if p.MenuLabel != "" && !nonblank(p.MenuLabel) {
			return fmt.Errorf("menu_label must be a non-empty string")
		}
		if utf8.RuneCountInString(p.MenuLabel) > 128 {
			return fmt.Errorf("menu_label must be at most 128 characters")
		}
		hasViewPanel = hasViewPanel || len(p.ViewTargets) != 0
		count := 0
		if err := widget(p.Root, 1, &count, map[string]bool{}, commands); err != nil {
			return fmt.Errorf("panel %q: %w", p.ID, err)
		}
		if cells := panelListCells(p.Root); cells > 4096 {
			return fmt.Errorf("panel '%s''s lists may realise at most 4096 cells together (each list's max_rows × row cells), not %d", p.ID, cells)
		}
		if movesView(p.Root) && len(p.ViewTargets) == 0 {
			return fmt.Errorf("panel %q has moves_view widgets and declares no view_targets", p.ID)
		}
	}
	seen = map[string]bool{}
	for _, o := range m.Contributions.Overlays {
		if err := unique(seen, "overlay", o.ID); err != nil {
			return err
		}
		if o.Chunked {
			if o.SegmentMiB != 0 {
				return fmt.Errorf("overlay %q: a chunked overlay's arena is sized by the host from the dataset; segment_mib does not apply", o.ID)
			}
			if o.Time != "" && o.Time != "per_t" && o.Time != "static" {
				return fmt.Errorf("time must be one of ['per_t', 'static'], got %q", o.Time)
			}
		} else {
			if o.Time != "" {
				return fmt.Errorf("time applies only to a chunked overlay")
			}
			if o.SegmentMiB < 0 || o.SegmentMiB > 512 {
				return fmt.Errorf("segment_mib must be an int in [1, 512]")
			}
		}
	}
	seen = map[string]bool{}
	for _, t := range m.Contributions.Tools {
		if len(t.ViewTargets) != 0 && !hasViewPanel {
			return fmt.Errorf("tool %q declares view_targets but no panel does; the move-the-view row lives on the plugin's panel", t.ID)
		}
		if err := validateViewTargets(t.ViewTargets); err != nil {
			return fmt.Errorf("tool %q: %w", t.ID, err)
		}
		if err := unique(seen, "tool", t.ID); err != nil {
			return err
		}
		if t.Views != nil && len(t.Views) == 0 {
			return fmt.Errorf("tool %q: views must be a non-empty list from ['2d', '3d']", t.ID)
		}
		for _, v := range t.Views {
			if v != "2d" && v != "3d" {
				return fmt.Errorf("views must be a non-empty list from ['2d', '3d'], got %q", v)
			}
		}
		if err := menu(t.Menu); err != nil {
			return err
		}
		if t.Mode != "" && t.Mode != "gesture" && t.Mode != "fire" {
			return fmt.Errorf("mode %q not in ['gesture', 'fire']", t.Mode)
		}
		if m.ManifestVersion == 0 && (t.Mode != "" || t.CancelOnCameraMove != nil || t.WantsPick != nil || len(t.Triggers) > 0 || len(t.Actions) > 0 || t.Pane != nil) {
			for _, field := range []struct {
				name    string
				present bool
			}{
				{"triggers", len(t.Triggers) > 0}, {"actions", len(t.Actions) > 0}, {"pane", t.Pane != nil}, {"mode", t.Mode != ""}, {"cancel_on_camera_move", t.CancelOnCameraMove != nil}, {"wants_pick", t.WantsPick != nil},
			} {
				if field.present {
					return fmt.Errorf("%s needs manifest_version 1 (this manifest declares 0)", field.name)
				}
			}
		}
		if err := pane(t.Pane, true); err != nil {
			return err
		}
		keys, triggers := map[string]bool{}, map[string]bool{}
		for _, tr := range t.Triggers {
			key, button := strings.ToLower(strings.TrimSpace(tr.Key)), strings.ToLower(strings.TrimSpace(tr.Button))
			if (key == "") == (button == "") {
				return fmt.Errorf("a trigger names a key or a button, not both or neither")
			}
			if tr.Hold != "" && tr.Hold != "toggle" && tr.Hold != "momentary" {
				return fmt.Errorf("hold %q not in ['toggle', 'momentary']", tr.Hold)
			}
			mods := map[string]bool{}
			for _, mod := range tr.Modifiers {
				mod = strings.ToLower(strings.TrimSpace(mod))
				if mod == "alt" {
					return fmt.Errorf("alt is not a mouse modifier")
				}
				if mod != "shift" && mod != "ctrl" && mod != "meta" {
					return fmt.Errorf("modifiers %q not in ['shift', 'ctrl', 'meta']", mod)
				}
				mods[mod] = true
			}
			ident := "key:" + key
			if key != "" {
				if len(mods) > 0 {
					return fmt.Errorf("a key trigger carries its modifiers in the key name")
				}
				keys[key] = true
			} else {
				if button != "left" && button != "right" && button != "middle" {
					return fmt.Errorf("button %q not in ['left', 'right', 'middle']", button)
				}
				if button == "left" && len(mods) == 0 {
					return fmt.Errorf("the bare left button is never a trigger")
				}
				if tr.Hold != "" && tr.Hold != "momentary" {
					return fmt.Errorf("a mouse trigger is momentary")
				}
				ident = fmt.Sprintf("mouse:%s:%t:%t:%t", button, mods["shift"], mods["ctrl"], mods["meta"])
			}
			if triggers[ident] {
				return fmt.Errorf("duplicate trigger")
			}
			triggers[ident] = true
		}
		if len(t.Actions) > 64 {
			return fmt.Errorf("tool has more than 64 actions")
		}
		triggerKeys := make(map[string]bool)
		for key := range keys {
			triggerKeys[key] = true
		}
		actions := map[string]bool{}
		for _, a := range t.Actions {
			if err := unique(actions, "action", a.ID); err != nil {
				return err
			}
			if err := pane(a.Pane, false); err != nil {
				return err
			}
			key := strings.ToLower(strings.TrimSpace(a.Key))
			if key == "" && a.Pane == nil {
				return fmt.Errorf("action %q: an action needs a key or a pane button to be reachable", a.ID)
			}
			if key != "" {
				if keys[key] {
					if triggerKeys[key] {
						return fmt.Errorf("key %q is also one of this tool's triggers", a.Key)
					}
					return fmt.Errorf("key %q is also another action's of this tool", a.Key)
				}
				keys[key] = true
			}
		}
	}
	if len(m.Contributions.Overlays) > 0 {
		c := m.Contributions
		hasStarter := len(c.Panels) > 0 || len(c.Readers) > 0 || len(c.Exporters) > 0 || len(c.Stages) > 0 || len(c.RegionTypes) > 0
		for _, command := range c.Commands {
			hasStarter = hasStarter || command.Menu != nil || command.Pane != nil
		}
		for _, tool := range c.Tools {
			hasStarter = hasStarter || tool.Menu != nil || tool.Pane != nil
		}
		if !hasStarter {
			return fmt.Errorf("overlays are drawn only while the plugin runs, and nothing declared here starts it; declare a panel (or a command or tool in the Tools menu or on the pane, a reader, an exporter or a stage)")
		}
	}
	seen = map[string]bool{}
	assetID := regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)
	for _, a := range m.Assets {
		if err := unique(seen, "asset", a.ID); err != nil {
			return err
		}
		if !assetID.MatchString(a.ID) {
			return fmt.Errorf("asset %q: id must start with an alphanumeric", a.ID)
		}
		if !LocalPath(a.Filename) || strings.Contains(a.Filename, "/") || strings.HasPrefix(a.Filename, ".") {
			return fmt.Errorf("asset filename must be one plain file name")
		}
		if a.Size <= 0 {
			return fmt.Errorf("asset size must be a positive integer")
		}
		if b, e := hex.DecodeString(a.SHA256); e != nil || len(b) != 32 {
			return fmt.Errorf("asset sha256 must be exactly 64 hexadecimal characters")
		}
		switch a.Source.Kind {
		case "manual":
			if a.Source.URL != "" {
				return fmt.Errorf("manual source must not declare a url")
			}
		case "https":
			if a.Source.URL == "" {
				return fmt.Errorf("HTTPS source needs a string url")
			}
			u, e := url.Parse(a.Source.URL)
			if e != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil {
				return fmt.Errorf("source.url must be an HTTPS URL without credentials")
			}
		default:
			return fmt.Errorf("source.kind must be 'manual' or 'https'")
		}
	}
	for _, s := range m.Subscriptions {
		if !utf8.ValidString(s) {
			return fmt.Errorf("subscription is not UTF-8")
		}
	}
	for _, p := range m.Permissions {
		if !utf8.ValidString(p) {
			return fmt.Errorf("permission is not UTF-8")
		}
	}
	return nil
}
func number(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, !math.IsNaN(n) && !math.IsInf(n, 0)
	case float32:
		return number(float64(n))
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	case json.Number:
		f, e := n.Float64()
		return f, e == nil && !math.IsNaN(f) && !math.IsInf(f, 0)
	}
	return 0, false
}
func widget(w Widget, depth int, count *int, ids, commands map[string]bool) error {
	if depth > 8 {
		return fmt.Errorf("panel tree deeper than 8")
	}
	*count++
	if *count > 256 {
		return fmt.Errorf("panel has more than 256 widgets")
	}
	if err := unique(ids, "widget", w.ID); err != nil {
		return err
	}
	for _, p := range []*float64{w.Min, w.Max, w.Step} {
		if p != nil {
			if _, ok := number(*p); !ok {
				return fmt.Errorf("widget number must be finite")
			}
		}
	}
	if w.textFormatNull {
		return fmt.Errorf("%s %q text_format must be a string, not null", w.Kind, w.ID)
	}
	if w.TextFormat != nil {
		if *w.TextFormat != "plain" && *w.TextFormat != "rich" {
			return fmt.Errorf("%s '%s' text_format must be one of ['plain', 'rich']", w.Kind, w.ID)
		}
		if w.Kind != "label" {
			return fmt.Errorf("%s '%s' does not take text_format; only a label does", w.Kind, w.ID)
		}
	}
	if w.Selectable != nil && *w.Selectable && w.Kind != "list" {
		return fmt.Errorf("%s %q does not take selectable", w.Kind, w.ID)
	}
	switch w.Kind {
	case "button", "slider", "dropdown", "checkbox", "line_edit", "channel_picker", "color_picker", "colormap_picker", "roi_picker":
		if !commands[w.Command] {
			return fmt.Errorf("%s widget %q must bind a declared command", w.Kind, w.ID)
		}
	case "list":
		if len(w.Row) == 0 {
			return fmt.Errorf("list %q has an empty row", w.ID)
		}
		if w.Selectable != nil && *w.Selectable {
			if !commands[w.Command] {
				return fmt.Errorf("selectable list %q must bind a declared command", w.ID)
			}
		} else if w.Command != "" {
			return fmt.Errorf("list %q binds a command only when selectable", w.ID)
		}
		for i, cell := range w.Row {
			if cell.Kind == "list" || cell.Kind == "row" || cell.Kind == "column" {
				return fmt.Errorf("list %q row[%d] is a %s; a row holds leaf widgets only", w.ID, i, cell.Kind)
			}
			if err := widget(cell, depth+1, count, ids, commands); err != nil {
				return err
			}
		}
		for _, field := range []struct {
			name  string
			bound *int
		}{{"max_rows", w.MaxRows}, {"visible_rows", w.VisibleRows}} {
			if field.bound != nil && (*field.bound < 1 || *field.bound > 1024) {
				return fmt.Errorf("list '%s' %s must be an integer in [1, 1024]", w.ID, field.name)
			}
		}
		if w.RowLimit() > 4096/len(w.Row) {
			return fmt.Errorf("list '%s' may realise at most 4096 cells (max_rows × row cells), not %d × %d", w.ID, w.RowLimit(), len(w.Row))
		}
	case "image":
		if w.Command != "" && !commands[w.Command] {
			return fmt.Errorf("image widget %q must bind a declared command", w.ID)
		}
	case "label", "progress":
	case "row", "column":
		if len(w.Children) == 0 {
			return fmt.Errorf("%s %q has no children", w.Kind, w.ID)
		}
	default:
		return fmt.Errorf("unknown widget kind %q", w.Kind)
	}
	if w.MovesView {
		switch w.Kind {
		case "button", "slider", "dropdown", "checkbox", "line_edit", "channel_picker", "color_picker", "colormap_picker", "roi_picker":
		case "list", "image":
			if w.Command == "" {
				return fmt.Errorf("moves_view widget %q needs a command", w.ID)
			}
		default:
			return fmt.Errorf("%s %q cannot move the view", w.Kind, w.ID)
		}
	}
	if w.Kind != "roi_picker" && (w.FollowsSelection || w.Filter != nil) {
		return fmt.Errorf("%s '%s' does not take follows_selection or filter", w.Kind, w.ID)
	}
	if w.Filter != nil && *w.Filter != "all" && *w.Filter != "mine" {
		return fmt.Errorf("roi_picker '%s' filter must be one of ['all', 'mine']", w.ID)
	}
	if len(w.Row) != 0 && w.Kind != "list" {
		return fmt.Errorf("only lists have row templates")
	}
	if w.Multiple != nil && *w.Multiple && w.Kind != "channel_picker" {
		return fmt.Errorf("%s %q does not take multiple", w.Kind, w.ID)
	}
	switch w.Kind {
	case "channel_picker":
		if w.Value != nil {
			valid := channelIndex(w.Value)
			if w.Multiple != nil && *w.Multiple {
				values := reflect.ValueOf(w.Value)
				if values.Kind() == reflect.Slice || values.Kind() == reflect.Array {
					valid = true
					for i := 0; i < values.Len(); i++ {
						if !channelIndex(values.Index(i).Interface()) {
							valid = false
							break
						}
					}
				}
			}
			if !valid {
				return fmt.Errorf("channel_picker %q value must be a channel index or, with multiple, a list of indices", w.ID)
			}
		}
	case "color_picker":
		if w.Value != nil {
			value, ok := w.Value.(string)
			if !ok || !regexp.MustCompile(`^#[0-9a-fA-F]{6}$`).MatchString(value) {
				return fmt.Errorf("color_picker %q value must be a #RRGGBB colour", w.ID)
			}
		}
	case "colormap_picker", "roi_picker":
		if w.Value != nil {
			if _, ok := w.Value.(string); !ok {
				return fmt.Errorf("%s %q value must be string", w.Kind, w.ID)
			}
		}
	case "progress":
		if w.Value != nil {
			value, ok := number(w.Value)
			if !ok {
				return fmt.Errorf("progress %q value must be numeric", w.ID)
			}
			if value < 0 || value > 1 {
				return fmt.Errorf("progress %q value must be within [0, 1]", w.ID)
			}
		}
	case "slider":
		lo, hi, step := 0.0, 1.0, 1.0
		if w.Min != nil {
			lo = *w.Min
		}
		if w.Max != nil {
			hi = *w.Max
		}
		if w.Step != nil {
			step = *w.Step
		}
		if lo >= hi {
			return fmt.Errorf("slider %q needs min < max", w.ID)
		}
		if step <= 0 {
			return fmt.Errorf("slider %q needs step > 0", w.ID)
		}
		if w.Value != nil {
			if _, ok := number(w.Value); !ok {
				return fmt.Errorf("slider %q value must be numeric and finite", w.ID)
			}
		}
	case "dropdown":
		if len(w.Options) == 0 {
			return fmt.Errorf("dropdown %q needs non-empty options", w.ID)
		}
		if w.Value != nil {
			s, ok := w.Value.(string)
			if !ok || !slices.Contains(w.Options, s) {
				return fmt.Errorf("dropdown %q default %q not in options", w.ID, w.Value)
			}
		}
	case "checkbox":
		if w.Value != nil {
			if _, ok := w.Value.(bool); !ok {
				return fmt.Errorf("checkbox value must be boolean")
			}
		}
	case "line_edit":
		if w.Value != nil {
			if _, ok := w.Value.(string); !ok {
				return fmt.Errorf("line_edit value must be string")
			}
		}
	}
	if w.Kind != "row" && w.Kind != "column" && len(w.Children) > 0 {
		return fmt.Errorf("only containers have children")
	}
	for _, c := range w.Children {
		if err := widget(c, depth+1, count, ids, commands); err != nil {
			return err
		}
	}
	return nil
}

// Icons returns distinct pane icon paths in declaration order.
func (m Manifest) Icons() []string {
	var out []string
	add := func(p *PaneButton) {
		if p != nil && !slices.Contains(out, p.Icon) {
			out = append(out, p.Icon)
		}
	}
	for _, c := range m.Contributions.Commands {
		add(c.Pane)
	}
	for _, t := range m.Contributions.Tools {
		add(t.Pane)
		for _, a := range t.Actions {
			add(a.Pane)
		}
	}
	return out
}

// ValidateFiles checks icon paths, size and file signatures as the host does.
// files can be an embed.FS, os.DirFS, or any other fs.FS. No asset downloads run.
func (m Manifest) ValidateFiles(files fs.FS) error {
	if err := m.Validate(); err != nil {
		return err
	}
	for _, name := range m.Icons() {
		if files == nil {
			return fmt.Errorf("pane icon %q needs a resource filesystem", name)
		}
		f, err := files.Open(name)
		if err != nil {
			return fmt.Errorf("pane.icon %q not found in the plugin directory: %w", name, err)
		}
		b, err := io.ReadAll(io.LimitReader(f, 65537))
		closeErr := f.Close()
		if err != nil {
			return err
		}
		if closeErr != nil {
			return closeErr
		}
		if len(b) > 65536 {
			return fmt.Errorf("pane.icon %q is %d bytes; the limit is 65536", name, len(b))
		}
		if strings.EqualFold(path.Ext(name), ".png") {
			if !bytes.HasPrefix(b, []byte("\x89PNG\r\n\x1a\n")) {
				return fmt.Errorf("pane.icon %q is not a PNG file", name)
			}
		} else if !bytes.Contains(b[:min(len(b), 512)], []byte("<svg")) {
			return fmt.Errorf("pane.icon %q is not an SVG file", name)
		}
	}
	return nil
}

// JSON widget values decode as float64, while declarations may use Go integers.
func channelIndex(value any) bool {
	if value == nil {
		return false
	}
	v := reflect.ValueOf(value)
	switch v.Kind() {
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return v.Int() >= 0
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		return true
	}
	f, ok := number(value)
	return ok && f >= 0 && math.Trunc(f) == f
}

func validateViewTargets(targets []string) error {
	seen := map[string]bool{}
	for _, target := range targets {
		switch target {
		case "t", "z", "select_region", "frame_region", "look_at", "orbit_point":
		default:
			return fmt.Errorf("view_targets must be distinct names from t, z, select_region, frame_region, look_at, orbit_point; invalid target %q", target)
		}
		if seen[target] {
			return fmt.Errorf("view_targets must be distinct names from t, z, select_region, frame_region, look_at, orbit_point; duplicate target %q", target)
		}
		seen[target] = true
	}
	return nil
}
func movesView(w Widget) bool {
	if w.MovesView {
		return true
	}
	for _, child := range w.Children {
		if movesView(child) {
			return true
		}
	}
	for _, cell := range w.Row {
		if movesView(cell) {
			return true
		}
	}
	return false
}

func panelListCells(w Widget) int {
	if w.Kind == "list" {
		return w.RowLimit() * len(w.Row)
	}
	total := 0
	for _, child := range w.Children {
		total += panelListCells(child)
	}
	return total
}
