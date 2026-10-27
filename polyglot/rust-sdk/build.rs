#[path = "codegen/regions.rs"]
mod regions;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let proto = "proto";
    println!("cargo:rerun-if-changed={proto}/vizi_plugin.proto");
    println!("cargo:rerun-if-changed={proto}/roi.proto");
    println!("cargo:rerun-if-changed=codegen/regions.rs");
    std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    let out = std::path::PathBuf::from(std::env::var("OUT_DIR")?);
    // The region messages are generated from their descriptors with a member
    // that keeps unknown field records (codegen/regions.rs); prost generates the
    // rest, and refers to the region messages through `extern_path`.
    let descriptors =
        tonic_build::Config::new().load_fds(&[format!("{proto}/vizi_plugin.proto")], &[proto])?;
    std::fs::write(
        out.join("pois_math.v1.messages.rs"),
        regions::generate(&descriptors),
    )?;
    let mut builder = tonic_build::configure();
    for message in regions::messages(&descriptors) {
        let rust = format!(
            "crate::proto::pois_math::v1::{}",
            message.rsplit('.').next().unwrap()
        );
        builder = builder.extern_path(message, rust);
    }
    builder
        // A later protocol minor may add enum values and message kinds (oneof
        // arms): code outside this crate must keep a wildcard arm for them.
        .enum_attribute(".", "#[non_exhaustive]")
        // Prost with a digest of each received message's wire bytes, so a
        // re-send under a used id can be told from different content.
        .codec_path("crate::control::codec::DigestCodec")
        .file_descriptor_set_path(out.join("vizi.bin"))
        .compile_protos(&[format!("{proto}/vizi_plugin.proto")], &[proto])?;
    // IDL prose is not Rust example code. Keep the canonical IDL untouched,
    // but render its indented equations and angle-bracket placeholders safely.
    let bindings = out.join("vizi.plugin.v1.rs");
    let source = std::fs::read_to_string(&bindings)?;
    let source = source
        .lines()
        .map(|line| {
            let line = if line.starts_with("///      ") {
                format!("/// {}", line[3..].trim_start())
            } else {
                line.to_owned()
            };
            if line.trim_start().starts_with("///") {
                line.replace("q<n>", "q&lt;n&gt;")
                    .replace("Label <id>", "Label &lt;id&gt;")
                    .replace("<plugin>", "&lt;plugin&gt;")
            } else {
                line
            }
        })
        .collect::<Vec<_>>()
        .join("\n");
    std::fs::write(bindings, source + "\n")?;
    Ok(())
}
