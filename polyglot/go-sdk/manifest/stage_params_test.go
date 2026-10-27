package manifest

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
)

func TestStageParameterDeclarations(t *testing.T) {
	for _, tc := range []struct {
		body string
		ok   bool
	}{
		{`{"key":"f","type":"float","value":0,"min":0,"max":1}`, true},
		{`{"key":"i","type":"int","value":9223372036854775807,"min":-9223372036854775808,"max":9223372036854775807}`, true},
		{`{"key":"i","type":"int","value":9223372036854775808}`, false},
		{`{"key":"i","type":"int","value":-9223372036854775809}`, false},
		{`{"key":"i","type":"int","value":0,"min":-9223372036854775809}`, false},
		{`{"key":"i","type":"int","value":0,"max":9223372036854775808}`, false},
		{`{"key":"b","type":"bool","value":false}`, true},
		{`{"key":"text","type":"string","value":""}`, true},
		{`{"key":"text","type":"string","value":"é","max_length":2}`, true},
		{`{"key":"text","type":"string","value":"é","max_length":1}`, false},
		{`{"key":"text","type":"string","value":4}`, false},
		{`{"key":"text","type":"string","value":"x","max_length":0}`, false},
		{`{"key":"text","type":"string","value":"x","max_length":-1}`, false},
		{`{"key":"text","type":"string","value":"x","max_length":4097}`, false},
		{`{"key":"text","type":"string","value":"x","max_length":2.0}`, false},
		{`{"key":"text","type":"string","value":"x","max_length":null}`, false},
		{`{"key":"text","type":"string","value":"x","options":[]}`, false},
		{`{"key":"text","type":"string","value":"x","min":null}`, false},
		{`{"key":"text","type":"string","value":"x","max":2}`, false},
		{`{"key":"b","type":"bool","value":true,"max_length":0}`, false},
		{`{"key":"b","type":"bool","value":true,"max_length":2}`, false},
		{`{"key":"s","type":"choice","value":"","options":["","A","a"]}`, true},
		{`{"key":" ","type":"bool","value":false}`, false},
		{`{"key":"b","type":"bool"}`, false},
		{`{"key":"b","type":"bool","value":null}`, false},
		{`{"key":"b","type":"bool","value":0}`, false},
		{`{"key":"b","type":"bool","value":true,"min":null}`, false},
		{`{"key":"b","type":"bool","value":true,"options":[]}`, false},
		{`{"key":"b","type":"bool","value":true,"options":null}`, false},
		{`{"key":"b","type":"bool","value":true,"step":1}`, false},
		{`{"key":"b","name":null,"type":"bool","value":true}`, false},
		{`{"key":"i","type":"int","value":1.0}`, false},
		{`{"key":"i","type":"int","value":1,"min":0.5}`, false},
		{`{"key":"f","type":"float","value":1,"min":2}`, false},
		{`{"key":"f","type":"float","value":1,"max":0}`, false},
		{`{"key":"f","type":"float","value":1,"min":2,"max":0}`, false},
		{`{"key":"f","type":"float","value":1,"min":null}`, false},
		{`{"key":"f","type":"float","value":1e999}`, false},
		{`{"key":"s","type":"choice","value":"a","options":["A"]}`, false},
		{`{"key":"s","type":"choice","value":"a","options":["a","a"]}`, false},
		{`{"key":"s","type":"choice","value":"","options":[null]}`, false},
		{`{"key":"s","type":"choice","value":"a","options":[]}`, false},
		{`{"key":"x","type":"other","value":1}`, false},
	} {
		t.Run(tc.body, func(t *testing.T) {
			var p StageParam
			err := json.Unmarshal([]byte(tc.body), &p)
			if err == nil {
				err = ValidateStageParams([]StageParam{p})
			}
			if (err == nil) != tc.ok {
				t.Fatalf("valid=%v want %v: %v", err == nil, tc.ok, err)
			}
			if tc.ok {
				b, err := json.Marshal(p)
				if err != nil {
					t.Fatal(err)
				}
				var q StageParam
				if err = json.Unmarshal(b, &q); err != nil {
					t.Fatal(err)
				}
				if err = ValidateStageParams([]StageParam{q}); err != nil {
					t.Fatal(err)
				}
				if p.Type == "int" && !strings.Contains(string(b), "9223372036854775807") {
					t.Fatal("integer rounded", string(b))
				}
			}
		})
	}
	for _, limit := range []int{0, 2, StringParamMaxBytes} {
		p := StageParam{Key: "text", Type: "string", Value: "é", MaxLength: limit}
		if err := ValidateStageParams([]StageParam{p}); err != nil {
			t.Fatal(err)
		}
		bound := limit
		if bound == 0 {
			bound = StringParamMaxBytes
		}
		if err := p.CheckValue(strings.Repeat("é", bound/2)); err != nil {
			t.Fatal(err)
		}
		if err := p.CheckValue(strings.Repeat("é", bound/2) + "!"); err == nil {
			t.Fatal("byte bound not enforced", limit)
		}
		if err := p.CheckValue(string([]byte{0xff})); err == nil {
			t.Fatal("invalid UTF-8 accepted")
		}
	}
	p := StageParam{Key: "x", Type: "int", Value: int64(math.MaxInt64), Min: int64(math.MaxInt64)}
	if err := ValidateStageParams([]StageParam{p}); err != nil {
		t.Fatal(err)
	}
	if err := p.CheckValue(int64(math.MaxInt64 - 1)); err == nil {
		t.Fatal("integer range rounded")
	}
	if err := ValidateStageParams([]StageParam{p, p}); err == nil {
		t.Fatal("duplicate key accepted")
	}
	for _, v := range []any{math.NaN(), math.Inf(1), math.Inf(-1), true, "1", nil} {
		if err := ValidateStageParams([]StageParam{{Key: "f", Type: "float", Value: v}}); err == nil {
			t.Fatalf("invalid float %v accepted", v)
		}
	}
}
