//! Region messages keep the fields a later 1.x host adds, at every depth, through
//! decode, edit and re-encode.

use prost::encoding::{encode_key, encode_varint, WireType};
use prost::Message;
use vizi_doorbell::proto::{self as pb, pois_math::v1 as roi};

/// One unknown record of every wire type, numbered from `base`: varint,
/// 64-bit, length-delimited, 32-bit, and a group holding a nested group.
fn records(base: u32) -> Vec<u8> {
    let mut b = Vec::new();
    encode_key(base, WireType::Varint, &mut b);
    encode_varint(300, &mut b);
    encode_key(base + 1, WireType::SixtyFourBit, &mut b);
    b.extend_from_slice(&0x0102_0304_0506_0708u64.to_le_bytes());
    encode_key(base + 2, WireType::LengthDelimited, &mut b);
    encode_varint(5, &mut b);
    b.extend_from_slice(b"newer");
    encode_key(base + 3, WireType::ThirtyTwoBit, &mut b);
    b.extend_from_slice(&[9, 8, 7, 6]);
    encode_key(base + 4, WireType::StartGroup, &mut b);
    encode_key(1, WireType::Varint, &mut b);
    encode_varint(7, &mut b);
    encode_key(2, WireType::StartGroup, &mut b);
    encode_key(3, WireType::LengthDelimited, &mut b);
    encode_varint(1, &mut b);
    b.push(b'g');
    encode_key(2, WireType::EndGroup, &mut b);
    encode_key(base + 4, WireType::EndGroup, &mut b);
    b
}

fn nested(tag: u32, body: &[u8], out: &mut Vec<u8>) {
    encode_key(tag, WireType::LengthDelimited, out);
    encode_varint(body.len() as u64, out);
    out.extend_from_slice(body);
}

/// A region as a later host would send it: known fields in prost's order,
/// each message followed by records this build does not know.
fn newer_roi(type_name: &str, label: &str, x: f64) -> Vec<u8> {
    let mut position = Vec::new();
    prost::encoding::double::encode(1, &1.5, &mut position);
    if x != 0.0 {
        prost::encoding::double::encode(3, &x, &mut position);
    }
    position.extend(records(40));

    let mut vertex = Vec::new();
    nested(1, &position, &mut vertex);
    prost::encoding::string::encode(10, &label.to_owned(), &mut vertex);
    vertex.extend(records(60));

    let mut parent = Vec::new();
    prost::encoding::string::encode(2, &"base".to_owned(), &mut parent);
    parent.extend(records(90));

    let mut r = Vec::new();
    prost::encoding::string::encode(1, &type_name.to_owned(), &mut r);
    nested(3, &vertex, &mut r);
    nested(29, &parent, &mut r);
    r.extend(records(100));
    r
}

#[test]
fn a_region_keeps_unknown_fields_at_every_depth_byte_for_byte() {
    let bytes = newer_roi("tetra", "a", 0.0);
    let r = roi::Roi::decode(bytes.as_slice()).unwrap();
    assert_eq!(r.type_name, "tetra");
    assert_eq!(r.unknown_fields.as_bytes(), records(100));
    let v = &r.vertices[0];
    assert_eq!(v.label, "a");
    assert_eq!(v.unknown_fields.as_bytes(), records(60));
    let p = v.position.as_ref().unwrap();
    assert_eq!((p.z, p.x), (1.5, 0.0));
    assert_eq!(p.unknown_fields.as_bytes(), records(40));
    let parent = r.parent.as_ref().unwrap();
    assert_eq!(parent.region_id, "base");
    assert_eq!(parent.unknown_fields.as_bytes(), records(90));
    // Unchanged, it encodes to exactly what arrived.
    assert_eq!(r.encoded_len(), bytes.len());
    assert_eq!(r.encode_to_vec(), bytes);
}

