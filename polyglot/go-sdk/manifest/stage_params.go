package manifest

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"math/big"
	"reflect"
	"slices"
	"strings"
	"unicode/utf8"
)

// StageParam declares a position's parameter. Value is required, including for
// false and zero defaults. Numeric bounds are inclusive. Int values use Go
// integer types or integer json.Number values, within signed 64-bit (including
// bounds); float values also accept integers.
// Name defaults to Key when omitted. Options belongs only to choice parameters.
// MaxLength belongs only to string parameters; zero omits it and means 4096
// UTF-8 bytes. An explicitly supplied JSON max_length must be in [1, 4096].
type StageParam struct {
	Key       string   `json:"key"`
	Name      string   `json:"name,omitempty"`
	Type      string   `json:"type"`
	Value     any      `json:"value"`
	Min       any      `json:"min,omitempty"`
	Max       any      `json:"max,omitempty"`
	Options   []string `json:"options,omitempty"`
	MaxLength int      `json:"max_length,omitempty"`
	invalid   string
}

const StringParamMaxBytes = 4096

// UnmarshalJSON preserves exact integers and remembers fields that omitempty
// would otherwise hide, so validation cannot silently repair a bad declaration.
func (p *StageParam) UnmarshalJSON(b []byte) error {
	type plain StageParam
	var q plain
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	if err := d.Decode(&q); err != nil {
		return err
	}
	*p = StageParam(q)
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(b, &fields); err != nil {
		return err
	}
	for k, v := range fields {
		allowed := k == "key" || k == "name" || k == "type" || k == "value" || ((p.Type == "float" || p.Type == "int") && (k == "min" || k == "max")) || (p.Type == "choice" && k == "options") || (p.Type == "string" && k == "max_length")
		if !allowed || bytes.Equal(bytes.TrimSpace(v), []byte("null")) {
			p.invalid = fmt.Sprintf("a %s param does not take ['%s'] or null fields", p.Type, k)
		}
	}
	if fields["max_length"] != nil && p.MaxLength == 0 {
		p.invalid = "max_length must be an integer in [1, 4096]"
	}
	if raw := fields["options"]; raw != nil {
		var items []json.RawMessage
		if err := json.Unmarshal(raw, &items); err != nil {
			return err
		}
		for _, item := range items {
			if bytes.Equal(bytes.TrimSpace(item), []byte("null")) {
				p.invalid = "null option"
			}
		}
	}
	return nil
}

// CloneStageParams copies declarations, including their option lists. Valid
// Value, Min and Max values are immutable scalars.
func CloneStageParams(params []StageParam) []StageParam {
	out := slices.Clone(params)
	for i := range out {
		out[i].Options = slices.Clone(out[i].Options)
	}
	return out
}

func paramNumber(v any, whole bool) (*big.Rat, error) {
	var s string
	if n, ok := v.(json.Number); ok {
		s = string(n)
	} else if v != nil {
		rv := reflect.ValueOf(v)
		switch rv.Kind() {
		case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
			s = fmt.Sprint(rv.Int())
		case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
			s = fmt.Sprint(rv.Uint())
		case reflect.Float32, reflect.Float64:
			if whole {
				return nil, fmt.Errorf("must be an integer")
			}
			s = fmt.Sprint(rv.Float())
		}
	}
	if s == "" {
		return nil, fmt.Errorf("must be numeric")
	}
	if whole {
		n, ok := new(big.Int).SetString(s, 10)
		if !ok || strings.ContainsAny(s, ".eE") {
			return nil, fmt.Errorf("must be an integer")
		}
		if !n.IsInt64() {
			return nil, fmt.Errorf("must fit a signed 64-bit integer")
		}
		return new(big.Rat).SetInt(n), nil
	}
	f, err := json.Number(s).Float64()
	if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
		return nil, fmt.Errorf("must be a finite number")
	}
	return new(big.Rat).SetFloat64(f), nil
}

