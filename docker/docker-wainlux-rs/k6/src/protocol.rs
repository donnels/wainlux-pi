//! Packet builders. Pure. No I/O.

pub const ACK: u8 = 0x09;
pub const NACK: u8 = 0x08;
pub const HEARTBEAT: [u8; 4] = [0xFF, 0xFF, 0xFF, 0xFE];
pub const CENTER_X_OFFSET: u16 = 67;
pub const DATA_CHUNK: usize = 1900;
const HEADER_OVERHEAD: u32 = 33;
const BLOCK_DIVISOR: u32 = 4094;

pub const VERSION: [u8; 4] = [0xFF, 0x00, 0x04, 0x00];
pub const CONNECT: [u8; 4] = [0x0A, 0x00, 0x04, 0x00];
pub const HOME: [u8; 4] = [0x17, 0x00, 0x04, 0x00];
pub const STOP: [u8; 4] = [0x16, 0x00, 0x04, 0x00];
pub const FRAMING: [u8; 4] = [0x21, 0x00, 0x04, 0x00];
pub const INIT: [u8; 11] = [0x24, 0x00, 0x0B, 0x00, 0, 0, 0, 0, 0, 0, 0];

/// Two's complement of the sum of all bytes except the last.
pub fn checksum(packet: &[u8]) -> u8 {
    let n = packet.len().saturating_sub(1);
    let sum = packet[..n].iter().fold(0u8, |a, b| a.wrapping_add(*b));
    sum.wrapping_neg()
}

/// DATA (0x22): opcode, u16 length, payload, checksum.
pub fn data_packet(payload: &[u8]) -> Vec<u8> {
    assert!(payload.len() <= DATA_CHUNK, "DATA payload too large");
    let len = payload.len() + 4;
    let mut p = Vec::with_capacity(len);
    p.push(0x22);
    p.extend_from_slice(&(len as u16).to_be_bytes());
    p.extend_from_slice(payload);
    p.push(0);
    let c = checksum(&p);
    *p.last_mut().unwrap() = c;
    p
}

/// BOUNDS (0x20): preview rectangle. Traces until STOP.
pub fn bounds_packet(w: u16, h: u16, cx: u16, cy: u16) -> [u8; 11] {
    let mut p = [0x20, 0x00, 0x0B, 0, 0, 0, 0, 0, 0, 0, 0];
    p[3..5].copy_from_slice(&w.to_be_bytes());
    p[5..7].copy_from_slice(&h.to_be_bytes());
    p[7..9].copy_from_slice(&cx.to_be_bytes());
    p[9..11].copy_from_slice(&cy.to_be_bytes());
    p
}

/// JOB_HEADER (0x23), raster only. 38 bytes, big-endian fields.
pub fn job_header(w: u16, h: u16, power: u16, depth: u16, cx: u16, cy: u16) -> [u8; 38] {
    let raster = ((w as u32 + 7) / 8) * h as u32;
    let total = raster + HEADER_OVERHEAD;
    let mut p = [0u8; 38];
    p[0] = 0x23;
    p[2] = 38;
    p[3..5].copy_from_slice(&((total / BLOCK_DIVISOR + 1) as u16).to_be_bytes());
    p[5] = 1;
    p[6..8].copy_from_slice(&w.to_be_bytes());
    p[8..10].copy_from_slice(&h.to_be_bytes());
    p[10..12].copy_from_slice(&(HEADER_OVERHEAD as u16).to_be_bytes());
    p[12..14].copy_from_slice(&power.to_be_bytes());
    p[14..16].copy_from_slice(&depth.to_be_bytes());
    p[20..24].copy_from_slice(&total.to_be_bytes());
    p[32..34].copy_from_slice(&cx.to_be_bytes());
    p[34..36].copy_from_slice(&cy.to_be_bytes());
    p[36] = 1;
    p
}

/// Centre for a box whose top-left is (x, y).
pub fn centre(x: u16, y: u16, w: u16, h: u16) -> (u16, u16) {
    (x + w / 2 + CENTER_X_OFFSET, y + h / 2)
}

/// Raster to DATA payload: MSB first, rows padded to whole bytes.
pub fn pack(r: &laser::Raster) -> Vec<u8> {
    let row = (r.width as usize + 7) / 8;
    let mut out = vec![0u8; row * r.height as usize];
    for (i, on) in r.bits.iter().enumerate() {
        if *on {
            let (y, x) = (i / r.width as usize, i % r.width as usize);
            out[y * row + x / 8] |= 0x80 >> (x % 8);
        }
    }
    out
}

fn blank_row(r: &laser::Raster, y: u32) -> bool {
    (0..r.width).all(|x| !r.get(x, y))
}

