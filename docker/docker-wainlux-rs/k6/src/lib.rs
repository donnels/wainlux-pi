//! Wainlux K6 driver. Implements the `laser` contract. Raster only.
//! Byte layouts: documentation/reference-k6_protocol.adoc.

mod device;
pub mod protocol;

pub use device::{Device, Error, Port};

use laser::{Info, Setting};

/// What the K6 is and takes. Defaults are the first QR that scanned on bamboo.
pub fn info() -> Info {
    Info {
        model: "Wainlux K6",
        px_per_mm: 20.0,
        reach_mm: (80.0, 75.0),
        settings: vec![
            Setting { key: "power", label: "Power", min: 0, max: 1000, default: 500, note: "500 scanned on bamboo" },
            Setting { key: "depth", label: "Depth", min: 1, max: 255, default: 10, note: "10 scanned on bamboo; dwell per dot (assumed)" },
        ],
        // Homes and lights the dot at power-up; dot stays on. 0x06/0x07 ignored on firmware v4.1.6.
        pointer: false,
        frame: true,
    }
}

/// Open the K6 on a serial port.
pub fn open(path: &str) -> laser::Result<Box<dyn laser::Machine>> {
    let port = serialport::new(path, 115_200)
        .timeout(std::time::Duration::from_millis(100))
        .open()
        .map_err(|e| laser::Error::Device(format!("open {path}: {e}")))?;
    Ok(Box::new(Device::new(port)))
}

#[cfg(test)]
mod tests {
    #[test]
    fn accepted_y_limit_is_a_normal_job_boundary() {
        let info = super::info();
        assert_eq!(info.reach_mm.1, 75.0);
        assert!(info.check_fits(laser::Rect { x: 0.0, y: 0.0, w: 2.0, h: 75.0 }).is_ok());
        assert!(info.check_fits(laser::Rect { x: 0.0, y: 0.0, w: 2.0, h: 75.05 }).is_err());
        assert_eq!(super::protocol::centre(0, 0, 40, 40), (87, 20));
    }
}
