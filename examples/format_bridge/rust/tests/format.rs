use format_bridge::{
    calibration,
    format::{Calibration, Crate, Header, Writer},
    offline,
};
use std::{
    io::Cursor,
    path::{Path, PathBuf},
};
use vizi::proto::error::Kind;

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../fixtures")
        .join(name)
}
fn open(name: &str) -> Crate<std::fs::File> {
    Crate::open(&fixture(name)).unwrap()
}
fn cal() -> Calibration {
    Calibration::load(&fixture("vizi-crate-cal.bin")).unwrap()
}
fn expected(t: u32, c: u32) -> Vec<u8> {
    let mut result = Vec::new();
    for z in 0..4 {
        for y in 0..6 {
            for x in 0..5 {
                result.extend_from_slice(
                    &((10000 * t + 1000 * c + 100 * z + 10 * y + x) as u16).to_le_bytes(),
                );
            }
        }
    }
    result
}
fn header(dtype: u16, shape: [u32; 5]) -> Header {
    Header {
        dtype,
        flags: 0,
        shape,
        calibration: [0.; 4],
        malformed_spacing: false,
    }
}
fn make(dtype: u16, bytes: &[u8], le: bool) -> Vec<u8> {
    let size = match dtype {
        1 => 1,
        2 => 2,
        _ => 4,
    };
    let mut w = Writer::begin(
        Cursor::new(Vec::new()),
        header(dtype, [1, 1, 1, 1, (bytes.len() / size) as u32]),
        "",
        "",
    )
    .unwrap();
    w.block(0, 0, bytes, le).unwrap();
    w.finish().unwrap().into_inner()
}
fn memory(b: Vec<u8>) -> Crate<Cursor<Vec<u8>>> {
    Crate::read(Cursor::new(b), "memory.crate").unwrap()
}

#[test]
fn independent_fixture_index_and_every_voxel() {
    let mut file = open("sample.crate");
    assert_eq!(file.header.shape, [3, 2, 4, 6, 5]);
    assert_eq!(file.header.dtype, 2);
    assert_eq!(
        file.index.iter().map(|e| e.offset).collect::<Vec<_>>(),
        [1746, 1506, 1266, 1026, 786, 546]
    );
    assert_eq!(
        file.index.iter().map(|e| e.crc).collect::<Vec<_>>(),
        [0x8d53b95b, 0x1c3015ab, 0xd56706b3, 0x03eaaa2e, 0x5889bb44, 0x5aa02fdd]
    );
    assert!(file.index.iter().all(|e| e.len == 240));
    for t in 0..3 {
        for c in 0..2 {
            assert_eq!(file.volume(t, c, true, None).unwrap(), expected(t, c));
            for z in 0..4 {
                assert_eq!(
                    file.plane(t, c, z, None).unwrap(),
                    expected(t, c)[z as usize * 60..(z as usize + 1) * 60]
                );
            }
        }
    }
    assert_eq!(file.header.calibration, [2., 0.325, 0.325, 1.5]);
    assert_eq!(
        file.header.label(),
        "2.000 × 0.325 × 0.325 µm (z × y × x), Δt 1.5 s"
    );
}

#[test]
fn calibrated_and_crc_failure_are_per_block() {
    let cal = cal();
    let mut calibrated = open("sample-calibrated.crate");
    assert!(calibrated
        .volume(0, 0, true, None)
        .unwrap_err()
        .message
        .contains("Plugin Manager"));
    let mut bad = open("badcrc.crate");
    for t in 0..3 {
        for c in 0..2 {
            assert_eq!(
                calibrated.volume(t, c, true, Some(&cal)).unwrap(),
                expected(t, c)
            );
            let got = bad.volume(t, c, true, None);
            if (t, c) == (1, 0) {
                let e = got.unwrap_err();
                assert_eq!(e.kind, Kind::IoError);
                assert!(e.message.contains("t=1, c=0"));
                assert_ne!(bad.volume(t, c, false, None).unwrap(), expected(t, c));
                assert!(bad.plane(t, c, 0, None).is_ok()); // deliberately no whole-block verification
            } else {
                assert_eq!(got.unwrap(), expected(t, c));
            }
        }
    }
    assert_eq!(bad.check().unwrap(), [(1, 0)]);
}