fn rows(r: &laser::Raster, from: u32, to: u32) -> laser::Raster {
    let w = r.width as usize;
    laser::Raster { width: r.width, height: to - from, bits: r.bits[from as usize * w..to as usize * w].to_vec() }
}

/// Split a raster into bands the K6 accepts. It answers 0x08 to an all-zero DATA chunk.
/// Blank edge rows are dropped. A blank run of half a chunk or more starts a new band, so the
/// zeros left in a band (run + partial rows, under 1350 bytes up to 1600 px wide) never fill a chunk.
/// Returns (first row, band).
pub fn bands(r: &laser::Raster) -> Vec<(u32, laser::Raster)> {
    let row_bytes = ((r.width as usize + 7) / 8).max(1);
    let split_at = (DATA_CHUNK / 2 / row_bytes).max(1);
    let (mut out, mut start, mut last_ink, mut gap) = (Vec::new(), None, 0, 0);
    for y in 0..r.height {
        if !blank_row(r, y) {
            start.get_or_insert(y);
            last_ink = y;
            gap = 0;
            continue;
        }
        gap += 1;
        if gap >= split_at {
            if let Some(s) = start.take() {
                out.push((s, rows(r, s, last_ink + 1)));
            }
        }
    }
    if let Some(s) = start {
        out.push((s, rows(r, s, last_ink + 1)));
    }
    out
}

/// True if any DATA chunk of this payload would be all zero.
pub fn has_zero_chunk(data: &[u8]) -> bool {
    data.chunks(DATA_CHUNK).any(|c| c.iter().all(|b| *b == 0))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(b: &[u8]) -> String {
        b.iter().map(|x| format!("{x:02x}")).collect()
    }

    // Golden bytes generated from the Python k6 library that burned the first QR.
    #[test]
    fn golden_job_header() {
        let (cx, cy) = centre(0, 0, 377, 377);
        assert_eq!(
            hex(&job_header(377, 377, 500, 10, cx, cy)),
            "23002600050101790179002101f4000a00000000000046d1000000000000000000ff00bc0100"
        );
        assert_eq!(
            hex(&job_header(1600, 1000, 500, 10, 900, 700)),
            "230026003101064003e8002101f4000a0000000000030d610000000000000000038402bc0100"
        );
    }

    #[test]
    fn golden_bounds() {
        let (cx, cy) = centre(0, 0, 377, 377);
        assert_eq!(hex(&bounds_packet(377, 377, cx, cy)), "20000b0179017900ff00bc");
    }

    #[test]
    fn golden_data() {
        assert_eq!(hex(&data_packet(&[0xAA, 0x55, 0x00, 0xFF])), "220008aa5500ffd8");
    }

    #[test]
    fn checksum_makes_sum_zero() {
        let p = data_packet(&[1, 2, 3]);
        assert_eq!(p.iter().fold(0u8, |a, b| a.wrapping_add(*b)), 0);
    }

    #[test]
    fn packs_msb_first_with_padding() {
        let mut r = laser::Raster::new(10, 1);
        r.set(0, 0, true);
        r.set(9, 0, true);
        assert_eq!(pack(&r), vec![0x80, 0x40]);
    }

    /// Two squares on a diagonal with `gap` blank rows between (as the ladder's blank-rows rung).
    fn diagonal(side: u32, gap: u32) -> laser::Raster {
        let n = 2 * side + gap;
        let mut r = laser::Raster::new(n, n);
        for y in 0..side {
            for x in 0..side {
                r.set(x, y, true);
                r.set(x + side + gap, y + side + gap, true);
            }
        }
        r
    }

    #[test]
    fn long_blank_run_splits_into_bands_without_zero_chunks() {
        let r = diagonal(100, 100);
        assert!(has_zero_chunk(&pack(&r)), "unsplit, the K6 would answer 0x08");
        let b = bands(&r);
        assert_eq!(b.iter().map(|(y, band)| (*y, band.height)).collect::<Vec<_>>(), [(0, 100), (200, 100)]);
        assert!(b.iter().all(|(_, band)| !has_zero_chunk(&pack(band))));
    }

    #[test]
    fn blank_edges_are_trimmed_short_gaps_kept() {
        let mut r = laser::Raster::new(300, 200);
        for x in 0..300 {
            r.set(x, 50, true);
            r.set(x, 60, true);
        }
        assert_eq!(bands(&r).iter().map(|(y, band)| (*y, band.height)).collect::<Vec<_>>(), [(50, 11)]);
    }

    #[test]
    fn blank_raster_has_no_bands() {
        assert!(bands(&laser::Raster::new(100, 100)).is_empty());
    }
}