#[test]
fn editing_known_fields_keeps_unknown_fields() {
    let mut r = roi::Roi::decode(newer_roi("tetra", "a", 0.0).as_slice()).unwrap();
    r.type_name = "tetra2".into();
    r.vertices[0].label = "b".into();
    r.vertices[0].position.as_mut().unwrap().x = 2.5;
    let out = r.encode_to_vec();
    assert_eq!(out, newer_roi("tetra2", "b", 2.5));
    assert_eq!(r.encoded_len(), out.len());
}

#[test]
fn a_region_keeps_unknown_fields_through_the_control_plane_envelopes() {
    let region = roi::Roi::decode(newer_roi("tetra", "a", 0.0).as_slice()).unwrap();
    let reply = pb::HostMsg {
        id: "q1".into(),
        msg: Some(pb::host_msg::Msg::Rois(pb::RoiList {
            rois: vec![pb::RoiRecord {
                region_id: "r1".into(),
                roi: Some(region),
                ..Default::default()
            }],
        })),
    };
    let reply = pb::HostMsg::decode(reply.encode_to_vec().as_slice()).unwrap();
    let Some(pb::host_msg::Msg::Rois(list)) = reply.msg else {
        panic!("{reply:?}")
    };
    let mut region = list.rois[0].roi.clone().unwrap();
    region.vertices[0].label = "b".into();
    let contribution = pb::PluginMsg {
        id: "p1".into(),
        msg: Some(pb::plugin_msg::Msg::ContributeRois(pb::ContributeRois {
            rois: vec![pb::RoiContribution {
                contrib_id: "c".into(),
                roi: Some(region),
                ..Default::default()
            }],
            ..Default::default()
        })),
    };
    let sent = pb::PluginMsg::decode(contribution.encode_to_vec().as_slice()).unwrap();
    let Some(pb::plugin_msg::Msg::ContributeRois(c)) = sent.msg else {
        panic!("{sent:?}")
    };
    let region = c.rois[0].roi.as_ref().unwrap();
    assert_eq!(region.encode_to_vec(), newer_roi("tetra", "b", 0.0));
}

#[test]
fn clearing_unknown_fields_encodes_known_fields_only() {
    let mut r = roi::Roi::decode(newer_roi("tetra", "a", 0.0).as_slice()).unwrap();
    r.unknown_fields.clear();
    let mut plain = Vec::new();
    prost::encoding::string::encode(1, &"tetra".to_owned(), &mut plain);
    let tail = r.encode_to_vec();
    assert!(tail.starts_with(&plain));
    let again = roi::Roi::decode(tail.as_slice()).unwrap();
    assert!(again.unknown_fields.is_empty());
    assert_eq!(again.vertices[0].unknown_fields.as_bytes(), records(60));
}

#[test]
fn every_region_message_keeps_unknown_fields() {
    fn check<M: Message + Default>(name: &str, unknown: impl Fn(&M) -> &pb::UnknownFields) {
        let m = M::decode(records(1000).as_slice()).unwrap();
        assert_eq!(unknown(&m).as_bytes(), records(1000), "{name}");
        assert_eq!(m.encode_to_vec(), records(1000), "{name}");
    }
    macro_rules! all {
        ($($t:ident),* $(,)?) => {
            $(check::<roi::$t>(stringify!($t), |m| &m.unknown_fields);)*
        };
    }
    all!(
        Vec3,
        HandleSpec,
        HandleStyle,
        DirectionHandle,
        Vertex,
        Edge,
        Facet,
        Patch,
        CameraView,
        LinearBinding,
        DirectionSpec,
        CompileTriplet,
        CompileOperator,
        Inclusion,
        Parent,
        Roi,
        RoiSeq,
        RoiSet,
        Box,
        Run,
        Mask,
        ConstrainMoveRequest,
        ConstrainMoveResponse,
        RefurbishRequest,
        RefurbishResponse,
        VoxelInclusionRequest,
        VoxelInclusionResponse,
        CompileRequest,
        CompileResponse,
        ListTypesRequest,
        ListTypesResponse,
        DescribeTypeRequest,
        DescribeTypeResponse,
    );
}

