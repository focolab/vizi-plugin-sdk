package manifest

import (
	"fmt"
	"regexp"
	"slices"
	"strings"
)

// Version is the one manifest_version this SDK reads and writes. A 1.x
// addition raises engines.vizi, never manifest_version.
const Version = 1

func manifestVersionError(version int, engines string) error {
	declared := "None"
	if engines != "" {
		declared = "'" + engines + "'"
	}
	return fmt.Errorf("manifest_version %d unsupported (this host reads [1]; a 1.x addition raises engines.vizi, which this one declares as %s, never manifest_version)", version, declared)
}

var idPattern = regexp.MustCompile(`^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$`)

// ValidID reports whether id is a plugin id: lower-case reverse-DNS with at
// least one dot, 3 to 64 characters, whose first part is not a Windows device
// name. Distinct ids then name distinct folders, and every one is a folder
// Windows can create.
func ValidID(id string) bool {
	return deviceName(id) == "" && len(id) >= 3 && len(id) <= 64 && idPattern.MatchString(id)
}

// windowsDeviceNames are the names Windows refuses as a folder name, in any
// case and with any extension: an id is an installer's folder name, so its
// first part may not be one.
var windowsDeviceNames = map[string]bool{
	"con": true, "prn": true, "aux": true, "nul": true,
	"com0": true, "com1": true, "com2": true, "com3": true, "com4": true, "com5": true,
	"com6": true, "com7": true, "com8": true, "com9": true,
	"lpt0": true, "lpt1": true, "lpt2": true, "lpt3": true, "lpt4": true, "lpt5": true,
	"lpt6": true, "lpt7": true, "lpt8": true, "lpt9": true,
}

// deviceName is id's first dot-separated part when that part, in any case, is
// a Windows device name, and "" otherwise.
func deviceName(id string) string {
	first, _, _ := strings.Cut(id, ".")
	if windowsDeviceNames[strings.ToLower(first)] {
		return first
	}
	return ""
}

func idError(id string) error {
	if part := deviceName(id); part != "" {
		return fmt.Errorf("id '%s' is not a plugin id: its first part '%s' is a Windows device name", id, part)
	}
	return fmt.Errorf("id '%s' is not a plugin id: lower-case reverse-DNS, 3 to 64 characters, matching ^[a-z][a-z0-9]*(\\.[a-z][a-z0-9_]*)+$", id)
}

// The PEP 440 versions min_vizi accepts: the normalized public form without an
// epoch or a local label -- release segments, then an optional a/b/rc
// pre-release, .post and .dev ("1.0.0", "1.0rc1", "1.2.post3", "2.0.dev1").
var minViziPattern = regexp.MustCompile(`^[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$`)

// ValidMinVizi reports whether v is a min_vizi version this SDK accepts.
func ValidMinVizi(v string) bool { return minViziPattern.MatchString(v) }

func minViziError(v string) error {
	return fmt.Errorf("min_vizi '%s' is not a PEP 440 version", v)
}

var errSDKVersion = fmt.Errorf("sdk_version must be a non-empty string")
var errMinViziType = fmt.Errorf("min_vizi must be a string")

// PermissionIntroduced and SubscriptionIntroduced give, for each name this
// SDK knows, the protocol minor that introduced it. A name is honored only
// when the manifest's engines minor is at least that; otherwise it is warned
// about and ignored, like an unknown name. Each table is append-only within 1.x.
var PermissionIntroduced = map[string]int{"data_access": 0, "contribute_rois": 0}
var SubscriptionIntroduced = map[string]int{"epochs": 0, "view": 0, "regions": 0}

func honoured(table map[string]int, name string, minor int) bool {
	introduced, known := table[name]
	return known && minor >= introduced
}

// HonouredPermissions are the declared permissions a host honors for this
// manifest: known ones whose introduced minor is within engines.vizi.
func (m Manifest) HonouredPermissions() []string {
	_, minor, _ := ParseEngines(m.Engines.Vizi)
	var out []string
	for _, p := range m.Permissions {
		if honoured(PermissionIntroduced, p, minor) && !slices.Contains(out, p) {
			out = append(out, p)
		}
	}
	return out
}

// HonouredSubscriptions are the declared subscriptions a host arranges: known
// ones whose introduced minor is within engines.vizi, and for view and
// regions, only with data_access honored.
func (m Manifest) HonouredSubscriptions() []string {
	_, minor, _ := ParseEngines(m.Engines.Vizi)
	data := slices.Contains(m.HonouredPermissions(), "data_access")
	var out []string
	for _, s := range m.Subscriptions {
		if !honoured(SubscriptionIntroduced, s, minor) || slices.Contains(out, s) {
			continue
		}
		if (s == "view" || s == "regions") && !data {
			continue
		}
		out = append(out, s)
	}
	return out
}

// Warnings name each declared permission or subscription that is ignored.
func (m Manifest) Warnings() []string {
	_, minor, _ := ParseEngines(m.Engines.Vizi)
	data := slices.Contains(m.HonouredPermissions(), "data_access")
	var out []string
	for _, p := range m.Permissions {
		if !honoured(PermissionIntroduced, p, minor) {
			out = append(out, fmt.Sprintf("unknown permission '%s' grants nothing", p))
		}
	}
	for _, s := range m.Subscriptions {
		switch {
		case !honoured(SubscriptionIntroduced, s, minor):
			out = append(out, fmt.Sprintf("unknown subscription '%s' ignored", s))
		case (s == "view" || s == "regions") && !data:
			out = append(out, fmt.Sprintf("subscription '%s' needs data_access; ignored", s))
		}
	}
	return out
}
