//! Small RGB plot with a PNG encoder (uncompressed DEFLATE blocks).
const W: usize = 480;
const H: usize = 240;

fn pixel(rgb: &mut [u8], x: usize, y: usize, color: [u8; 3]) {
    if x < W && y < H {
        rgb[(y * W + x) * 3..(y * W + x + 1) * 3].copy_from_slice(&color);
    }
}
fn line(rgb: &mut [u8], a: (usize, usize), b: (usize, usize), color: [u8; 3]) {
    let n = a.0.abs_diff(b.0).max(a.1.abs_diff(b.1)).max(1);
    for i in 0..=n {
        let f = i as f64 / n as f64;
        pixel(
            rgb,
            (a.0 as f64 + (b.0 as f64 - a.0 as f64) * f).round() as usize,
            (a.1 as f64 + (b.1 as f64 - a.1 as f64) * f).round() as usize,
            color,
        );
    }
}
fn glyph(c: char) -> [u8; 7] {
    match c {
        'A' => [14, 17, 17, 31, 17, 17, 17],
        'E' => [31, 16, 16, 30, 16, 16, 31],
        'M' => [17, 27, 21, 21, 17, 17, 17],
        'N' => [17, 25, 25, 21, 19, 19, 17],
        'T' => [31, 4, 4, 4, 4, 4, 4],
        'I' => [31, 4, 4, 4, 4, 4, 31],
        'D' => [30, 17, 17, 17, 17, 17, 30],
        'X' => [17, 17, 10, 4, 10, 17, 17],
        '0' => [14, 17, 19, 21, 25, 17, 14],
        '1' => [4, 12, 4, 4, 4, 4, 14],
        '2' => [14, 17, 1, 2, 4, 8, 31],
        '3' => [30, 1, 1, 14, 1, 1, 30],
        '4' => [2, 6, 10, 18, 31, 2, 2],
        '5' => [31, 16, 16, 30, 1, 1, 30],
        '6' => [14, 16, 16, 30, 17, 17, 14],
        '7' => [31, 1, 2, 4, 8, 8, 8],
        '8' => [14, 17, 17, 14, 17, 17, 14],
        '9' => [14, 17, 17, 15, 1, 1, 14],
        '-' => [0, 0, 0, 31, 0, 0, 0],
        '.' => [0, 0, 0, 0, 0, 6, 6],
        _ => [0; 7],
    }
}
fn text(rgb: &mut [u8], x: usize, y: usize, s: &str) {
    for (i, c) in s.chars().enumerate() {
        for (dy, row) in glyph(c).into_iter().enumerate() {
            for dx in 0..5 {
                if row & (1 << (4 - dx)) != 0 {
                    pixel(rgb, x + i * 6 + dx, y + dy, [30; 3]);
                }
            }
        }
    }
}
fn chunk(out: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(kind);
    out.extend_from_slice(data);
    let mut crc = crc32fast::Hasher::new();
    crc.update(kind);
    crc.update(data);
    out.extend_from_slice(&crc.finalize().to_be_bytes());
}
pub fn render(means: &[f64]) -> Vec<u8> {
    let mut rgb = vec![250; W * H * 3];
    line(&mut rgb, (65, 30), (65, 200), [30; 3]);
    line(&mut rgb, (65, 200), (455, 200), [30; 3]);
    text(&mut rgb, 10, 12, "MEAN");
    text(&mut rgb, 210, 225, "TIME INDEX");
    let min = means
        .iter()
        .copied()
        .filter(|v| v.is_finite())
        .fold(f64::INFINITY, f64::min);
    let max = means
        .iter()
        .copied()
        .filter(|v| v.is_finite())
        .fold(f64::NEG_INFINITY, f64::max);
    let (min, max) = if min.is_finite() {
        (min, max.max(min + 1.0))
    } else {
        (0., 1.)
    };
    text(&mut rgb, 5, 30, &format!("{max:.1}"));
    text(&mut rgb, 5, 190, &format!("{min:.1}"));
    text(&mut rgb, 65, 210, "0");
    text(
        &mut rgb,
        440,
        210,
        &means.len().saturating_sub(1).to_string(),
    );
    let mut previous = None;
    for (i, &v) in means.iter().enumerate() {
        if !v.is_finite() {
            previous = None;
            continue;
        }
        let p = (
            65 + i * 390 / means.len().saturating_sub(1).max(1),
            200 - ((v - min) / (max - min) * 170.0).clamp(0., 170.) as usize,
        );
        if let Some(a) = previous {
            line(&mut rgb, a, p, [40, 100, 200]);
        }
        for dy in 0..3 {
            for dx in 0..3 {
                pixel(&mut rgb, p.0 + dx, p.1 + dy, [40, 100, 200]);
            }
        }
        previous = Some(p);
    }
    let mut raw = Vec::with_capacity(H * (W * 3 + 1));
    for row in rgb.as_chunks::<{ W * 3 }>().0 {
        raw.push(0);
        raw.extend_from_slice(row);
    }
    let mut zlib = vec![0x78, 0x01];
    let count = raw.len().div_ceil(65535);
    for (i, block) in raw.chunks(65535).enumerate() {
        zlib.push(u8::from(i + 1 == count));
        let len = block.len() as u16;
        zlib.extend_from_slice(&len.to_le_bytes());
        zlib.extend_from_slice(&(!len).to_le_bytes());
        zlib.extend_from_slice(block);
    }
    let (mut a, mut b) = (1u32, 0u32);
    for &v in &raw {
        a = (a + u32::from(v)) % 65521;
        b = (b + a) % 65521;
    }
    zlib.extend_from_slice(&((b << 16) | a).to_be_bytes());
    let mut out = b"\x89PNG\r\n\x1a\n".to_vec();
    let mut ihdr = Vec::new();
    ihdr.extend_from_slice(&(W as u32).to_be_bytes());
    ihdr.extend_from_slice(&(H as u32).to_be_bytes());
    ihdr.extend_from_slice(&[8, 2, 0, 0, 0]);
    chunk(&mut out, b"IHDR", &ihdr);
    chunk(&mut out, b"IDAT", &zlib);
    chunk(&mut out, b"IEND", &[]);
    out
}
