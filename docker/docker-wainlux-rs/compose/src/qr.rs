//! Text to QR raster.

use laser::Raster;

/// QR with a 4-module quiet zone, scaled to whole pixels per module.
pub fn qr(text: &str, side_px: u32) -> Result<Raster, String> {
    if text.trim().is_empty() { return Err("QR content is required".into()); }
    let code = qrcode::QrCode::new(text.as_bytes()).map_err(|e| e.to_string())?;
    let n = code.width();
    let quiet_zone = 4;
    let full = n + 2 * quiet_zone;
    let module = side_px / full as u32;
    if module < 4 {
        return Err(format!("{full} modules need at least {} px; got {side_px}", full * 4));
    }
    let dark = code.to_colors();
    let mut m = vec![false; full * full];
    for y in 0..n {
        for x in 0..n {
            m[(y + quiet_zone) * full + x + quiet_zone] = dark[y * n + x] == qrcode::Color::Dark;
        }
    }
    Ok(Raster::from_modules(&m, full, module))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fits_and_has_quiet_zone() {
        let r = qr("https://github.com/donnels", 380).unwrap();
        assert!(r.width <= 380);
        let modules = qrcode::QrCode::new("https://github.com/donnels".as_bytes()).unwrap().width() as u32;
        let margin = 4 * (r.width / (modules + 8));
        for row in 0..r.height {
            for column in 0..r.width {
                if row < margin || column < margin || row >= r.height - margin || column >= r.width - margin {
                    assert!(!r.get(column, row));
                }
            }
        }
        assert!(r.get(margin, margin), "finder begins after four empty modules");
    }

    #[test]
    fn too_small_is_refused() {
        assert!(qr("https://github.com/donnels", 40).is_err());
        assert!(qr("", 800).is_err());
        assert!(qr(" \n", 800).is_err());
    }
}