// CheckValue checks a plain value against this parameter's type and bounds.
// ValidateStageParams must first validate the declaration itself.
func (p StageParam) CheckValue(v any) error {
	switch p.Type {
	case "string":
		s, ok := v.(string)
		if !ok || !utf8.ValidString(s) {
			return fmt.Errorf("value must be a string in UTF-8")
		}
		limit := p.MaxLength
		if limit == 0 {
			limit = StringParamMaxBytes
		}
		if len(s) > limit {
			return fmt.Errorf("value is longer than max_length (%d UTF-8 bytes)", limit)
		}
	case "bool":
		if _, ok := v.(bool); !ok {
			return fmt.Errorf("value must be a boolean")
		}
	case "choice":
		s, ok := v.(string)
		if !ok || !slices.Contains(p.Options, s) {
			return fmt.Errorf("value must be one of the options")
		}
	case "float", "int":
		n, err := paramNumber(v, p.Type == "int")
		if err != nil {
			return fmt.Errorf("numeric field 'value' %w", err)
		}
		for _, b := range []struct {
			value any
			lower bool
		}{{p.Min, true}, {p.Max, false}} {
			if b.value == nil {
				continue
			}
			bound, err := paramNumber(b.value, p.Type == "int")
			if err != nil {
				return err
			}
			if (b.lower && n.Cmp(bound) < 0) || (!b.lower && n.Cmp(bound) > 0) {
				return fmt.Errorf("value is outside [min, max]")
			}
		}
	default:
		return fmt.Errorf("type must be one of ['float', 'int', 'bool', 'choice', 'string']")
	}
	return nil
}

func ValidateStageParams(params []StageParam) error {
	seen := map[string]bool{}
	for _, p := range params {
		if !nonblank(p.Key) {
			return fmt.Errorf("key must be non-empty")
		}
		if seen[p.Key] {
			return fmt.Errorf("duplicate param key '%s'", p.Key)
		}
		seen[p.Key] = true
		if !slices.Contains([]string{"float", "int", "bool", "choice", "string"}, p.Type) {
			return fmt.Errorf("type must be one of ['float', 'int', 'bool', 'choice', 'string']")
		}
		if p.Value == nil {
			return fmt.Errorf("parameter %q needs a value, the default", p.Key)
		}
		if p.invalid != "" {
			return fmt.Errorf("parameter %q: %s", p.Key, p.invalid)
		}
		if !utf8.ValidString(p.Name) {
			return fmt.Errorf("parameter name must be UTF-8")
		}
		if p.Type != "float" && p.Type != "int" && (p.Min != nil || p.Max != nil) {
			return fmt.Errorf("parameter %q cannot have numeric bounds", p.Key)
		}
		if p.Type != "choice" && p.Options != nil {
			return fmt.Errorf("parameter %q cannot have options", p.Key)
		}
		if (p.Type != "string" && p.MaxLength != 0) || p.MaxLength < 0 || p.MaxLength > StringParamMaxBytes {
			return fmt.Errorf("parameter %q: max_length must be an integer in [1, 4096] and is only for strings", p.Key)
		}
		if p.Type == "choice" {
			if len(p.Options) == 0 {
				return fmt.Errorf("parameter %q: options must be a non-empty list of distinct strings", p.Key)
			}
			options := map[string]bool{}
			for _, s := range p.Options {
				if !utf8.ValidString(s) || options[s] {
					return fmt.Errorf("options must be a non-empty list of distinct strings")
				}
				options[s] = true
			}
		}
		// Validate bounds before checking the default, matching the reference's order.
		if p.Type == "float" || p.Type == "int" {
			var bounds [2]*big.Rat
			for i, field := range []struct {
				name  string
				value any
			}{{"min", p.Min}, {"max", p.Max}} {
				if field.value == nil {
					continue
				}
				n, err := paramNumber(field.value, p.Type == "int")
				if err != nil {
					return fmt.Errorf("numeric field '%s' %w", field.name, err)
				}
				bounds[i] = n
			}
			if bounds[0] != nil && bounds[1] != nil && bounds[0].Cmp(bounds[1]) > 0 {
				return fmt.Errorf("needs min <= max")
			}
		}
		if err := p.CheckValue(p.Value); err != nil {
			return fmt.Errorf("parameter %q default: %w", p.Key, err)
		}
	}
	return nil
}
