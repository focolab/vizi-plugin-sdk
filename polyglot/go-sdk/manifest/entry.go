package manifest

import (
	"encoding/json"
	"errors"
	"math"
)

var errStartupSeconds = errors.New("entry.startup_s must be a positive number of seconds")

func validateStartupSeconds(seconds *float64) error {
	if seconds != nil && (*seconds <= 0 || math.IsNaN(*seconds) || math.IsInf(*seconds, 0)) {
		return errStartupSeconds
	}
	return nil
}

// UnmarshalJSON gives malformed startup estimates the schema's diagnostic,
// including values rejected by Go's field type. As with other optional numeric
// fields, null is treated as absent. Host-specific limits are not SDK limits.
func (e *Entry) UnmarshalJSON(data []byte) error {
	type plain Entry
	var decoded plain
	if err := json.Unmarshal(data, &decoded); err != nil {
		var typeErr *json.UnmarshalTypeError
		if errors.As(err, &typeErr) && typeErr.Field == "startup_s" {
			return errStartupSeconds
		}
		return err
	}
	if err := validateStartupSeconds(decoded.StartupSeconds); err != nil {
		return err
	}
	*e = Entry(decoded)
	return nil
}
