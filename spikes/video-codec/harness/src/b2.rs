//! FAITHFUL REPLICATION of shinkend's dirty-tile delta screencast encoder (B2).
//!
//! `shinkend` is a binary-only crate (no `lib.rs`), so this spike cannot path-depend on
//! it. Instead the load-bearing functions below are **verbatim copies** from
//! `shinkend/src/executor.rs` @ 23eed91 (`TILE_SIZE`, `KEYFRAME_INTERVAL`, `diff_tiles`,
//! `tile_rgb`, `encode_png_fast`, `encode_png`, `encode_jpeg`, `DeltaState::tick`
//! semantics), built against the SAME crate versions (`png = "0.17"`,
//! `jpeg-encoder = "0.6"`) so payload bytes are byte-identical to what the runtime
//! produces. The only deltas: base64/protocol wrapping is dropped (the spike counts raw
//! payload bytes; the +1/3 base64-in-JSON wire tax applies equally to every contender)
//! and `CapturedImage` is reduced to `Vec<u8>`.

use anyhow::Result;

/// Tile edge (px) — verbatim `executor::TILE_SIZE`.
pub const TILE_SIZE: u16 = 64;

/// Every Nth DELIVERED delta frame is a full keyframe — verbatim `executor::KEYFRAME_INTERVAL`.
pub const KEYFRAME_INTERVAL: u64 = 30;

/// Runtime default JPEG quality — verbatim `executor::DEFAULT_JPEG_QUALITY`.
pub const DEFAULT_JPEG_QUALITY: u8 = 80;

pub type TileRect = (u32, u32, u16, u16);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Png,
    Jpeg,
}

/// Verbatim `executor::diff_tiles`: per-row slice equality inside each 64px tile
/// (compiles to memcmp), early exit on the first differing row.
pub fn diff_tiles(prev: &[u8], curr: &[u8], w: u16, h: u16) -> Vec<TileRect> {
    debug_assert_eq!(prev.len(), curr.len());
    let (wu, hu, ts) = (w as usize, h as usize, TILE_SIZE as usize);
    let mut out = Vec::new();
    let mut ty = 0usize;
    while ty < hu {
        let th = ts.min(hu - ty);
        let mut tx = 0usize;
        while tx < wu {
            let tw = ts.min(wu - tx);
            let changed = (ty..ty + th).any(|row| {
                let start = (row * wu + tx) * 3;
                prev[start..start + tw * 3] != curr[start..start + tw * 3]
            });
            if changed {
                out.push((tx as u32, ty as u32, tw as u16, th as u16));
            }
            tx += ts;
        }
        ty += ts;
    }
    out
}

/// Verbatim `executor::encode_png_fast`: tile payloads — `Compression::Fast` + adaptive
/// filtering.
pub fn encode_png_fast(rgb: &[u8], w: u32, h: u32) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    {
        let mut enc = png::Encoder::new(&mut out, w, h);
        enc.set_color(png::ColorType::Rgb);
        enc.set_depth(png::BitDepth::Eight);
        enc.set_compression(png::Compression::Fast);
        enc.set_adaptive_filter(png::AdaptiveFilterType::Adaptive);
        let mut writer = enc.write_header()?;
        writer.write_image_data(rgb)?;
        writer.finish()?;
    }
    Ok(out)
}

/// Verbatim `executor::encode_png`: keyframes — the default compression preset.
pub fn encode_png(rgb: &[u8], w: u32, h: u32) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    {
        let mut enc = png::Encoder::new(&mut out, w, h);
        enc.set_color(png::ColorType::Rgb);
        enc.set_depth(png::BitDepth::Eight);
        let mut writer = enc.write_header()?;
        writer.write_image_data(rgb)?;
        writer.finish()?;
    }
    Ok(out)
}

