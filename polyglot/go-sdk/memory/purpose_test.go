package memory

import "testing"

func TestUnknownAndKnownContributionSets(t *testing.T) {
	for _, tc := range []struct {
		name          string
		contributions map[string][]string
		accept        bool
	}{
		{"unknown", nil, true},
		{"known_empty", map[string][]string{}, false},
		{"known_match", map[string][]string{"O": {"o"}, "P": {"p"}}, true},
		{"known_other", map[string][]string{"O": {"other"}, "P": {"other"}}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, err := New(Config{Contributions: tc.contributions})
			if err != nil {
				t.Fatal(err)
			}
			defer r.Close()
			for i, item := range []struct {
				purpose, subject string
				shaped           bool
			}{
				{"O", "o", true}, {"P", "p", true},
				{"O", "", false}, {"P", "", false},
				{"G", "o", false}, {"V", "o", false}, {"X", "o", false},
			} {
				u, err := r.Install(InstallSegment{SegmentID: uint64(i + 1), Purpose: item.purpose, Subject: item.subject})
				if err != nil {
					t.Fatal(err)
				}
				if tc.accept && item.shaped {
					if u.Ack != nil {
						t.Fatal("valid subject refused", item, u)
					}
				} else if u.Ack == nil || u.Ack.Failure == nil || u.Ack.Failure.Class != Malformed {
					t.Fatal("invalid subject accepted", item, u)
				}
			}
		})
	}
}

func TestStandardPurposesAndArenaMultiplicity(t *testing.T) {
	r, err := New(Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	for _, p := range []string{PurposeReader, PurposeVolume, PurposePanel, PurposeOverlay, PurposeExport} {
		if !r.purposes[p] {
			t.Fatal("missing standard purpose", p)
		}
	}
	in := InstallSegment{SegmentID: 1, Purpose: PurposeReader, Regions: []Region{
		{Index: 1, Offset: 128, Extent: 64, Discipline: Loaned, Producer: Plugin},
		{Index: 0, Offset: 32, Extent: 64, Discipline: Loaned, Producer: Host},
	}}
	if f := validateInstall(in, r.purposes); f != nil {
		t.Fatal(f)
	}
	in.Regions[1].Producer = Plugin
	if f := validateInstall(in, r.purposes); f == nil || f.Class != Malformed {
		t.Fatal("ambiguous arena accepted", f)
	}
	in.Regions = in.Regions[:1]
	in.Regions[0].Index = 0
	in.Purpose = "g"
	if f := validateInstall(in, r.purposes); f == nil || f.Class != Unsupported {
		t.Fatal("purpose not matched exactly", f)
	}
	empty, err := New(Config{Purposes: []string{}})
	if err != nil {
		t.Fatal(err)
	}
	defer empty.Close()
	if len(empty.purposes) != 0 {
		t.Fatal("explicit empty purposes replaced with defaults")
	}
}

func TestSubjectAndPulsedLaneInstallContract(t *testing.T) {
	purposes := map[string]bool{"O": true, "P": true, "G": true, "V": true, "X": true}
	for purpose := range purposes {
		for _, subject := range []string{"", "overlay"} {
			in := InstallSegment{SegmentID: 1, Purpose: purpose, Subject: subject}
			f := validateInstall(in, purposes)
			want := (purpose == "O" || purpose == "P") == (subject != "")
			if (f == nil) != want || (f != nil && f.Class != Malformed) {
				t.Fatalf("%s %q: %v", purpose, subject, f)
			}
		}
	}
	tail := uint64(56)
	in := InstallSegment{SegmentID: 1, Purpose: "O", Subject: "overlay", Regions: []Region{
		{Index: 0, Offset: 32, Extent: 240, Discipline: Pulsed, Producer: Plugin, Buffers: 3, SlotBytes: 64, EpochOffset: &tail},
	}}
	if f := validateInstall(in, purposes); f != nil {
		t.Fatal(f)
	}
	in.Regions = append(in.Regions, in.Regions[0])
	in.Regions[1].Index = 1
	in.Regions[1].Offset = 272
	if f := validateInstall(in, purposes); f == nil || f.Class != Malformed {
		t.Fatal("two lanes accepted", f)
	}
	in.Regions = in.Regions[:1]
	tail = 0
	if f := validateInstall(in, purposes); f == nil || f.Class != Malformed {
		t.Fatal("header at payload origin accepted", f)
	}
}

func TestDeclaredSubjectsAreCheckedAndSnapshotted(t *testing.T) {
	subjects := map[string][]string{"O": {"overlay"}}
	r, err := New(Config{Contributions: subjects})
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	subjects["O"][0] = "changed"
	for i, subject := range []string{"overlay", "changed", "unknown"} {
		u, err := r.Install(InstallSegment{SegmentID: uint64(i + 1), Purpose: "O", Subject: subject})
		if err != nil {
			t.Fatal(err)
		}
		if subject == "overlay" {
			if u.Ack != nil {
				t.Fatal("declared subject rejected", u)
			}
		} else if u.Ack == nil || u.Ack.Failure == nil || u.Ack.Failure.Class != Malformed {
			t.Fatal("undeclared subject accepted", subject, u)
		}
	}
	tail := uint64(0)
	in := InstallSegment{SegmentID: 4, Purpose: "O", Subject: "overlay", Regions: []Region{{Offset: 32, Extent: 3 * (16 + 8), Discipline: Pulsed, Producer: Plugin, Buffers: 3, SlotBytes: 8, EpochOffset: &tail}}}
	u, err := r.Install(in)
	if err != nil || u.Ack == nil || u.Ack.Failure == nil || u.Ack.Failure.Class != Malformed {
		t.Fatal("header-only lane accepted", u, err)
	}
}
