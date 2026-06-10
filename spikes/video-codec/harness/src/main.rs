//! vcspike — software-video-codec spike harness (spikes/video-codec).
//!
//! Reads raw RGB24 frames on stdin (rawvideo, no header) and runs ONE encode mode over
//! the sequence on a single thread, printing a JSON summary to stdout:
//!
//! ```text
//! vcspike <mode> <width> <height> <fps> [bitrate_bps]
//!   delta-png    shinkend's B2 dirty-tile path, PNG tiles (faithful replication, src/b2.rs)
//!   delta-jpeg   same path with format=jpeg quality=80 (the runtime's other tile codec)
//!   jpeg         full-frame JPEG q80 per frame + idle suppression (runtime delta=false)
//!   h264         in-process software H.264 (openh264, ScreenContentRealTime, 1 thread,
//!                zero-latency: no B-frames, no lookahead) — requires bitrate_bps
//! ```
//!
//! Per mode it measures: payload bytes (raw, pre-base64), encode CPU per input frame,
//! a client-side decode+composite pass (PNG/JPEG decode + blit, or H.264 decode +
//! YUV→RGB), and mean PSNR of the client view vs the source frames (`null` = lossless).

mod b2;

use anyhow::{bail, Context, Result};
use std::io::Read;
use std::time::Instant;

fn read_frames(w: usize, h: usize) -> Result<Vec<Vec<u8>>> {
    let mut frames = Vec::new();
    let mut stdin = std::io::stdin().lock();
    let frame_len = w * h * 3;
    loop {
        let mut buf = vec![0u8; frame_len];
        let mut got = 0usize;
        while got < frame_len {
            let n = stdin.read(&mut buf[got..])?;
            if n == 0 {
                break;
            }
            got += n;
        }
        if got == 0 {
            break;
        }
        if got != frame_len {
            bail!("trailing partial frame: {got} of {frame_len} bytes");
        }
        frames.push(buf);
    }
    if frames.is_empty() {
        bail!("no frames on stdin");
    }
    Ok(frames)
}

fn stats(ms: &[f64]) -> serde_json::Value {
    if ms.is_empty() {
        return serde_json::Value::Null;
    }
    let mut sorted = ms.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let pct = |q: f64| sorted[((sorted.len() - 1) as f64 * q).round() as usize];
    serde_json::json!({
        "mean": ms.iter().sum::<f64>() / ms.len() as f64,
        "p50": pct(0.50),
        "p95": pct(0.95),
        "max": sorted[sorted.len() - 1],
    })
}

/// Sum of squared subpixel error for PSNR accumulation.
fn sse(a: &[u8], b: &[u8]) -> f64 {
    a.iter()
        .zip(b)
        .map(|(&x, &y)| {
            let d = x as f64 - y as f64;
            d * d
        })
        .sum()
}

fn psnr_from(total_sse: f64, total_subpixels: f64) -> serde_json::Value {
    if total_sse == 0.0 {
        serde_json::Value::Null // lossless
    } else {
        let mse = total_sse / total_subpixels;
        serde_json::json!(10.0 * (255.0f64 * 255.0 / mse).log10())
    }
}

fn decode_png_into(data: &[u8]) -> Result<(Vec<u8>, u32, u32)> {
    let dec = png::Decoder::new(data);
    let mut reader = dec.read_info()?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf)?;
    buf.truncate(info.buffer_size());
    Ok((buf, info.width, info.height))
}

fn decode_jpeg_into(data: &[u8]) -> Result<(Vec<u8>, u32, u32)> {
    let mut dec = jpeg_decoder::Decoder::new(data);
    let pixels = dec.decode().context("jpeg decode")?;
    let info = dec.info().context("jpeg info")?;
    Ok((pixels, info.width as u32, info.height as u32))
}

fn blit(fb: &mut [u8], fw: usize, tile: &[u8], rect: b2::TileRect) {
    let (x, y, tw, th) = (
        rect.0 as usize,
        rect.1 as usize,
        rect.2 as usize,
        rect.3 as usize,
    );
    for row in 0..th {
        let dst = ((y + row) * fw + x) * 3;
        let src = row * tw * 3;
        fb[dst..dst + tw * 3].copy_from_slice(&tile[src..src + tw * 3]);
    }
}

