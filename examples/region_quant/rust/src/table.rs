//! Exact arithmetic and file/image encoding, independent of the transport.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Measure {
    pub count: u64,
    pub sum: u64,
}
impl Measure {
    pub fn mean(self) -> String {
        if self.count == 0 {
            return String::new();
        }
        let rounded =
            (2000 * u128::from(self.sum) + u128::from(self.count)) / (2 * u128::from(self.count));
        format!("{}.{:03}", rounded / 1000, rounded % 1000)
    }
    pub fn swatch_index(self) -> usize {
        (self.sum.checked_div(self.count).unwrap_or(0) >> 8).min(255) as usize
    }
    pub fn brighter_than(self, other: Self) -> bool {
        self.count != 0
            && (other.count == 0
                || u128::from(self.sum) * u128::from(other.count)
                    > u128::from(other.sum) * u128::from(self.count))
    }
}
#[derive(Clone, Debug)]
pub struct Row {
    pub id: String,
    pub name: String,
    pub measure: Measure,
}
impl Row {
    pub fn display_name(&self) -> &str {
        if self.name.is_empty() {
            &self.id
        } else {
            &self.name
        }
    }
}
pub fn ordered<'a>(rows: impl Iterator<Item = &'a Row>) -> Vec<&'a Row> {
    let mut rows: Vec<_> = rows.collect();
    // UTF-8 byte lexicographic order agrees with Unicode scalar order.
    rows.sort_by(|a, b| (a.display_name(), &a.id).cmp(&(b.display_name(), &b.id)));
    rows
}
pub fn best_frame(frames: impl IntoIterator<Item = (u32, Measure)>) -> Option<u32> {
    let mut best: Option<(u32, Measure)> = None;
    for (t, m) in frames {
        if m.count > 0
            && best.is_none_or(|(old_t, old)| {
                m.brighter_than(old) || (!old.brighter_than(m) && t < old_t)
            })
        {
            best = Some((t, m));
        }
    }
    best.map(|(t, _)| t)
}
fn csv_field(s: &str) -> String {
    if s.contains([',', '"', '\r', '\n']) {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_owned()
    }
}
pub fn csv(rows: &[&Row], t: u32, c: u32, integer: bool) -> String {
    let mut out = String::from("region_id,name,t,channel,voxels,mean\n");
    for row in rows {
        out.push_str(&format!(
            "{},{},{t},{c},{},{}\n",
            csv_field(&row.id),
            csv_field(&row.name),
            if integer {
                row.measure.count.to_string()
            } else {
                String::new()
            },
            if integer {
                row.measure.mean()
            } else {
                String::new()
            }
        ));
    }
    out
}
/// An 8x8 RGB PNG using one stored DEFLATE block: filter 0, identical pixels.
pub fn swatch(rgb: [u8; 3]) -> Vec<u8> {
    let mut raw = [0_u8; 200];
    for row in raw.as_chunks_mut::<25>().0 {
        for pixel in row[1..].as_chunks_mut::<3>().0 {
            pixel.copy_from_slice(&rgb);
        }
    }
    let mut zlib = vec![0x78, 0x01, 0x01];
    let len = raw.len() as u16;
    zlib.extend_from_slice(&len.to_le_bytes());
    zlib.extend_from_slice(&(!len).to_le_bytes());
    zlib.extend_from_slice(&raw);
    let (mut a, mut b) = (1_u32, 0_u32);
    for byte in raw {
        a = (a + u32::from(byte)) % 65521;
        b = (b + a) % 65521;
    }
    zlib.extend_from_slice(&((b << 16) | a).to_be_bytes());
    let mut png = b"\x89PNG\r\n\x1a\n".to_vec();
    fn chunk(png: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
        png.extend_from_slice(&(data.len() as u32).to_be_bytes());
        let start = png.len();
        png.extend_from_slice(kind);
        png.extend_from_slice(data);
        let crc = crc32fast::hash(&png[start..]);
        png.extend_from_slice(&crc.to_be_bytes());
    }
    let mut header = Vec::new();
    header.extend_from_slice(&8_u32.to_be_bytes());
    header.extend_from_slice(&8_u32.to_be_bytes());
    header.extend_from_slice(&[8, 2, 0, 0, 0]);
    chunk(&mut png, b"IHDR", &header);
    chunk(&mut png, b"IDAT", &zlib);
    chunk(&mut png, b"IEND", &[]);
    png
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_mean_and_swatch_edges() {
        assert_eq!(Measure { sum: 17, count: 16 }.mean(), "1.063");
        assert_eq!(Measure::default().mean(), "");
        assert_eq!(
            Measure {
                sum: u64::MAX,
                count: 1
            }
            .mean(),
            "18446744073709551615.000"
        );
        assert_eq!(Measure::default().swatch_index(), 0);
        assert_eq!(
            Measure {
                sum: 65536,
                count: 1
            }
            .swatch_index(),
            255
        );
        assert_eq!(Measure { sum: 511, count: 2 }.swatch_index(), 0);
    }
    #[test]
    fn names_ids_and_unicode_order() {
        let rows = [
            ("z", "same"),
            ("a", "same"),
            ("b", ""),
            ("u", "🦀"),
            ("v", "é"),
        ]
        .map(|(id, name)| Row {
            id: id.into(),
            name: name.into(),
            measure: Measure::default(),
        });
        let sorted = ordered(rows.iter());
        assert_eq!(
            sorted.iter().map(|r| r.id.as_str()).collect::<Vec<_>>(),
            ["b", "a", "z", "v", "u"]
        );
    }
    #[test]
    fn csv_quotes_original_names_and_empty_means() {
        let rows = [
            Row {
                id: "x".into(),
                name: "a,\"b\"".into(),
                measure: Measure { sum: 17, count: 16 },
            },
            Row {
                id: "y".into(),
                name: String::new(),
                measure: Measure::default(),
            },
        ];
        assert_eq!(
            csv(&rows.iter().collect::<Vec<_>>(), 2, 1, true),
            "region_id,name,t,channel,voxels,mean\nx,\"a,\"\"b\"\"\",2,1,16,1.063\ny,,2,1,0,\n"
        );
        assert_eq!(csv_field(" leading space"), " leading space");
        assert_eq!(
            csv(&[&rows[0]], 2, 1, false),
            "region_id,name,t,channel,voxels,mean\nx,\"a,\"\"b\"\"\",2,1,,\n"
        );
        assert_eq!(csv_field("a\nb"), "\"a\nb\"");
    }
    #[test]
    fn best_skips_empty_and_keeps_earliest_tie_without_overflow() {
        let frames = [
            Measure::default(),
            Measure { sum: 4, count: 2 },
            Measure { sum: 6, count: 3 },
            Measure::default(),
        ];
        assert_eq!(
            best_frame(frames.into_iter().enumerate().map(|(t, m)| (t as u32, m))),
            Some(1)
        );
        assert_eq!(
            best_frame([
                (5, Measure { sum: 4, count: 2 }),
                (2, Measure { sum: 6, count: 3 })
            ]),
            Some(2)
        );
        assert_eq!(best_frame([(0, Measure::default())]), None);
        assert!(Measure {
            sum: u64::MAX,
            count: u64::MAX - 1
        }
        .brighter_than(Measure {
            sum: u64::MAX - 1,
            count: u64::MAX
        }));
    }
}
