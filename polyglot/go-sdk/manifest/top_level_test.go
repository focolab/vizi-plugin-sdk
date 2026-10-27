package manifest

import (
	"slices"
	"strings"
	"testing"
)

// A permission or subscription is honored only from the minor that
// introduced it; before that, and when unknown, it is warned about and ignored.
func TestIntroducedMinors(t *testing.T) {
	m := valid()
	m.Permissions = []string{"data_access", "teleport"}
	m.Subscriptions = []string{"view", "epochs", "future"}
	if got := m.HonouredPermissions(); !slices.Equal(got, []string{"data_access"}) {
		t.Fatal(got)
	}
	if got := m.HonouredSubscriptions(); !slices.Equal(got, []string{"view", "epochs"}) {
		t.Fatal(got)
	}
	if got := m.Warnings(); !slices.Equal(got, []string{"unknown permission 'teleport' grants nothing", "unknown subscription 'future' ignored"}) {
		t.Fatal(got)
	}
	PermissionIntroduced["teleport"] = 1
	defer delete(PermissionIntroduced, "teleport")
	if got := m.HonouredPermissions(); !slices.Equal(got, []string{"data_access"}) {
		t.Fatal("a name introduced at 1.1 is honored under engines 1.0:", got)
	}
	m.Permissions = []string{"teleport"}
	if got := m.HonouredSubscriptions(); !slices.Equal(got, []string{"epochs"}) {
		t.Fatal("view without data_access:", got)
	}
	for k, v := range PermissionIntroduced {
		if k != "teleport" && v != 0 {
			t.Fatal(k, v)
		}
	}
	for k, v := range SubscriptionIntroduced {
		if v != 0 {
			t.Fatal(k, v)
		}
	}
}

// An id whose first part is a Windows device name, in any case, is refused
// with its own sentence: installers use the id as a folder name. A device name
// as a later part, or inside a longer first part, is an ordinary id; every
// other refusal keeps the grammar sentence.
func TestWindowsDeviceNameIDs(t *testing.T) {
	refused := map[string]string{
		"con.example":  "id 'con.example' is not a plugin id: its first part 'con' is a Windows device name",
		"lpt1.tool":    "id 'lpt1.tool' is not a plugin id: its first part 'lpt1' is a Windows device name",
		"LPT1.tool":    "id 'LPT1.tool' is not a plugin id: its first part 'LPT1' is a Windows device name",
		"com9.x":       "id 'com9.x' is not a plugin id: its first part 'com9' is a Windows device name",
		"com0.x":       "id 'com0.x' is not a plugin id: its first part 'com0' is a Windows device name",
		"lpt0.x":       "id 'lpt0.x' is not a plugin id: its first part 'lpt0' is a Windows device name",
		"nul.a.b":      "id 'nul.a.b' is not a plugin id: its first part 'nul' is a Windows device name",
		"Aux":          "id 'Aux' is not a plugin id: its first part 'Aux' is a Windows device name",
		"prn.x":        "id 'prn.x' is not a plugin id: its first part 'prn' is a Windows device name",
		"org.Example":  "id 'org.Example' is not a plugin id: lower-case reverse-DNS, 3 to 64 characters",
		"exampletool":  "id 'exampletool' is not a plugin id: lower-case reverse-DNS, 3 to 64 characters",
		"my_org.thing": "id 'my_org.thing' is not a plugin id: lower-case reverse-DNS, 3 to 64 characters",
	}
	for id, want := range refused {
		m := valid()
		m.ID = id
		err := m.Validate()
		if ValidID(id) || err == nil || !strings.HasPrefix(err.Error(), want) {
			t.Errorf("%s: %v; want %q", id, err, want)
		}
	}
	for _, id := range []string{"console.example", "com10.x", "example.con", "org.lpt1.aux", "conx.nul"} {
		m := valid()
		m.ID = id
		if !ValidID(id) || m.Validate() != nil {
			t.Errorf("%s refused: %v", id, m.Validate())
		}
	}
}