/// The B2 dirty-tile path (delta-png / delta-jpeg) and the full-frame jpeg path.
fn run_image_mode(mode: &str, w: u16, h: u16, fps: f64, frames: &[Vec<u8>]) -> Result<()> {
    let (delta, format) = match mode {
        "delta-png" => (true, b2::Format::Png),
        "delta-jpeg" => (true, b2::Format::Jpeg),
        "jpeg" => (false, b2::Format::Jpeg),
        _ => unreachable!(),
    };
    let q = b2::DEFAULT_JPEG_QUALITY;
    let (fw, fh) = (w as usize, h as usize);

    let mut state = b2::DeltaState::default();
    let mut encode_ms = Vec::new();
    let mut decode_ms = Vec::new();
    let mut bytes_total = 0u64;
    let mut delivered = 0u64;
    let mut keyframes = 0u64;
    let mut tiles_total = 0u64;
    let mut fb = vec![0u8; fw * fh * 3]; // client-side composited view
    let mut total_sse = 0f64;
    // delta=false idle suppression: the runtime hashes the encoded frame; identical raw
    // frames encode identically, so raw equality is an equivalent (cheaper) predicate.
    let mut prev_raw: Option<Vec<u8>> = None;

    for frame in frames {
        if delta {
            let t = Instant::now();
            let out = state.tick(frame, w, h, format, q)?;
            encode_ms.push(t.elapsed().as_secs_f64() * 1e3);
            match out {
                b2::DeltaFrame::Unchanged => {}
                b2::DeltaFrame::Key(data) => {
                    bytes_total += data.len() as u64;
                    delivered += 1;
                    keyframes += 1;
                    let t = Instant::now();
                    let (pix, _, _) = match format {
                        b2::Format::Png => decode_png_into(&data)?,
                        b2::Format::Jpeg => decode_jpeg_into(&data)?,
                    };
                    fb.copy_from_slice(&pix);
                    decode_ms.push(t.elapsed().as_secs_f64() * 1e3);
                    state.commit(frame.clone(), w, h, true);
                }
                b2::DeltaFrame::Tiles(tiles) => {
                    bytes_total += tiles.iter().map(|t| t.data.len() as u64).sum::<u64>();
                    delivered += 1;
                    tiles_total += tiles.len() as u64;
                    let t = Instant::now();
                    for tile in &tiles {
                        let (pix, _, _) = match format {
                            b2::Format::Png => decode_png_into(&tile.data)?,
                            b2::Format::Jpeg => decode_jpeg_into(&tile.data)?,
                        };
                        blit(&mut fb, fw, &pix, tile.rect);
                    }
                    decode_ms.push(t.elapsed().as_secs_f64() * 1e3);
                    state.commit(frame.clone(), w, h, false);
                }
            }
        } else {
            // idle suppression — the client holds the last decoded view
            if prev_raw.as_deref() != Some(frame.as_slice()) {
                let t = Instant::now();
                let data = b2::encode_full(frame, w, h, format, q)?;
                encode_ms.push(t.elapsed().as_secs_f64() * 1e3);
                bytes_total += data.len() as u64;
                delivered += 1;
                let t = Instant::now();
                let (pix, _, _) = decode_jpeg_into(&data)?;
                fb.copy_from_slice(&pix);
                decode_ms.push(t.elapsed().as_secs_f64() * 1e3);
                prev_raw = Some(frame.clone());
            }
        }
        total_sse += sse(&fb, frame);
    }

    let duration_s = frames.len() as f64 / fps;
    let out = serde_json::json!({
        "mode": mode,
        "frames_in": frames.len(),
        "frames_delivered": delivered,
        "keyframes": keyframes,
        "tiles_total": tiles_total,
        "bytes_total": bytes_total,
        "bytes_per_s": bytes_total as f64 / duration_s,
        "encode_ms_per_frame": stats(&encode_ms),
        "decode_ms_per_delivered_frame": stats(&decode_ms),
        "psnr_db": psnr_from(total_sse, (frames.len() * fw * fh * 3) as f64),
    });
    println!("{}", serde_json::to_string(&out)?);
    Ok(())
}

