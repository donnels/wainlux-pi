use fontdue::layout::{CoordinateSystem, Layout, LayoutSettings, TextStyle};
use fontdue::{Font, FontSettings};
use laser::Raster;

pub const DEFAULT_FONT: &str = "sans-bold";

pub fn raster(value: &str, font_px: f32, width: u32, height: u32) -> Result<Raster, String> {
    raster_with_font(value, DEFAULT_FONT, font_px, width, height)
}

pub fn raster_with_font(value: &str, face: &str, font_px: f32, width: u32, height: u32) -> Result<Raster, String> {
    if value.trim().is_empty() || !font_px.is_finite() || font_px < 1.0
        || width < 3 || height < 3 || width > 10000 || height > 10000
    {
        return Err("text needs content, a positive font size and a valid bounding box".into());
    }
    let bytes: &[u8] = match face {
        "sans-bold" => include_bytes!("../assets/DejaVuSans-Bold.ttf"),
        "mono" => include_bytes!("../assets/DejaVuSansMono.ttf"),
        _ => return Err("unknown font; choose sans-bold or mono".into()),
    };
    let font = Font::from_bytes(bytes, FontSettings::default()).map_err(|error| error.to_string())?;
    if value.chars().any(|character| !character.is_whitespace() && font.lookup_glyph_index(character) == 0) {
        return Err("text contains a character unavailable in the selected font".into());
    }
    let mut layout = Layout::new(CoordinateSystem::PositiveYDown);
    layout.reset(&LayoutSettings { x: 1.0, y: 1.0, max_width: Some((width - 2) as f32), ..LayoutSettings::default() });
    layout.append(&[&font], &TextStyle::new(value, font_px, 0));
    let shift_x = 1.0 - layout.glyphs().iter().filter(|glyph| glyph.width > 0).map(|glyph| glyph.x.floor()).fold(1.0, f32::min);
    let shift_y = 1.0 - layout.glyphs().iter().filter(|glyph| glyph.height > 0).map(|glyph| glyph.y.floor()).fold(1.0, f32::min);
    let mut out = Raster::new(width, height);
    for glyph in layout.glyphs() {
        if glyph.width == 0 || glyph.height == 0 { continue; }
        let left = (glyph.x.floor() + shift_x) as i32;
        let top = (glyph.y.floor() + shift_y) as i32;
        if left < 0 || top < 0 || left as usize + glyph.width > width as usize || top as usize + glyph.height > height as usize {
            return Err("text does not fit its box; enlarge the box or reduce font size".into());
        }
        let (metrics, coverage) = font.rasterize_config(glyph.key);
        for row in 0..metrics.height {
            for column in 0..metrics.width {
                if coverage[row * metrics.width + column] >= 128 {
                    out.set(left as u32 + column as u32, top as u32 + row as u32, true);
                }
            }
        }
    }
    if !out.bits.iter().any(|pixel| *pixel) {
        return Err("text rendered no marks at this font size".into());
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn newlines_make_distinct_rows_of_raster_text() {
        let image = raster("Jane Doe\nEngineer", 20.0, 180, 90).unwrap();
        let mut bands = 0;
        let mut previous = false;
        for row in 0..image.height {
            let ink = (0..image.width).any(|column| image.get(column, row));
            if ink && !previous { bands += 1; }
            previous = ink;
        }
        assert_eq!(bands, 2);
        assert_eq!(image, raster("Jane Doe\nEngineer", 20.0, 180, 90).unwrap());
    }

    #[test]
    fn wrapping_fits_or_is_refused_without_clipping() {
        assert!(raster("One two three four five", 20.0, 100, 160).is_ok());
        assert!(raster("One two three four five", 20.0, 100, 15).is_err());
        assert!(raster("", 20.0, 100, 100).is_err());
    }

    #[test]
    fn proportional_bold_has_more_ink_than_the_old_mono_face() {
        let bold = raster_with_font("Readable", "sans-bold", 60.0, 600, 160).unwrap();
        let mono = raster_with_font("Readable", "mono", 60.0, 600, 160).unwrap();
        assert!(bold.bits.iter().filter(|pixel| **pixel).count() > mono.bits.iter().filter(|pixel| **pixel).count());
        assert!(raster_with_font("Readable", "not-a-font", 60.0, 600, 160).is_err());
    }
}