//! Lay out the readable payload and a vector QR on one A4 page.
//! This module returns PDF bytes in memory; it never creates a file.

use printpdf::path::PaintMode;
use printpdf::{Color, Greyscale, Mm, PdfDocument, PdfLayerReference, Rect};
use qrcode::QrCode;
use std::io::Cursor;

// Explicit units make it harder to mix millimetres with PDF font points.
const A4_WIDTH_MM: f32 = 210.0;
const A4_HEIGHT_MM: f32 = 297.0;
const LEFT_MARGIN_MM: f32 = 20.0;
const TEXT_WIDTH_MM: f32 = 170.0;
const TEXT_TOP_MM: f32 = 142.0;
const TEXT_BOTTOM_MM: f32 = 22.0;
const TITLE_BASELINE_MM: f32 = 277.0;
const QR_SIDE_MM: f32 = 100.0;
const QR_BOTTOM_MM: f32 = 157.0;
const QUIET_ZONE_MODULES: usize = 4;
const LINE_SPACING: f32 = 1.25;
const MONOSPACE_WIDTH_FACTOR: f32 = 0.61;

// include_bytes! bundles the font at compile time. Printing needs no font file
// on the user's machine. The font's redistribution license is kept in assets.
const PRINT_FONT: &[u8] = include_bytes!("../assets/DejaVuSansMono.ttf");

struct PrintLayout {
    font_size_points: f32,
    line_height_mm: f32,
    lines: Vec<String>,
}

fn points_to_mm(points: f32) -> f32 {
    points * 25.4 / 72.0
}

fn print_layout(text: &str) -> Result<PrintLayout, String> {
    let font = ttf_parser::Face::parse(PRINT_FONT, 0)
        .map_err(|_| "Cannot read the embedded print font.".to_owned())?;
    for character in text.chars() {
        if !character.is_whitespace() && font.glyph_index(character).is_none() {
            return Err("The print font does not support a character in the payload. Use supported text; nothing was written.".into());
        }
    }

    // Try larger text first, but do not shrink below the agreed 8-point floor.
    let printable_text = text.replace('\t', "    ");
    for font_size_points in [12.0_f32, 11.0, 10.0, 9.0, 8.0] {
        let character_width_mm = points_to_mm(font_size_points * MONOSPACE_WIDTH_FACTOR);
        let columns = (TEXT_WIDTH_MM / character_width_mm) as usize;
        let wrap_options = textwrap::Options::new(columns).break_words(false);
        let mut lines = Vec::new();
        for source_line in printable_text.lines() {
            for wrapped_line in textwrap::wrap(source_line, &wrap_options) {
                lines.push(wrapped_line.into_owned());
            }
        }

        let line_height_mm = points_to_mm(font_size_points * LINE_SPACING);
        // saturating_sub avoids underflow when the line list is empty.
        let line_gaps = lines.len().saturating_sub(1);
        let last_baseline_mm = TEXT_TOP_MM - line_gaps as f32 * line_height_mm;
        let width_fits = lines.iter().all(|line| line.chars().count() <= columns);
        if width_fits && last_baseline_mm >= TEXT_BOTTOM_MM {
            return Ok(PrintLayout {
                font_size_points,
                line_height_mm,
                lines,
            });
        }
    }
    Err("Text cannot fit on one A4 page at a readable size. Shorten long lines or remove extra blank lines; nothing was written.".into())
}

fn draw_qr(layer: &PdfLayerReference, qr: &QrCode) {
    let matrix_width = qr.width();
    let total_modules = matrix_width + 2 * QUIET_ZONE_MODULES;
    let module_size_mm = QR_SIDE_MM / total_modules as f32;
    let qr_left_mm = (A4_WIDTH_MM - QR_SIDE_MM) / 2.0;

    // The QR library chooses the modules. We only draw its dark squares.
    // Empty space around the matrix provides the four-module quiet zone.
    for row in 0..matrix_width {
        for column in 0..matrix_width {
            if qr[(column, row)] != qrcode::Color::Dark {
                continue;
            }
            let left_mm = qr_left_mm + (column + QUIET_ZONE_MODULES) as f32 * module_size_mm;
            // QR rows run downwards; PDF coordinates run upwards from the bottom.
            let bottom_mm = QR_BOTTOM_MM
                + (matrix_width + QUIET_ZONE_MODULES - 1 - row) as f32 * module_size_mm;
            let square = Rect::new(
                Mm(left_mm),
                Mm(bottom_mm),
                Mm(left_mm + module_size_mm),
                Mm(bottom_mm + module_size_mm),
            )
            .with_mode(PaintMode::Fill);
            layer.add_rect(square);
        }
    }
}

pub(crate) fn render(text: &str, qr: &QrCode) -> Result<Vec<u8>, String> {
    let layout = print_layout(text)?;
    let (document, page, layer) = PdfDocument::new(
        "Recovery code backup",
        Mm(A4_WIDTH_MM),
        Mm(A4_HEIGHT_MM),
        "Backup",
    );
    let font = document
        .add_external_font(Cursor::new(PRINT_FONT))
        .map_err(|_| "Cannot embed the print font.".to_owned())?;
    let layer = document.get_page(page).get_layer(layer);
    layer.set_fill_color(Color::Greyscale(Greyscale::new(0.0, None)));
    layer.use_text(
        "Recovery code backup",
        16.0,
        Mm(LEFT_MARGIN_MM),
        Mm(TITLE_BASELINE_MM),
        &font,
    );
    draw_qr(&layer, qr);

    for (line_index, line) in layout.lines.iter().enumerate() {
        let baseline_mm = TEXT_TOP_MM - line_index as f32 * layout.line_height_mm;
        layer.use_text(
            line,
            layout.font_size_points,
            Mm(LEFT_MARGIN_MM),
            Mm(baseline_mm),
            &font,
        );
    }
    document
        .save_to_bytes()
        .map_err(|_| "Cannot encode PDF output.".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn print_layout_wraps_without_losing_text_or_splitting_codes() {
        let text = format!("{}\n\nDEMO-ALPHA-001\n", "Short instructions. ".repeat(20));
        let layout = print_layout(&text).unwrap();
        assert!(layout.font_size_points >= 8.0);
        assert_eq!(
            layout
                .lines
                .join(" ")
                .split_whitespace()
                .collect::<Vec<_>>(),
            text.split_whitespace().collect::<Vec<_>>()
        );
        assert!(layout.lines.iter().any(|line| line == "DEMO-ALPHA-001"));
        assert!(print_layout(&"x".repeat(489)).is_err());
        assert!(print_layout(&"code\n".repeat(100)).is_err());
        assert!(print_layout("Caf\u{e9}\nDEMO-001").is_ok());
        assert!(print_layout("Unsupported: \u{10ffff}").is_err());
    }

    #[test]
    fn print_pdf_has_one_a4_page() {
        let text = "SYNTHETIC TEST ONLY\nDEMO-001\n";
        let qr = QrCode::new(text).unwrap();
        let pdf = render(text, &qr).unwrap();
        let document = printpdf::lopdf::Document::load_mem(&pdf).unwrap();
        let pages = document.get_pages();
        assert_eq!(pages.len(), 1);
        let page = document
            .get_object(*pages.get(&1).unwrap())
            .unwrap()
            .as_dict()
            .unwrap();
        let bounds = page.get(b"MediaBox").unwrap().as_array().unwrap();
        assert!((bounds[2].as_float().unwrap() - 595.276).abs() < 0.1);
        assert!((bounds[3].as_float().unwrap() - 841.89).abs() < 0.1);
    }
}