/// Verbatim `executor::encode_jpeg`: pure-Rust baseline JPEG.
pub fn encode_jpeg(rgb: &[u8], w: u16, h: u16, quality: u8) -> Result<Vec<u8>> {
    let q = quality.clamp(1, 100);
    let mut out = Vec::new();
    let enc = jpeg_encoder::Encoder::new(&mut out, q);
    enc.encode(rgb, w, h, jpeg_encoder::ColorType::Rgb)
        .map_err(|e| anyhow::anyhow!("jpeg encode failed: {e}"))?;
    Ok(out)
}

/// Verbatim `executor::tile_rgb`: copy one tile's pixels into a contiguous buffer.
pub fn tile_rgb(rgb: &[u8], frame_w: u16, rect: TileRect) -> Vec<u8> {
    let (x, y, tw, th) = (
        rect.0 as usize,
        rect.1 as usize,
        rect.2 as usize,
        rect.3 as usize,
    );
    let fw = frame_w as usize;
    let mut out = Vec::with_capacity(tw * th * 3);
    for row in y..y + th {
        let start = (row * fw + x) * 3;
        out.extend_from_slice(&rgb[start..start + tw * 3]);
    }
    out
}

pub struct EncodedTile {
    pub rect: TileRect,
    pub data: Vec<u8>,
}

/// Mirrors `executor::encode_tiles` (PNG tiles use the fast preset; JPEG tiles use the
/// runtime's encoder at the same quality).
pub fn encode_tiles(
    rgb: &[u8],
    w: u16,
    rects: &[TileRect],
    format: Format,
    quality: u8,
) -> Result<Vec<EncodedTile>> {
    rects
        .iter()
        .map(|&rect| {
            let (tw, th) = (rect.2, rect.3);
            let pixels = tile_rgb(rgb, w, rect);
            let data = match format {
                Format::Png => encode_png_fast(&pixels, tw as u32, th as u32)?,
                Format::Jpeg => encode_jpeg(&pixels, tw, th, quality)?,
            };
            Ok(EncodedTile { rect, data })
        })
        .collect()
}

pub enum DeltaFrame {
    Unchanged,
    Key(Vec<u8>),
    Tiles(Vec<EncodedTile>),
}

/// Mirrors `executor::DeltaState`: ONE previous RGB frame baseline; the baseline
/// advances only via `commit` after a delivered frame; keyframe on the first frame and
/// every `KEYFRAME_INTERVAL`th delivered frame.
#[derive(Default)]
pub struct DeltaState {
    prev: Option<(Vec<u8>, u16, u16)>,
    since_key: u64,
}

impl DeltaState {
    pub fn tick(
        &self,
        rgb: &[u8],
        w: u16,
        h: u16,
        format: Format,
        quality: u8,
    ) -> Result<DeltaFrame> {
        match &self.prev {
            Some((prev, pw, ph)) if (*pw, *ph) == (w, h) => {
                let rects = diff_tiles(prev, rgb, w, h);
                if rects.is_empty() {
                    return Ok(DeltaFrame::Unchanged);
                }
                if self.since_key >= KEYFRAME_INTERVAL - 1 {
                    Ok(DeltaFrame::Key(encode_full(rgb, w, h, format, quality)?))
                } else {
                    Ok(DeltaFrame::Tiles(encode_tiles(
                        rgb, w, &rects, format, quality,
                    )?))
                }
            }
            _ => Ok(DeltaFrame::Key(encode_full(rgb, w, h, format, quality)?)),
        }
    }

    pub fn commit(&mut self, rgb: Vec<u8>, w: u16, h: u16, was_key: bool) {
        self.prev = Some((rgb, w, h));
        self.since_key = if was_key { 0 } else { self.since_key + 1 };
    }
}

/// Mirrors `executor::encode_frame` for the no-downscale case (the spike feeds frames
/// already at the delivered resolution, exactly like the delta path post-`capture_raw`).
pub fn encode_full(rgb: &[u8], w: u16, h: u16, format: Format, quality: u8) -> Result<Vec<u8>> {
    match format {
        Format::Png => encode_png(rgb, w as u32, h as u32),
        Format::Jpeg => encode_jpeg(rgb, w, h, quality),
    }
}
