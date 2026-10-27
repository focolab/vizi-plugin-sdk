use super::*;
use vizi::manifest::Manifest;

#[test]
fn bands_cover_edges_nonfinite_values_and_integer_inputs() {
    for (value, expected) in [
        (399.999, 0),
        (400.0, 1),
        (599.0, 1),
        (600.0, 2),
        (800.0, 3),
        (1e100, 3),
        (f64::NAN, 0),
        (f64::NEG_INFINITY, 0),
        (f64::INFINITY, 3),
    ] {
        assert_eq!(bands::label(value, 400.0), expected, "{value}");
    }
    assert_eq!(bands::label(f64::from(600u16), 400.0), 2);
    assert_eq!(bands::label(0.0, 0.0), 1);
    assert_eq!(bands::label(-0.01, 0.0), 0);
}

#[test]
fn tight_box_keeps_zeros_and_uses_off_origin_zyx_coordinates() {
    let mut labels = vec![0; 3 * 4 * 5];
    labels[(4 + 1) * 5 + 2] = 1;
    labels[(2 * 4 + 2) * 5 + 3] = 3;
    let chunk = bands::tight(&labels, [3, 4, 5]).unwrap();
    assert_eq!(chunk.position_zyx, [1, 1, 2]);
    assert_eq!(chunk.labels.shape_zyx, [2, 2, 2]);
    assert_eq!(chunk.labels.bytes, [1, 0, 0, 0, 0, 0, 0, 3]);
    assert_eq!(chunk.labels.dtype, pb::DType::DtypeU8);
    assert!(bands::tight(&[0; 8], [2, 2, 2]).is_none());
    let full = bands::tight(&[2; 8], [2, 2, 2]).unwrap();
    assert_eq!(full.position_zyx, [0; 3]);
    assert_eq!(full.labels.shape_zyx, [2; 3]);
    assert_eq!(full.labels.bytes, [2; 8]);
}

#[test]
fn footprint_unions_three_frames_and_nan_is_never_foreground() {
    let mut mask = [0; 5];
    for values in [
        [400.0, 0.0, 0.0, f32::NAN, 0.0],
        [0.0, 500.0, 0.0, f32::NAN, 0.0],
        [0.0, 0.0, f32::INFINITY, f32::NAN, 399.0],
    ] {
        bands::union_into(&mut mask, &values, 400.0);
    }
    assert_eq!(mask, [1, 1, 1, 0, 0]);
}

#[test]
fn tables_pin_all_rows_names_visibility_and_style() {
    let table = bands::bands_table([true, false, true]);
    assert_eq!(table.colormap[0], [0; 4]);
    assert_eq!(
        table.colormap[1..4],
        [[60, 120, 255, 255], [255, 200, 0, 0], [255, 60, 60, 255]]
    );
    assert!(table.colormap[4..].iter().all(|r| r == &[0; 4]));
    assert_eq!(
        table.label_names,
        [(1, "dim".into()), (2, "mid".into()), (3, "bright".into())].into()
    );
    assert_eq!(
        table.label_visible,
        [(1, true), (2, false), (3, true)].into()
    );
    assert_eq!(
        (
            table.style.opacity,
            table.style.hidden,
            table.style.draw_priority
        ),
        (0.6, false, 1)
    );
    let table = bands::footprint_table();
    assert_eq!(table.colormap[0], [0; 4]);
    assert_eq!(table.colormap[1], [200, 200, 200, 255]);
    assert!(table.colormap[2..].iter().all(|r| r == &[0; 4]));
    assert_eq!(table.label_names, [(1, "ever".into())].into());
    assert!(table.label_visible.is_empty());
    assert_eq!(
        (
            table.style.opacity,
            table.style.hidden,
            table.style.draw_priority
        ),
        (0.3, false, 0)
    );
    let visible = bands::bands_table([true; 3]);
    let hidden = bands::bands_table([false; 3]);
    for band in 1..=3 {
        assert_eq!(visible.colormap[band][3], 255);
        assert_eq!(hidden.colormap[band][3], 0);
        assert_eq!(visible.colormap[band][..3], hidden.colormap[band][..3]);
    }
}
fn command(args: &[(&str, Kind)]) -> pb::InvokeCommand {
    pb::InvokeCommand {
        args: args
            .iter()
            .map(|(k, v)| {
                (
                    (*k).into(),
                    pb::Value {
                        kind: Some(v.clone()),
                    },
                )
            })
            .collect(),
        ..Default::default()
    }
}

