use super::*;

fn channels() -> Vec<vizi_doorbell::ChannelName> {
    [
        (1, "file", true),
        (2, "placeholder", false),
        (3, "user", true),
        (0, "unspecified", false),
        (97, "97", false),
    ]
    .into_iter()
    .enumerate()
    .map(|(index, (source, label, declared))| {
        let ch = pb::ChannelInfo {
            index: index as u32 + 4,
            name: format!("Channel {index}"),
            source,
        };
        assert_eq!(ch.source_name(), label);
        assert_eq!(ch.declared(), declared);
        ch
    })
    .collect()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn view_channels_preserve_source_and_series_presence() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    for series in [None, Some(String::new()), Some("opaque".into())] {
        let ctx = s.ctx();
        let task = tokio::task::spawn_blocking(move || ctx.view_state());
        let request = s.recv().await;
        s.send(
            &request.id,
            Host::ViewState(pb::ViewState {
                dataset_series: series.clone(),
                channels: channels(),
                ..Default::default()
            }),
        )
        .await;
        let view = task.await.unwrap().unwrap();
        assert_eq!(view.dataset_series, series);
        assert_eq!(view.channels, channels());
    }
    s.stop().await;
}

#[test]
fn geometry_channels_keep_unknown_source_on_wire_roundtrip() {
    use prost::Message;
    let geometry = pb::Geometry {
        channels: channels(),
        ..Default::default()
    };
    let decoded = pb::Geometry::decode(geometry.encode_to_vec().as_slice()).unwrap();
    assert_eq!(decoded.channels, channels());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn exporter_gets_source_document_channels_and_unparsed_provenance() {
    let (tx, rx) = std::sync::mpsc::channel();
    let mut s = Session::new(Registry::new().exporter("raw", move |_, req| {
        tx.send(req.clone()).unwrap();
        Ok(Arc::new(TestExporter(Arc::new(AtomicUsize::new(0)))))
    }))
    .await;
    s.recv().await;
    for record in [
        "",
        "{}",
        "{\"source\":\"image\"}",
        "broken JSON",
        "[]",
        "null",
    ] {
        s.send(
            "export",
            Host::OpenExport(pb::OpenExport {
                exporter_id: "raw".into(),
                shape_tczyx: vec![1, 5, 1, 1, 1],
                dtype: pb::DType::DtypeU8 as i32,
                source_metadata: Some(pb::MetadataDoc {
                    form: "ome-xml".into(),
                    text: "source document".into(),
                }),
                channels: channels(),
                provenance: record.into(),
                ..Default::default()
            }),
        )
        .await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::ExportOpened(_))));
        let req = rx.recv().unwrap();
        assert_eq!(req.source_metadata.unwrap().text, "source document");
        assert_eq!(req.channels, channels());
        assert_eq!(req.provenance, record);
    }
    s.stop().await;
}