#[test]
fn calibration_uses_fields_and_diagnoses_only_partial_unknowns() {
    assert_eq!(
        calibration::resolve([2., 3., 4., 5.], "um", "s").source_diagnostic(),
        None
    );
    assert_eq!(
        calibration::resolve([0.; 4], "um", "s").source_diagnostic(),
        None
    );
    let partial = calibration::resolve([0., 3., 4., 5.], "um", "s");
    assert_eq!(partial.values, [0., 3., 4., 5.]);
    assert_eq!(
        partial.source_diagnostic().as_deref(),
        Some("z from unknown, y from fields, x from fields, t from fields")
    );
    assert_eq!(
        calibration::resolve([1.; 4], "furlong", "fortnight").values,
        [0.; 4]
    );
    for unit in ["\u{00b5}m", "\u{03bc}m"] {
        assert_eq!(
            calibration::resolve([2.; 4], unit, "ms").values,
            [2., 2., 2., 0.002]
        );
    }
    assert_eq!(
        calibration::resolve([-1., f64::INFINITY, f64::NAN, 0.], "um", "s").values,
        [0.; 4]
    );
    let mut conflict = open("spacing-conflict.crate");
    assert_eq!(conflict.header.calibration, [2., 0.325, 0.325, 1.5]);
    assert_eq!(conflict.volume(0, 0, true, None).unwrap(), expected(0, 0));
    let mut none = open("nospacing.crate");
    assert_eq!(none.header.label(), "(unknown)");
    let mut w = Writer::begin(
        Cursor::new(Vec::new()),
        none.header.clone(),
        &none.form,
        &none.text,
    )
    .unwrap();
    for t in 0..3 {
        for c in 0..2 {
            w.block(t, c, &none.volume(t, c, true, None).unwrap(), true)
                .unwrap();
        }
    }
    let mut again = memory(w.finish().unwrap().into_inner());
    assert_eq!(again.header.calibration, [0.; 4]);
    assert_eq!(again.volume(2, 1, true, None).unwrap(), expected(2, 1));
}

#[test]
fn byte_stability_scrambled_order_and_interop() {
    let mut source = open("sample.crate");
    let write = |slots: &[(u32, u32)]| {
        let mut w = Writer::begin(
            Cursor::new(Vec::new()),
            source.header.clone(),
            &source.form,
            &source.text,
        )
        .unwrap();
        for &(t, c) in slots {
            w.block(t, c, &expected(t, c), true).unwrap();
        }
        w.finish().unwrap().into_inner()
    };
    let ascending = [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)];
    let a = write(&ascending);
    assert_eq!(write(&ascending), a);
    assert_eq!(
        a,
        std::fs::read(fixture("../interop/python.crate")).unwrap()
    );
    let scrambled = write(&[(1, 1), (0, 0), (2, 1), (1, 0), (2, 0), (0, 1)]);
    assert_ne!(scrambled, a);
    let mut again = memory(scrambled);
    for (t, c) in ascending {
        assert_eq!(
            again.volume(t, c, true, None).unwrap(),
            source.volume(t, c, true, None).unwrap()
        );
    }
    for path in ["python.crate", "scala.crate", "go.crate"] {
        let path = fixture(&format!("../interop/{path}"));
        if path.exists() {
            let mut peer = Crate::open(&path).unwrap();
            for (t, c) in ascending {
                assert_eq!(peer.volume(t, c, true, None).unwrap(), expected(t, c));
            }
        }
    }
}