#[test]
fn arguments_accept_numeric_text_and_reject_nonfinite_or_wrong_types() {
    for invalid in [
        Kind::B(true),
        Kind::S("high".into()),
        Kind::S("".into()),
        Kind::S("   ".into()),
        Kind::S("NaN".into()),
        Kind::S("inf".into()),
        Kind::S("1e999".into()),
        Kind::S(" -1 ".into()),
        Kind::I(-1),
        Kind::F(-0.1),
        Kind::F(f64::NAN),
        Kind::F(f64::INFINITY),
    ] {
        assert_eq!(threshold_arg(&command(&[("value", invalid)])), None);
    }
    assert_eq!(
        threshold_arg(&command(&[("value", Kind::I(400))])),
        Some(400.0)
    );
    assert_eq!(
        threshold_arg(&command(&[("value", Kind::F(1.25))])),
        Some(1.25)
    );
    assert_eq!(threshold_arg(&command(&[])), None);
    for (text, expected) in [
        ("400", 400.0),
        (" 800 ", 800.0),
        ("\t1.25\n", 1.25),
        ("4e2", 400.0),
        ("-0", 0.0),
    ] {
        assert_eq!(
            threshold_arg(&command(&[("value", Kind::S(text.into()))])),
            Some(expected)
        );
    }
    for band in [Kind::B(true), Kind::F(2.0), Kind::I(0), Kind::I(7)] {
        assert_eq!(
            visibility_args(&command(&[("band", band), ("visible", Kind::B(true))])),
            None
        );
    }
    assert_eq!(
        visibility_args(&command(&[
            ("band", Kind::I(2)),
            ("visible", Kind::S("false".into()))
        ])),
        None
    );
    assert_eq!(
        visibility_args(&command(&[
            ("band", Kind::I(2)),
            ("visible", Kind::B(false))
        ])),
        Some((1, false))
    );
}

#[test]
fn replies_use_pinned_spelling_and_shortest_decimal_thresholds() {
    assert_eq!(
        threshold_reply(400.0, 3),
        "threshold 400; 3 wanted volume(s) to serve again"
    );
    assert_eq!(
        threshold_reply(400.125, 0),
        "threshold 400.125; 0 wanted volume(s) to serve again"
    );
    assert_eq!(bands::threshold_text(-0.0), "0");
    assert_eq!(bands::threshold_text(0.1), "0.1");
    assert_eq!(visibility_reply(1, false), "band 2 (mid) hidden");
    assert_eq!(visibility_reply(1, true), "band 2 (mid) visible");
}

#[test]
fn manifest_and_chunked_field_restrictions() {
    let json = include_str!("../vizi-plugin.json");
    let manifest = Manifest::parse(json).unwrap();
    assert_eq!(manifest.id(), "org.example.movie_seg");
    let mut arena = manifest.value().clone();
    arena["contributions"]["overlays"][0]["segment_mib"] = 32.into();
    assert!(Manifest::parse(&arena.to_string()).is_err());
    let mut unchunked = manifest.value().clone();
    unchunked["contributions"]["overlays"][0]
        .as_object_mut()
        .unwrap()
        .remove("chunked");
    assert!(Manifest::parse(&unchunked.to_string()).is_err());
    let mut non_boolean = manifest.value().clone();
    non_boolean["contributions"]["overlays"][0]["chunked"] = 1.into();
    assert!(Manifest::parse(&non_boolean.to_string()).is_err());
    let mut unknown_time = manifest.value().clone();
    unknown_time["contributions"]["overlays"][0]["time"] = "always".into();
    assert!(Manifest::parse(&unknown_time.to_string()).is_err());
}
