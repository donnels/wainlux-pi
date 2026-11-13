//! 1-bit image. true = burn. Row-major, top-left origin.

#[derive(Clone, Debug, PartialEq)]
pub struct Raster {
    pub width: u32,
    pub height: u32,
    pub bits: Vec<bool>,
}

impl Raster {
    pub fn new(width: u32, height: u32) -> Self {
        Raster { width, height, bits: vec![false; width as usize * height as usize] }
    }

    pub fn get(&self, x: u32, y: u32) -> bool {
        self.bits[y as usize * self.width as usize + x as usize]
    }

    pub fn set(&mut self, x: u32, y: u32, on: bool) {
        self.bits[y as usize * self.width as usize + x as usize] = on;
    }

    pub fn fill_rect(&mut self, x: u32, y: u32, width: u32, height: u32) {
        for row in y..y + height { for column in x..x + width { self.set(column, row, true); } }
    }

    /// Filled rectangle, all burn.
    pub fn filled(width: u32, height: u32) -> Self {
        Raster { width, height, bits: vec![true; width as usize * height as usize] }
    }

    /// Filled square, all burn.
    pub fn square(side: u32) -> Self {
        Raster::filled(side, side)
    }

    /// Scale an n x n module matrix (true = dark) by `px` pixels per module.
    pub fn from_modules(modules: &[bool], n: usize, px: u32) -> Self {
        let side = n as u32 * px;
        let mut r = Raster::new(side, side);
        for y in 0..side {
            for x in 0..side {
                r.set(x, y, modules[(y / px) as usize * n + (x / px) as usize]);
            }
        }
        r
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scales_modules() {
        let r = Raster::from_modules(&[true, false, false, true], 2, 2);
        assert_eq!(r.width, 4);
        assert!(r.get(1, 1) && !r.get(2, 1) && r.get(3, 3));
    }
}