#[test]
fn hand_built_header_footer_and_endianness() {
    // Construct every byte independently; no production writer for expected bytes.
    let mut b = vec![0; 154];
    b[..8].copy_from_slice(b"VIZCRATE");
    b[8] = 1;
    b[10] = 2;
    for p in [16, 20, 24, 28, 32] {
        b[p] = 1;
    }
    b[96..98].copy_from_slice(&[0x34, 0x12]);
    b[98..106].copy_from_slice(&96u64.to_le_bytes());
    b[106..114].copy_from_slice(&2u64.to_le_bytes());
    b[114..118].copy_from_slice(&crc32fast::hash(&[0x34, 0x12]).to_le_bytes());
    b[122..130].copy_from_slice(&98u64.to_le_bytes());
    b[130..138].copy_from_slice(&1u64.to_le_bytes());
    let index_crc = crc32fast::hash(&b[98..122]);
    b[138..142].copy_from_slice(&index_crc.to_le_bytes());
    b[146..].copy_from_slice(b"ETARCZIV");
    assert_eq!(make(2, &[0x12, 0x34], false), b);
    assert_eq!(
        memory(b.clone()).volume(0, 0, true, None).unwrap(),
        [0x34, 0x12]
    );
    for p in [
        42, 43, 44, 45, 46, 47, 88, 95, 118, 119, 120, 121, 142, 143, 144, 145,
    ] {
        b[p] = 0xa5;
    }
    let index_crc = crc32fast::hash(&b[98..122]);
    b[138..142].copy_from_slice(&index_crc.to_le_bytes());
    assert_eq!(memory(b).volume(0, 0, true, None).unwrap(), [0x34, 0x12]);
    let mut f = memory(make(3, &[0x3f, 0x80, 0, 0, 0xc0, 0, 0, 0], false));
    assert_eq!(
        f.volume(0, 0, true, None).unwrap(),
        [0, 0, 0x80, 0x3f, 0, 0, 0, 0xc0]
    );
    assert_eq!(
        memory(make(1, &[1, 255], false))
            .volume(0, 0, true, None)
            .unwrap(),
        [1, 255]
    );
}

#[test]
fn malformed_metadata_is_not_a_voxel_failure() {
    let mut source = std::fs::read(fixture("sample.crate")).unwrap();
    for (i, v) in [-1., f64::INFINITY, f64::NAN, 0.].iter().enumerate() {
        source[48 + 8 * i..56 + 8 * i].copy_from_slice(&v.to_le_bytes());
    }
    source[103] = 0xff;
    let mut file = memory(source);
    assert_eq!(file.header.calibration, [0.; 4]);
    assert!(file.header.malformed_spacing);
    assert!(file.invalid_utf8);
    assert_eq!((file.form.as_str(), file.text.as_str()), ("", ""));
    assert_eq!(file.volume(0, 0, true, None).unwrap(), expected(0, 0));
    let mut w = Writer::begin(
        Cursor::new(Vec::new()),
        header(1, [1; 5]),
        "other-form",
        "opaque",
    )
    .unwrap();
    w.block(0, 0, &[7], true).unwrap();
    let other = memory(w.finish().unwrap().into_inner());
    assert_eq!(
        (other.form.as_str(), other.text.as_str()),
        ("other-form", "opaque")
    );
}

#[test]
fn malformed_geometry_footer_index_and_bounds_fail_cleanly() {
    let unfinished = Crate::open(&fixture("unfinalized.crate")).err().unwrap();
    assert_eq!(unfinished.kind, Kind::IoError);
    assert!(unfinished.message.contains("unfinished"));
    for (i, axis) in ["t", "c", "z", "y", "x"].iter().enumerate() {
        let mut b = make(1, &[1], true);
        b[16 + i * 4..20 + i * 4].fill(0);
        let e = Crate::read(Cursor::new(b), "zero.crate").err().unwrap();
        assert_eq!(e.kind, Kind::InvalidArgument);
        assert!(e.message.contains(&format!("axis {axis}")));
    }
    let mut b = make(1, &[1], true);
    b[97] ^= 1;
    assert!(Crate::read(Cursor::new(b), "badindex.crate")
        .err()
        .unwrap()
        .message
        .contains("index CRC"));
    let mut file = open("sample.crate");
    for (t, c) in [(3, 0), (0, 2)] {
        assert_eq!(
            file.volume(t, c, true, None).unwrap_err().kind,
            Kind::OutOfBounds
        );
    }
    assert_eq!(
        file.plane(0, 0, 4, None).unwrap_err().kind,
        Kind::OutOfBounds
    );
    for len in [0, 8, 95, 96, 100] {
        assert!(Crate::read(
            Cursor::new(std::fs::read(fixture("sample.crate")).unwrap()[..len].to_vec()),
            "short.crate"
        )
        .is_err());
    }
}