/// In-process software H.264: openh264, screen-content realtime, single thread,
/// zero-latency by construction (OpenH264 emits no B-frames and has no lookahead).
fn run_h264(w: u16, h: u16, fps: f64, bitrate_bps: u32, frames: &[Vec<u8>]) -> Result<()> {
    use openh264::decoder::Decoder;
    use openh264::encoder::{
        BitRate, Encoder, EncoderConfig, FrameRate, IntraFramePeriod, RateControlMode, UsageType,
    };
    use openh264::formats::{RgbSliceU8, YUVBuffer};
    use openh264::OpenH264API;

    let (fw, fh) = (w as usize, h as usize);
    let cfg = EncoderConfig::new()
        .bitrate(BitRate::from_bps(bitrate_bps))
        .max_frame_rate(FrameRate::from_hz(fps as f32))
        .usage_type(UsageType::ScreenContentRealTime)
        .rate_control_mode(RateControlMode::Bitrate)
        // Match the B2 path's self-heal cadence (KEYFRAME_INTERVAL = 30 delivered frames).
        .intra_frame_period(IntraFramePeriod::from_num_frames(
            b2::KEYFRAME_INTERVAL as u32,
        ))
        .num_threads(1); // single-core measurement
    let mut enc = Encoder::with_api_config(OpenH264API::from_source(), cfg)?;
    let mut dec = Decoder::new()?;

    let mut convert_ms = Vec::new();
    let mut encode_ms = Vec::new();
    let mut decode_ms = Vec::new();
    let mut bytes_total = 0u64;
    let mut delivered = 0u64;
    let mut skipped = 0u64;
    let mut fb = vec![0u8; fw * fh * 3];
    let mut have_picture = false;
    let mut total_sse = 0f64;
    let mut sse_frames = 0usize;

    for frame in frames {
        let t = Instant::now();
        let yuv = YUVBuffer::from_rgb8_source(RgbSliceU8::new(frame, (fw, fh)));
        convert_ms.push(t.elapsed().as_secs_f64() * 1e3);

        let t = Instant::now();
        let bitstream = enc.encode(&yuv)?;
        let au = bitstream.to_vec();
        encode_ms.push(t.elapsed().as_secs_f64() * 1e3);

        if au.is_empty() {
            skipped += 1; // rate control skipped this frame; client holds last picture
        } else {
            bytes_total += au.len() as u64;
            delivered += 1;
            let t = Instant::now();
            if let Some(pic) = dec.decode(&au)? {
                pic.write_rgb8(&mut fb);
                have_picture = true;
            }
            decode_ms.push(t.elapsed().as_secs_f64() * 1e3);
        }
        if have_picture {
            total_sse += sse(&fb, frame);
            sse_frames += 1;
        }
    }

    let duration_s = frames.len() as f64 / fps;
    let enc_total: Vec<f64> = convert_ms
        .iter()
        .zip(&encode_ms)
        .map(|(a, b)| a + b)
        .collect();
    let out = serde_json::json!({
        "mode": "h264",
        "bitrate_bps": bitrate_bps,
        "frames_in": frames.len(),
        "frames_delivered": delivered,
        "frames_rc_skipped": skipped,
        "bytes_total": bytes_total,
        "bytes_per_s": bytes_total as f64 / duration_s,
        "encode_ms_per_frame": stats(&enc_total),
        "encode_ms_split": {
            "rgb_to_yuv420": stats(&convert_ms),
            "h264_encode": stats(&encode_ms),
        },
        "decode_ms_per_delivered_frame": stats(&decode_ms),
        "psnr_db": psnr_from(total_sse, (sse_frames * fw * fh * 3) as f64),
    });
    println!("{}", serde_json::to_string(&out)?);
    Ok(())
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 5 {
        bail!("usage: vcspike <delta-png|delta-jpeg|jpeg|h264> <w> <h> <fps> [bitrate_bps]");
    }
    let mode = args[1].as_str();
    let w: u16 = args[2].parse()?;
    let h: u16 = args[3].parse()?;
    let fps: f64 = args[4].parse()?;
    let frames = read_frames(w as usize, h as usize)?;
    match mode {
        "delta-png" | "delta-jpeg" | "jpeg" => run_image_mode(mode, w, h, fps, &frames),
        "h264" => {
            let bitrate: u32 = args
                .get(5)
                .context("h264 mode needs bitrate_bps")?
                .parse()?;
            run_h264(w, h, fps, bitrate, &frames)
        }
        other => bail!("unknown mode {other:?}"),
    }
}