#[test]
fn known_fields_agree_with_the_descriptor_codec() {
    // Every field kind: implicit-presence scalars, optional scalars, enums,
    // packed numbers, strings, nested and repeated messages, and a map.
    let r = roi::Roi {
        type_name: "t".into(),
        vertices: vec![roi::Vertex {
            position: Some(roi::Vec3 {
                z: 1.,
                y: -0.0,
                x: 3.,
                ..Default::default()
            }),
            scalars: vec![1., 2.],
            handle: Some(roi::HandleSpec {
                frame: vec![1.; 9],
                mode: roi::DofMode::All as i32,
                min_extent: Some(0.0),
                ..Default::default()
            }),
            enclosing: true,
            ..Default::default()
        }],
        attrs: [("k".to_owned(), "v".to_owned())].into(),
        t_start: Some(0),
        root: -1,
        pose_translation: vec![0., 1.],
        ..Default::default()
    };
    let bytes = r.encode_to_vec();
    assert_eq!(bytes.len(), r.encoded_len());
    // A descriptor-driven codec reads every field and writes the same region.
    let pool = prost_reflect::DescriptorPool::decode(
        include_bytes!(concat!(env!("OUT_DIR"), "/vizi.bin")).as_slice(),
    )
    .unwrap();
    let descriptor = pool.get_message_by_name("pois_math.v1.Roi").unwrap();
    let dynamic = prost_reflect::DynamicMessage::decode(descriptor, bytes.as_slice()).unwrap();
    assert_eq!(
        dynamic.get_field_by_name("root").unwrap().as_i32(),
        Some(-1)
    );
    assert_eq!(
        dynamic.get_field_by_name("t_start").unwrap().as_i32(),
        Some(0)
    );
    assert!(dynamic.has_field_by_name("t_start") && !dynamic.has_field_by_name("t_end"));
    // Both write fields in number order (`root` is declared before `t_start`
    // but numbered after it), so the bytes match exactly.
    assert_eq!(dynamic.encode_to_vec(), bytes);
    let back = roi::Roi::decode(bytes.as_slice()).unwrap();
    assert_eq!(back, r);
    assert!(back.unknown_fields.is_empty());
    let handle = back.vertices[0].handle.as_ref().unwrap();
    assert_eq!(handle.mode(), roi::DofMode::All);
    assert_eq!(handle.min_extent(), 0.0);
    assert_eq!(handle.max_extent(), 0.0);
    assert_eq!(back.t_start(), 0);
    let mut parent = roi::Parent::default();
    parent.set_kind(roi::ParentKind::ParentRegion);
    assert_eq!(parent.kind(), roi::ParentKind::ParentRegion);
    // A later host's enum value stays as its number.
    parent.kind = 9;
    assert_eq!(parent.kind(), roi::ParentKind::ParentWorld);
    assert_eq!(
        roi::Parent::decode(&parent.encode_to_vec()[..])
            .unwrap()
            .kind,
        9
    );
}

#[test]
fn a_malformed_unknown_field_is_a_decode_error() {
    let mut truncated = Vec::new();
    encode_key(77, WireType::LengthDelimited, &mut truncated);
    encode_varint(10, &mut truncated);
    truncated.extend_from_slice(b"abc");
    assert!(roi::Roi::decode(truncated.as_slice()).is_err());

    let mut mismatched = Vec::new();
    encode_key(77, WireType::StartGroup, &mut mismatched);
    encode_key(78, WireType::EndGroup, &mut mismatched);
    assert!(roi::Vec3::decode(mismatched.as_slice()).is_err());

    let mut stray = Vec::new();
    encode_key(77, WireType::EndGroup, &mut stray);
    assert!(roi::Vec3::decode(stray.as_slice()).is_err());
}