#[test]
fn calibration_saturates_and_writer_replaces_duplicate_blocks() {
    let positive = Calibration([(0, 10)].into());
    let negative = Calibration([(0, -10)].into());
    let mut b = [5, 250];
    positive.apply(1, 0, &mut b);
    assert_eq!(b, [0, 240]);
    negative.apply(1, 0, &mut b);
    assert_eq!(b, [10, 250]);
    negative.apply(1, 0, &mut b);
    assert_eq!(b, [20, 255]);
    let mut b = [0xff, 0xff, 5, 0];
    negative.apply(2, 0, &mut b);
    assert_eq!(b, [0xff, 0xff, 15, 0]);
    let mut b = 5f32.to_le_bytes();
    positive.apply(3, 0, &mut b);
    assert_eq!(f32::from_le_bytes(b), -5.);
    assert!(Calibration::parse(b"VIZCAL01").is_err());
    let mut w = Writer::begin(Cursor::new(Vec::new()), header(1, [1; 5]), "", "").unwrap();
    w.block(0, 0, &[1], true).unwrap();
    w.block(0, 0, &[2], true).unwrap();
    assert_eq!(
        memory(w.finish().unwrap().into_inner())
            .volume(0, 0, true, None)
            .unwrap(),
        [2]
    );
    let w = Writer::begin(Cursor::new(Vec::new()), header(1, [1; 5]), "", "").unwrap();
    assert!(w
        .finish()
        .unwrap_err()
        .message
        .contains("1 of 1 blocks missing"));
}

#[test]
fn comparator_reports_all_tiers_and_refuses_unopenable_inputs() {
    let sample = fixture("sample.crate");
    assert_eq!(
        offline::compare(&sample, &sample, None).unwrap(),
        "byte-equal"
    );
    assert_eq!(
        offline::compare(&sample, &fixture("../interop/python.crate"), None).unwrap(),
        "semantically equal"
    );
    assert_eq!(
        offline::compare(&sample, &fixture("sample-calibrated.crate"), Some(&cal())).unwrap(),
        "semantically equal"
    );
    assert!(offline::compare(&sample, &fixture("nospacing.crate"), None)
        .unwrap()
        .starts_with("voxel-equal"));
    assert!(offline::compare(&sample, &fixture("badcrc.crate"), None)
        .unwrap()
        .starts_with("different"));
    let unfinalized = fixture("unfinalized.crate");
    assert!(offline::compare(&unfinalized, &unfinalized, None).is_err());
    let calibrated = fixture("sample-calibrated.crate");
    assert!(offline::compare(&calibrated, &calibrated, None).is_err());
}

// Independently encode all four sections, the block, index and footer.
fn section_fixture(flags: u32, names: &[u8]) -> Vec<u8> {
    let (form, text, record) = (
        b"ome-xml".as_slice(),
        b"<source/>".as_slice(),
        b"{\"source\":\"x\"}".as_slice(),
    );
    let mut b = vec![0; 96];
    b[..8].copy_from_slice(b"VIZCRATE");
    b[8] = 1;
    b[10] = 1;
    b[12..16].copy_from_slice(&flags.to_le_bytes());
    for p in [16, 20, 24, 28, 32] {
        b[p] = 1;
    }
    b[20] = 3;
    b[36..38].copy_from_slice(&(form.len() as u16).to_le_bytes());
    b[38..42].copy_from_slice(&(text.len() as u32).to_le_bytes());
    b[80..84].copy_from_slice(&(record.len() as u32).to_le_bytes());
    b[84..88].copy_from_slice(&(names.len() as u32).to_le_bytes());
    b.extend(form);
    b.extend(text);
    b.extend(record);
    b.extend(names);
    let block_start = b.len();
    b.extend([10, 20, 30]);
    let index_start = b.len();
    for (i, voxel) in [10, 20, 30].into_iter().enumerate() {
        b.extend(((block_start + i) as u64).to_le_bytes());
        b.extend(1u64.to_le_bytes());
        b.extend(crc32fast::hash(&[voxel]).to_le_bytes());
        b.extend([0; 4]);
    }
    let index_crc = crc32fast::hash(&b[index_start..]);
    b.extend((index_start as u64).to_le_bytes());
    b.extend(3u64.to_le_bytes());
    b.extend(index_crc.to_le_bytes());
    b.extend([0; 4]);
    b.extend(b"ETARCZIV");
    b
}

