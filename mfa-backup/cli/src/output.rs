//! Render the same payload into four formats, then write private files.

use crate::pdf;
use image::{DynamicImage, ImageFormat, Luma};
use qrcode::{render::svg, EcLevel, QrCode};
use std::fs::{self, OpenOptions};
use std::io::{self, Cursor, Write};
use std::path::{Path, PathBuf};

const PIXELS_PER_MODULE: u32 = 12;

pub(crate) fn save(prefix: &Path, text: &str) -> Result<(), String> {
    // One matrix keeps the QR identical across SVG, PNG, and PDF.
    // QR construction belongs to the library, not to our own implementation.
    let qr = QrCode::with_error_correction_level(text.as_bytes(), EcLevel::M)
        .map_err(|_| "Cannot encode the payload as a QR.".to_owned())?;
    let svg = qr
        .render::<svg::Color>()
        .quiet_zone(true)
        .module_dimensions(PIXELS_PER_MODULE, PIXELS_PER_MODULE)
        .build();
    let png = render_png(&qr)?;
    let pdf = pdf::render(text, &qr)?;

    // Complete every render before opening a file. A layout error must not
    // leave behind a partial set of backups.
    let outputs: [(&str, &[u8]); 4] = [
        (".txt", text.as_bytes()),
        (".pdf", &pdf),
        (".svg", svg.as_bytes()),
        (".png", &png),
    ];
    write_outputs(prefix, &outputs).map_err(|_| {
        "Cannot create outputs. Check permissions and use a new output prefix.".to_owned()
    })
}

fn render_png(qr: &QrCode) -> Result<Vec<u8>, String> {
    let bitmap = qr
        .render::<Luma<u8>>()
        .quiet_zone(true)
        .module_dimensions(PIXELS_PER_MODULE, PIXELS_PER_MODULE)
        .build();
    // Cursor gives a byte vector a seekable file-like interface for the encoder.
    let mut encoded = Cursor::new(Vec::new());
    DynamicImage::ImageLuma8(bitmap)
        .write_to(&mut encoded, ImageFormat::Png)
        .map_err(|_| "Cannot encode PNG output.".to_owned())?;
    Ok(encoded.into_inner())
}

fn output_path(prefix: &Path, suffix: &str) -> PathBuf {
    // OsString preserves paths that are not valid UTF-8 on the host OS.
    let mut name = prefix.as_os_str().to_owned();
    name.push(suffix);
    PathBuf::from(name)
}

fn write_private(path: &Path, data: &[u8]) -> io::Result<()> {
    let mut options = OpenOptions::new();
    // create_new fails if the path already exists, including a symlink.
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        // This block is compiled only on Unix. 0600 grants access to the owner.
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path)?;
    if let Err(error) = file.write_all(data).and_then(|_| file.sync_all()) {
        // Close our file before attempting cleanup. Preserve the original error.
        drop(file);
        let _ = fs::remove_file(path);
        return Err(error);
    }
    Ok(())
}

fn write_outputs(prefix: &Path, outputs: &[(&str, &[u8])]) -> io::Result<()> {
    let mut created_paths = Vec::new();
    for (suffix, data) in outputs {
        let path = output_path(prefix, suffix);
        if let Err(error) = write_private(&path, data) {
            // Roll back only files this call created. Never remove existing files.
            for created_path in created_paths {
                let _ = fs::remove_file(created_path);
            }
            return Err(error);
        }
        created_paths.push(path);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn outputs_are_private_and_failure_preserves_existing_files() {
        let prefix = std::env::temp_dir().join(format!("mfa-backup-test-{}", std::process::id()));
        let png_path = output_path(&prefix, ".png");
        write_private(&png_path, b"EXISTING").unwrap();
        assert!(write_outputs(
            &prefix,
            &[
                (".txt", b"DEMO"),
                (".pdf", b"DEMO"),
                (".svg", b"DEMO"),
                (".png", b"NEW"),
            ]
        )
        .is_err());
        for suffix in [".txt", ".pdf", ".svg"] {
            assert!(!output_path(&prefix, suffix).exists());
        }
        assert_eq!(fs::read(&png_path).unwrap(), b"EXISTING");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&png_path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
        fs::remove_file(png_path).unwrap();
    }
}
