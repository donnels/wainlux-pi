use laser::Raster;
use serde_json::{json, Value};

const TIGER: &[u8] = include_bytes!("../assets/tiger.png");

pub fn catalogue() -> Value {
    json!([{"id":"tiger","name":"Tiger demo","width_px":628,"height_px":572,"format":"image/png;1-bit","href":"/api/assets/tiger.png"}])
}

pub fn bytes(id: &str) -> Option<&'static [u8]> { (id == "tiger").then_some(TIGER) }

pub fn raster(id: &str, width: u32, height: u32) -> Result<Raster, String> {
    let bytes = bytes(id).ok_or("unknown public asset")?;
    let mut reader = png::Decoder::new(std::io::Cursor::new(bytes)).read_info().map_err(|error| error.to_string())?;
    if reader.info().bit_depth != png::BitDepth::One || reader.info().color_type != png::ColorType::Grayscale { return Err("asset must be a prepared 1-bit grayscale PNG".into()); }
    let (source_width, source_height) = (reader.info().width, reader.info().height);
    let mut pixels = vec![0; reader.output_buffer_size()];
    reader.next_frame(&mut pixels).map_err(|error| error.to_string())?;
    let scale = (width as f64 / source_width as f64).min(height as f64 / source_height as f64);
    let (width, height) = ((source_width as f64 * scale).floor() as u32, (source_height as f64 * scale).floor() as u32);
    if width == 0 || height == 0 { return Err("asset box is too small".into()); }
    let mut raster = Raster::new(width, height);
    let stride = (source_width as usize + 7) / 8;
    for row in 0..height {
        for column in 0..width {
            let source_x = column as u64 * source_width as u64 / width as u64;
            let source_y = row as u64 * source_height as u64 / height as u64;
            if pixels[source_y as usize * stride + source_x as usize / 8] & (128 >> (source_x % 8)) == 0 { raster.set(column, row, true); }
        }
    }
    Ok(raster)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prepared_tiger_keeps_its_aspect_ratio() {
        let raster = raster("tiger", 800, 800).unwrap();
        assert_eq!(raster.width, 800); assert_eq!(raster.height, 728);
        assert!(raster.bits.iter().any(|pixel| *pixel));
        assert!(super::raster("https://example.com/image.png", 800, 800).is_err());
    }
}