#[test]
fn hand_built_sections_offsets_names_and_malformed_names() {
    use format_bridge::format::DERIVED;
    for (raw_names, expected_names, malformed) in [
        (b"DNA\0\0RNA".as_slice(), vec!["DNA", "", "RNA"], false),
        (b"\0RNA".as_slice(), vec!["", "RNA"], false),
        (b"".as_slice(), vec![], false),
        (b"a\0b\0c\0d".as_slice(), vec![], true),
        (b"\xff".as_slice(), vec![], true),
    ] {
        let mut file = memory(section_fixture(DERIVED, raw_names));
        assert_eq!(file.header.flags, DERIVED);
        assert_eq!((&*file.form, &*file.text), ("ome-xml", "<source/>"));
        assert_eq!(file.provenance, b"{\"source\":\"x\"}");
        assert_eq!(file.names, expected_names);
        assert_eq!(file.malformed_names, malformed);
        assert_eq!(
            file.index[0].offset,
            (96 + 7 + 9 + 14 + raw_names.len()) as u64
        );
        for (c, v) in [10, 20, 30].into_iter().enumerate() {
            assert_eq!(file.volume(0, c as u32, true, None).unwrap(), [v]);
        }
    }
    // A corrupt index pointing inside the new sections must be refused even
    // when its CRC is correct and the bytes would otherwise be readable.
    let mut b = section_fixture(DERIVED, b"DNA");
    let index = b.len() - 32 - 3 * 24;
    b[index..index + 8].copy_from_slice(&96u64.to_le_bytes());
    let crc = crc32fast::hash(&b[index..index + 72]);
    b[index + 88..index + 92].copy_from_slice(&crc.to_le_bytes());
    assert!(Crate::read(Cursor::new(b), "overlap")
        .err()
        .unwrap()
        .message
        .contains("outside block region"));
}

#[test]
fn writer_sections_match_hand_built_bytes_and_sanitize_names() {
    use format_bridge::format::DERIVED;
    let mut h = header(1, [1, 3, 1, 1, 1]);
    h.flags = DERIVED | 1 | 128;
    let mut w = Writer::begin_sections(
        Cursor::new(Vec::new()),
        h.clone(),
        "ome-xml",
        "<source/>",
        b"{\"source\":\"x\"}",
        &["DNA".into(), "".into(), "RNA".into()],
    )
    .unwrap();
    for (c, v) in [10, 20, 30].into_iter().enumerate() {
        w.block(0, c as u32, &[v], true).unwrap();
    }
    assert_eq!(
        w.finish().unwrap().into_inner(),
        section_fixture(DERIVED, b"DNA\0\0RNA")
    );
    for (names, expected) in [
        (vec!["bad\0name", "RNA", ""], vec!["", "RNA"]),
        (vec!["", "", ""], vec![]),
        (vec!["DNA", "", ""], vec!["DNA"]),
    ] {
        let mut w = Writer::begin_sections(
            Cursor::new(Vec::new()),
            h.clone(),
            "",
            "",
            &[],
            &names.into_iter().map(str::to_owned).collect::<Vec<_>>(),
        )
        .unwrap();
        for c in 0..3 {
            w.block(0, c, &[0], true).unwrap();
        }
        assert_eq!(memory(w.finish().unwrap().into_inner()).names, expected);
    }
}

#[test]
fn recreate_carries_sections_and_comparator_counts_names_only() {
    use format_bridge::format::DERIVED;
    let dir = tempfile::tempdir().unwrap();
    let a = dir.path().join("a.crate");
    let b = dir.path().join("b.crate");
    let c = dir.path().join("c.crate");
    std::fs::write(&a, section_fixture(DERIVED, b"DNA\0\0RNA")).unwrap();
    offline::recreate(&a, &b, "ascending", None).unwrap();
    assert_eq!(std::fs::read(&a).unwrap(), std::fs::read(&b).unwrap());
    assert!(offline::compare(&a, &b, None)
        .unwrap()
        .starts_with("byte-equal"));
    std::fs::write(&c, section_fixture(0, b"DNA\0\0RNA")).unwrap();
    assert!(offline::compare(&a, &c, None)
        .unwrap()
        .starts_with("semantically equal"));
    let mut changed = std::fs::read(&c).unwrap();
    changed[96 + 7 + 9 + 11] = b'y'; // different provenance, same length
    std::fs::write(&c, changed).unwrap();
    assert!(offline::compare(&a, &c, None)
        .unwrap()
        .starts_with("semantically equal"));
    std::fs::write(&c, section_fixture(DERIVED, b"DNA\0\0other")).unwrap();
    let verdict = offline::compare(&a, &c, None).unwrap();
    assert!(verdict.starts_with("voxel-equal") && verdict.contains("channel names differ"));
}
