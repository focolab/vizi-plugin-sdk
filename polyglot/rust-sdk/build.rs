fn main() -> Result<(), Box<dyn std::error::Error>> {
    let proto = "proto";
    println!("cargo:rerun-if-changed={proto}/vizi_plugin.proto");
    println!("cargo:rerun-if-changed={proto}/roi.proto");
    std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    tonic_build::configure()
        .file_descriptor_set_path(
            std::path::PathBuf::from(std::env::var("OUT_DIR")?).join("vizi.bin"),
        )
        .compile_protos(&[format!("{proto}/vizi_plugin.proto")], &[proto])?;
    // IDL prose is not Rust example code. Keep the canonical IDL untouched,
    // but render its indented equations and angle-bracket placeholders safely.
    let bindings = std::path::PathBuf::from(std::env::var("OUT_DIR")?).join("vizi.plugin.v1.rs");
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
