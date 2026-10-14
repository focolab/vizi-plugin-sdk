//! The container layout in SPEC.md. No transport, mapping, or protobuf serialization.
use std::{
    collections::BTreeMap,
    fs::File,
    io::{Read, Seek, SeekFrom, Write},
    path::Path,
};
use vizi::{
    control::{Error, Result},
    proto::error::Kind,
};

pub const DERIVED: u32 = 2;

pub fn error(kind: Kind, message: impl Into<String>) -> Error {
    Error::new(kind, message)
}
fn io(e: std::io::Error) -> Error {
    error(Kind::IoError, e.to_string())
}
fn malformed(s: &str) -> Error {
    error(Kind::IoError, s)
}
pub fn clean(v: f64) -> f64 {
    if v.is_finite() && v > 0.0 {
        v
    } else {
        0.0
    }
}
fn u16le(b: &[u8], p: usize) -> u16 {
    u16::from_le_bytes(b[p..p + 2].try_into().unwrap())
}
fn u32le(b: &[u8], p: usize) -> u32 {
    u32::from_le_bytes(b[p..p + 4].try_into().unwrap())
}
fn u64le(b: &[u8], p: usize) -> u64 {
    u64::from_le_bytes(b[p..p + 8].try_into().unwrap())
}
fn read_at(r: &mut (impl Read + Seek), offset: u64, len: u64) -> Result<Vec<u8>> {
    let len = usize::try_from(len).map_err(|_| malformed("section exceeds address space"))?;
    let mut bytes = Vec::new();
    bytes
        .try_reserve_exact(len)
        .map_err(|_| malformed("cannot allocate crate section"))?;
    bytes.resize(len, 0);
    r.seek(SeekFrom::Start(offset)).map_err(io)?;
    r.read_exact(&mut bytes).map_err(io)?;
    Ok(bytes)
}

#[derive(Clone, Debug, PartialEq)]
pub struct Header {
    pub dtype: u16,
    pub flags: u32,
    pub shape: [u32; 5],
    pub calibration: [f64; 4], // ZYX in micrometres; time in seconds
    pub malformed_spacing: bool,
}
impl Header {
    pub fn itemsize(&self) -> Result<usize> {
        match self.dtype {
            1 => Ok(1),
            2 => Ok(2),
            3 => Ok(4),
            n => Err(error(
                Kind::Unsupported,
                format!("crate dtype {n} is unsupported"),
            )),
        }
    }
    pub fn block_bytes(&self) -> Result<u64> {
        self.shape[2..]
            .iter()
            .try_fold(self.itemsize()? as u64, |n, &d| {
                n.checked_mul(d as u64)
                    .ok_or_else(|| malformed("volume size overflow"))
            })
    }
    pub fn blocks(&self) -> u64 {
        self.shape[0] as u64 * self.shape[1] as u64
    }
    pub fn validate(&self) -> Result<()> {
        self.itemsize()?;
        for (axis, n) in ["t", "c", "z", "y", "x"].iter().zip(self.shape) {
            if n == 0 {
                return Err(error(
                    Kind::InvalidArgument,
                    format!("crate axis {axis} has zero length"),
                ));
            }
        }
        self.block_bytes()?;
        Ok(())
    }
    pub fn parse(b: &[u8]) -> Result<(Self, u16, u32)> {
        if b.len() != 96 || &b[..8] != b"VIZCRATE" || u16le(b, 8) != 1 {
            return Err(error(
                Kind::InvalidArgument,
                "not a crate: header magic, size or version is invalid",
            ));
        }
        let mut raw = [0.; 4];
        for (i, v) in raw.iter_mut().enumerate() {
            *v = f64::from_le_bytes(b[48 + i * 8..56 + i * 8].try_into().unwrap());
        }
        let h = Self {
            dtype: u16le(b, 10),
            flags: u32le(b, 12),
            shape: std::array::from_fn(|i| u32le(b, 16 + i * 4)),
            calibration: raw.map(clean),
            malformed_spacing: raw.iter().any(|v| !v.is_finite() || *v < 0.),
        };
        h.validate()?;
        Ok((h, u16le(b, 36), u32le(b, 38)))
    }
    fn pack(&self, form: &str, text: &str) -> Result<[u8; 96]> {
        self.validate()?;
        let fl =
            u16::try_from(form.len()).map_err(|_| malformed("metadata form exceeds u16 length"))?;
        let tl =
            u32::try_from(text.len()).map_err(|_| malformed("metadata text exceeds u32 length"))?;
        let mut b = [0; 96];
        b[..8].copy_from_slice(b"VIZCRATE");
        b[8..10].copy_from_slice(&1u16.to_le_bytes());
        b[10..12].copy_from_slice(&self.dtype.to_le_bytes());
        b[12..16].copy_from_slice(&self.flags.to_le_bytes());
        for (i, d) in self.shape.iter().enumerate() {
            b[16 + i * 4..20 + i * 4].copy_from_slice(&d.to_le_bytes());
        }
        b[36..38].copy_from_slice(&fl.to_le_bytes());
        b[38..42].copy_from_slice(&tl.to_le_bytes());
        for (i, v) in self.calibration.iter().enumerate() {
            b[48 + i * 8..56 + i * 8].copy_from_slice(&clean(*v).to_le_bytes());
        }
        Ok(b)
    }
    pub fn label(&self) -> String {
        spacing_label(self.calibration)
    }
}
pub fn spacing_label(values: [f64; 4]) -> String {
    if values.iter().all(|&v| v == 0.) {
        return "(unknown)".into();
    }
    let spatial = values[..3]
        .iter()
        .map(|v| {
            if *v == 0. {
                "?".into()
            } else {
                format!("{v:.3}")
            }
        })
        .collect::<Vec<_>>()
        .join(" × ");
    let dt = if values[3] == 0. {
        "?".into()
    } else {
        values[3].to_string()
    };
    format!("{spatial} µm (z × y × x), Δt {dt} s")
}

#[derive(Clone, Debug, Default)]
pub struct Calibration(pub BTreeMap<u32, i32>);
impl Calibration {
    pub fn load(path: &Path) -> Result<Self> {
        Self::parse(&std::fs::read(path).map_err(io)?)
            .map_err(|e| error(e.kind, format!("{}: {}", path.display(), e.message)))
    }
    pub fn parse(b: &[u8]) -> Result<Self> {
        if b.len() < 16 || &b[..8] != b"VIZCAL01" {
            return Err(malformed("invalid calibration table header"));
        }
        if b.len() as u64 != 16 + u32le(b, 8) as u64 * 8 {
            return Err(malformed("calibration entry count disagrees with length"));
        }
        Ok(Self(
            b[16..]
                .as_chunks::<8>()
                .0
                .iter()
                .map(|p| (u32le(p, 0), i32::from_le_bytes(p[4..].try_into().unwrap())))
                .collect(),
        ))
    }
    pub fn apply(&self, dtype: u16, channel: u32, b: &mut [u8]) {
        let offset = *self.0.get(&channel).unwrap_or(&0);
        if offset == 0 {
            return;
        }
        match dtype {
            1 => {
                for v in b {
                    *v = (*v as i64 - offset as i64).clamp(0, u8::MAX as i64) as u8;
                }
            }
            2 => {
                for v in b.as_chunks_mut::<2>().0 {
                    let n = (u16le(v, 0) as i64 - offset as i64).clamp(0, u16::MAX as i64) as u16;
                    v.copy_from_slice(&n.to_le_bytes());
                }
            }
            3 => {
                for v in b.as_chunks_mut::<4>().0 {
                    let n = f32::from_le_bytes(*v) - offset as f32;
                    v.copy_from_slice(&n.to_le_bytes());
                }
            }
            _ => unreachable!("validated dtype"),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Entry {
    pub offset: u64,
    pub len: u64,
    pub crc: u32,
}
impl Entry {
    fn pack(self) -> [u8; 24] {
        let mut b = [0; 24];
        b[..8].copy_from_slice(&self.offset.to_le_bytes());
        b[8..16].copy_from_slice(&self.len.to_le_bytes());
        b[16..20].copy_from_slice(&self.crc.to_le_bytes());
        b
    }
}
pub struct Crate<R> {
    input: R,
    pub name: String,
    pub header: Header,
    pub form: String,
    pub text: String,
    pub invalid_utf8: bool,
    pub provenance: Vec<u8>,
    pub names: Vec<String>,
    pub malformed_names: bool,
    pub index: Vec<Entry>,
}
impl Crate<File> {
    pub fn open(path: &Path) -> Result<Self> {
        let input = File::open(path)
            .map_err(|e| error(Kind::NotFound, format!("{}: {e}", path.display())))?;
        Self::read(input, &path.display().to_string())
    }
}
impl<R: Read + Seek> Crate<R> {
    pub fn read(mut input: R, name: &str) -> Result<Self> {
        let parsed = (|| {
            let size = input.seek(SeekFrom::End(0)).map_err(io)?;
            if size < 96 {
                return Err(error(
                    Kind::InvalidArgument,
                    "not a crate: shorter than header",
                ));
            }
            let raw_header = read_at(&mut input, 0, 96)?;
            let (header, fl, tl) = Header::parse(&raw_header)?;
            let pl = u32le(&raw_header, 80) as u64;
            let nl = u32le(&raw_header, 84) as u64;
            if size < 128 {
                return Err(malformed(
                    "unfinished crate — an export of this file did not complete",
                ));
            }
            let footer = read_at(&mut input, size - 32, 32)?;
            if &footer[24..] != b"ETARCZIV" {
                return Err(malformed(
                    "unfinished crate — an export of this file did not complete",
                ));
            }
            let start = 96 + fl as u64 + tl as u64 + pl + nl;
            let index_offset = u64le(&footer, 0);
            let count = u64le(&footer, 8);
            if count != header.blocks()
                || index_offset < start
                || count
                    .checked_mul(24)
                    .and_then(|n| index_offset.checked_add(n))
                    != Some(size - 32)
            {
                return Err(malformed("index geometry disagrees with header or footer"));
            }
            let raw = read_at(&mut input, index_offset, count * 24)?;
            if crc32fast::hash(&raw) != u32le(&footer, 16) {
                return Err(malformed("index CRC-32 mismatch"));
            }
            let mut index = Vec::new();
            let block = header.block_bytes()?;
            for p in raw.as_chunks::<24>().0 {
                let e = Entry {
                    offset: u64le(p, 0),
                    len: u64le(p, 8),
                    crc: u32le(p, 16),
                };
                if e.len != block
                    || e.offset < start
                    || e.offset
                        .checked_add(e.len)
                        .is_none_or(|end| end > index_offset)
                {
                    return Err(malformed(
                        "index entry length or offset outside block region",
                    ));
                }
                index.push(e);
            }
            let form = String::from_utf8(read_at(&mut input, 96, fl as u64)?);
            let text = String::from_utf8(read_at(&mut input, 96 + fl as u64, tl as u64)?);
            let invalid_utf8 = form.is_err() || text.is_err();
            let (form, text) = if invalid_utf8 {
                (String::new(), String::new())
            } else {
                (form.unwrap(), text.unwrap())
            };
            let provenance = read_at(&mut input, 96 + fl as u64 + tl as u64, pl)?;
            let raw_names = read_at(&mut input, start - nl, nl)?;
            let (names, malformed_names) = match String::from_utf8(raw_names) {
                Ok(names) if names.is_empty() => (vec![], false),
                Ok(names) if names.split('\0').count() as u64 <= u64::from(header.shape[1]) => {
                    (names.split('\0').map(str::to_owned).collect(), false)
                }
                _ => (vec![], true),
            };
            Ok((
                header,
                index,
                form,
                text,
                invalid_utf8,
                provenance,
                names,
                malformed_names,
            ))
        })()
        .map_err(|e: Error| error(e.kind, format!("{name}: {}", e.message)))?;
        Ok(Self {
            input,
            name: name.into(),
            header: parsed.0,
            index: parsed.1,
            form: parsed.2,
            text: parsed.3,
            invalid_utf8: parsed.4,
            provenance: parsed.5,
            names: parsed.6,
            malformed_names: parsed.7,
        })
    }
    fn entry(&self, t: u32, c: u32) -> Result<Entry> {
        if t >= self.header.shape[0] || c >= self.header.shape[1] {
            return Err(error(
                Kind::OutOfBounds,
                format!(
                    "{}: (t={t}, c={c}) outside shape {:?}",
                    self.name, self.header.shape
                ),
            ));
        }
        Ok(self.index[(t as u64 * self.header.shape[1] as u64 + c as u64) as usize])
    }
    pub fn raw(&mut self, t: u32, c: u32) -> Result<(Vec<u8>, bool)> {
        let e = self.entry(t, c)?;
        let b = read_at(&mut self.input, e.offset, e.len).map_err(|e| {
            error(
                e.kind,
                format!("{}: (t={t}, c={c}): {}", self.name, e.message),
            )
        })?;
        let valid = crc32fast::hash(&b) == e.crc;
        Ok((b, valid))
    }
    pub fn require_calibration(&self, cal: Option<&Calibration>) -> Result<()> {
        if self.header.flags & 1 != 0 && cal.is_none() {
            return Err(malformed(&format!("{}: calibrated crate needs a calibration table — import vizi-crate-cal.bin in Plugin Manager", self.name)));
        }
        Ok(())
    }
    fn decode(&self, c: u32, b: &mut [u8], cal: Option<&Calibration>) -> Result<()> {
        self.require_calibration(cal)?;
        if self.header.flags & 1 != 0 {
            cal.unwrap().apply(self.header.dtype, c, b);
        }
        Ok(())
    }
    pub fn volume(
        &mut self,
        t: u32,
        c: u32,
        verify: bool,
        cal: Option<&Calibration>,
    ) -> Result<Vec<u8>> {
        let (mut b, valid) = self.raw(t, c)?;
        if verify && !valid {
            return Err(malformed(&format!(
                "{}: CRC-32 mismatch at (t={t}, c={c})",
                self.name
            )));
        }
        self.decode(c, &mut b, cal)?;
        Ok(b)
    }
    pub fn plane(&mut self, t: u32, c: u32, z: u32, cal: Option<&Calibration>) -> Result<Vec<u8>> {
        let e = self.entry(t, c)?;
        if z >= self.header.shape[2] {
            return Err(error(
                Kind::OutOfBounds,
                format!("{}: (t={t}, c={c}, z={z}) outside z axis", self.name),
            ));
        }
        let len = e.len / self.header.shape[2] as u64;
        let mut b = read_at(&mut self.input, e.offset + z as u64 * len, len).map_err(|e| {
            error(
                e.kind,
                format!("{}: (t={t}, c={c}, z={z}): {}", self.name, e.message),
            )
        })?;
        self.decode(c, &mut b, cal)?;
        Ok(b)
    }
    pub fn check(&mut self) -> Result<Vec<(u32, u32)>> {
        let mut failed = Vec::new();
        for t in 0..self.header.shape[0] {
            for c in 0..self.header.shape[1] {
                if !self.raw(t, c)?.1 {
                    failed.push((t, c));
                }
            }
        }
        Ok(failed)
    }
}

pub struct Writer<W> {
    output: W,
    pub header: Header,
    index: Vec<Option<Entry>>,
}
impl<W: Write + Seek> Writer<W> {
    pub fn begin(output: W, header: Header, form: &str, text: &str) -> Result<Self> {
        Self::begin_sections(output, header, form, text, &[], &[])
    }
    pub fn begin_sections(
        mut output: W,
        mut header: Header,
        form: &str,
        text: &str,
        provenance: &[u8],
        names: &[String],
    ) -> Result<Self> {
        if names.len() as u64 > u64::from(header.shape[1]) {
            return Err(malformed("more channel names than channels"));
        }
        let mut names: Vec<_> = names
            .iter()
            .map(|name| {
                if name.contains('\0') {
                    ""
                } else {
                    name.as_str()
                }
            })
            .collect();
        while names.last() == Some(&"") {
            names.pop();
        }
        let names = names.join("\0");
        header.flags &= DERIVED;
        header.calibration = header.calibration.map(clean);
        let mut packed = header.pack(form, text)?;
        let pl = u32::try_from(provenance.len())
            .map_err(|_| malformed("provenance exceeds u32 length"))?;
        let nl = u32::try_from(names.len()).map_err(|_| malformed("names exceed u32 length"))?;
        packed[80..84].copy_from_slice(&pl.to_le_bytes());
        packed[84..88].copy_from_slice(&nl.to_le_bytes());
        output.seek(SeekFrom::Start(0)).map_err(io)?;
        output.write_all(&packed).map_err(io)?;
        output.write_all(form.as_bytes()).map_err(io)?;
        output.write_all(text.as_bytes()).map_err(io)?;
        output.write_all(provenance).map_err(io)?;
        output.write_all(names.as_bytes()).map_err(io)?;
        let n = usize::try_from(header.blocks())
            .map_err(|_| malformed("index exceeds address space"))?;
        let mut index = Vec::new();
        index
            .try_reserve_exact(n)
            .map_err(|_| malformed("cannot allocate index"))?;
        index.resize(n, None);
        Ok(Self {
            output,
            header,
            index,
        })
    }
    pub fn block(&mut self, t: u32, c: u32, b: &[u8], little_endian: bool) -> Result<()> {
        if t >= self.header.shape[0] || c >= self.header.shape[1] {
            return Err(error(
                Kind::OutOfBounds,
                format!("artifact (t={t}, c={c}) outside shape"),
            ));
        }
        if b.len() as u64 != self.header.block_bytes()? {
            return Err(malformed("block length disagrees with geometry"));
        }
        let size = self.header.itemsize()?;
        let swapped;
        let bytes = if !little_endian && size > 1 {
            swapped = b
                .chunks_exact(size)
                .flat_map(|v| v.iter().rev().copied())
                .collect::<Vec<_>>();
            &swapped[..]
        } else {
            b
        };
        let offset = self.output.stream_position().map_err(io)?;
        self.output.write_all(bytes).map_err(io)?;
        self.index[(t as u64 * self.header.shape[1] as u64 + c as u64) as usize] = Some(Entry {
            offset,
            len: bytes.len() as u64,
            crc: crc32fast::hash(bytes),
        });
        Ok(())
    }
    pub fn finish(mut self) -> Result<W> {
        let missing = self.index.iter().filter(|e| e.is_none()).count();
        if missing != 0 {
            let i = self.index.iter().position(|e| e.is_none()).unwrap();
            return Err(malformed(&format!(
                "{missing} of {} blocks missing; first: (t={}, c={})",
                self.index.len(),
                i / self.header.shape[1] as usize,
                i % self.header.shape[1] as usize
            )));
        }
        let offset = self.output.stream_position().map_err(io)?;
        let raw: Vec<u8> = self
            .index
            .into_iter()
            .flat_map(|e| e.unwrap().pack())
            .collect();
        self.output.write_all(&raw).map_err(io)?;
        self.output.write_all(&offset.to_le_bytes()).map_err(io)?;
        self.output
            .write_all(&self.header.blocks().to_le_bytes())
            .map_err(io)?;
        self.output
            .write_all(&crc32fast::hash(&raw).to_le_bytes())
            .map_err(io)?;
        self.output.write_all(&[0; 4]).map_err(io)?;
        self.output.write_all(b"ETARCZIV").map_err(io)?;
        self.output.flush().map_err(io)?;
        Ok(self.output)
    }
}

pub fn values(dtype: u16, bytes: &[u8]) -> Result<Vec<f64>> {
    Ok(match dtype {
        1 => bytes.iter().map(|v| *v as f64).collect(),
        2 => bytes
            .as_chunks::<2>()
            .0
            .iter()
            .map(|v| u16le(v, 0) as f64)
            .collect(),
        3 => bytes
            .as_chunks::<4>()
            .0
            .iter()
            .map(|v| f32::from_le_bytes(*v) as f64)
            .collect(),
        _ => {
            return Err(error(
                Kind::Unsupported,
                format!("dtype {dtype} is unsupported"),
            ))
        }
    })
}
