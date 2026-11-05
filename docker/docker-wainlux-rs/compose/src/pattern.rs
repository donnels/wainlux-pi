//! Test patterns for the ladder.

use laser::Raster;

pub fn checkboxes(count: u32, columns: u32, box_px: u32, gap_px: u32, line_px: u32) -> Result<Raster, String> {
    if count == 0 || count > 100 || columns == 0 || columns > count || box_px < 3 || gap_px == 0 || line_px == 0 || line_px * 2 >= box_px {
        return Err("checkbox grid needs 1..=100 boxes, valid columns and positive fitting dimensions".into());
    }
    let rows = count.div_ceil(columns);
    let width = columns.checked_mul(box_px).and_then(|value| value.checked_add((columns - 1).checked_mul(gap_px)?)).ok_or("checkbox grid width overflow")?;
    let height = rows.checked_mul(box_px).and_then(|value| value.checked_add((rows - 1).checked_mul(gap_px)?)).ok_or("checkbox grid height overflow")?;
    if width > 10000 || height > 10000 { return Err("checkbox grid exceeds 10000 pixels".into()); }
    let mut raster = Raster::new(width, height);
    for index in 0..count {
        let x = (index % columns) * (box_px + gap_px);
        let y = (index / columns) * (box_px + gap_px);
        for offset in 0..box_px {
            for thickness in 0..line_px {
                raster.set(x + offset, y + thickness, true);
                raster.set(x + offset, y + box_px - 1 - thickness, true);
                raster.set(x + thickness, y + offset, true);
                raster.set(x + box_px - 1 - thickness, y + offset, true);
            }
        }
    }
    Ok(raster)
}

pub fn vertical_strip(height: u32, side: u32, gap_start: u32, gap_length: u32, continuous: bool) -> Result<Raster, String> {
    let gap_end = gap_start.checked_add(gap_length).ok_or("strip gap overflow")?;
    if height > 10000 || side == 0 || side > 10000 || gap_length == 0
        || gap_start < side || height < side || gap_end > height - side
    {
        return Err("vertical strip markers and gap must fit the height".into());
    }
    let mut raster = Raster::new(side, height);
    for row in 0..height {
        if continuous || row < gap_start || row >= gap_end { raster.set(0, row, true); }
    }
    for start in [0, gap_start - side, gap_end, height - side] {
        for row in start..start + side {
            for column in 0..side { raster.set(column, row, true); }
        }
    }
    Ok(raster)
}

pub fn registration(side: u32, tail: u32, lead_rows: u32) -> Result<Raster, String> {
    let width = side.checked_add(tail).ok_or("registration width overflow")?;
    let height = side.checked_add(lead_rows).ok_or("registration height overflow")?;
    if side == 0 || tail == 0 || width > 10000 || height > 10000 {
        return Err("registration dimensions must be positive and at most 10000 pixels".into());
    }
    let mut raster = Raster::new(width, height);
    for row in 0..lead_rows { raster.set(0, row, true); }
    for row in lead_rows..height {
        for column in 0..side { raster.set(column, row, true); }
    }
    for column in side..width { raster.set(column, lead_rows + side / 2, true); }
    Ok(raster)
}

pub fn corner_frame(width: u32, height: u32, side: u32, gap: u32) -> Result<Raster, String> {
    let occupied = side.checked_mul(2).and_then(|value| value.checked_add(gap));
    if side == 0 || width > 10000 || height > 10000
        || occupied.map_or(true, |value| value > width || value > height)
    {
        return Err("corner squares and centre gaps must fit inside the rectangle".into());
    }
    let mut raster = Raster::new(width, height);
    let horizontal_gap = (width - gap) / 2;
    let vertical_gap = (height - gap) / 2;
    for column in 0..width {
        if column < horizontal_gap || column >= horizontal_gap + gap {
            raster.set(column, 0, true);
            raster.set(column, height - 1, true);
        }
    }
    for row in 0..height {
        if row < vertical_gap || row >= vertical_gap + gap {
            raster.set(0, row, true);
            raster.set(width - 1, row, true);
        }
    }
    for row in 0..side {
        for column in 0..side {
            raster.set(column, row, true);
            raster.set(width - side + column, row, true);
            raster.set(column, height - side + row, true);
            raster.set(width - side + column, height - side + row, true);
        }
    }
    Ok(raster)
}

pub fn limit_squares(side: u32, gap: u32, drop: u32) -> Result<Raster, String> {
    let width = side.checked_mul(7).and_then(|value| gap.checked_mul(6).and_then(|space| value.checked_add(space)));
    let height = drop.checked_mul(6).and_then(|value| value.checked_add(side));
    let (width, height) = match (width, height) {
        (Some(width), Some(height)) if side > 0 && drop > 0 && width <= 10000 && height <= 10000 => (width, height),
        _ => return Err("seven-square dimensions must be positive and fit within 10000 pixels".into()),
    };
    let mut raster = Raster::new(width, height);
    for index in 0..7 {
        for row in 0..side {
            for column in 0..side {
                raster.set(index * (side + gap) + column, index * drop + row, true);
            }
        }
    }
    Ok(raster)
}

pub fn limit_stairs(width: u32, rows: u32) -> Result<Raster, String> {
    if rows == 0 || width < rows || width > 10000 || rows > 10000 {
        return Err("stairs need 1..=width rows and dimensions at most 10000 pixels".into());
    }
    let length = width / rows;
    let mut raster = Raster::new(width, rows);
    for row in 0..rows {
        raster.set(0, row, true);
        for column in row * length..(row + 1) * length {
            raster.set(column, row, true);
        }
    }
    Ok(raster)
}

pub fn located_limit_stairs(width: u32, rows: u32, locator_side: u32, gap: u32) -> Result<Raster, String> {
    let stairs = limit_stairs(width, rows)?;
    let prefix = locator_side.checked_add(gap).ok_or("locator dimensions overflow")?;
    let height = prefix.checked_add(rows).ok_or("pattern height overflow")?;
    if locator_side == 0 || locator_side > width || height > 10000 {
        return Err("locator must fit the pattern width and total height must not exceed 10000 pixels".into());
    }
    let mut raster = Raster::new(width, height);
    for row in 0..prefix {
        raster.set(0, row, true);
        if row < locator_side {
            for column in 0..locator_side {
                raster.set(column, row, true);
            }
        }
    }
    for row in 0..rows {
        for column in 0..width {
            raster.set(column, prefix + row, stairs.get(column, row));
        }
    }
    Ok(raster)
}

/// Two `side` squares on a diagonal, the second `gap` right of and below the first's corner.
/// The `gap` rows between them carry no burn data.
pub fn blank_rows(side: u32, gap: u32) -> Raster {
    let n = 2 * side + gap;
    let mut r = Raster::new(n, n);
    for y in 0..side {
        for x in 0..side {
            r.set(x, y, true);
            r.set(x + side + gap, y + side + gap, true);
        }
    }
    r
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sixteen_checkbox_grid_has_sixteen_blank_outlines() {
        let raster = checkboxes(16, 2, 70, 16, 7).unwrap();
        assert_eq!((raster.width, raster.height), (156, 672));
        for index in 0..16 {
            let x=(index%2)*86; let y=(index/2)*86;
            assert!(raster.get(x,y));
            assert!(raster.get(x+69,y+69));
            assert!(!raster.get(x+35,y+35), "box interior stays blank for a pen mark");
        }
        assert!(checkboxes(16,0,70,16,7).is_err());
    }

    #[test]
    fn vertical_comparison_changes_only_the_intended_rows() {
        let a = vertical_strip(1549, 40, 574, 400, false).unwrap();
        let b = vertical_strip(1549, 40, 574, 400, true).unwrap();
        let c = vertical_strip(1500, 40, 574, 400, false).unwrap();
        let mut differences = 0;
        for row in 0..1549 {
            for column in 0..40 {
                if a.get(column, row) != b.get(column, row) {
                    assert_eq!(column, 0);
                    assert!((574..974).contains(&row));
                    differences += 1;
                }
            }
        }
        assert_eq!(differences, 400);
        for row in 0..1460 {
            for column in 0..40 { assert_eq!(a.get(column, row), c.get(column, row)); }
        }
        for start in [0, 534, 974, 1460] {
            assert!(c.get(39, start) && c.get(39, start + 39));
        }
        assert!(vertical_strip(500, 40, 574, 400, false).is_err());
    }

    #[test]
    fn registration_changes_only_the_leading_line() {
        let short = registration(40, 60, 0).unwrap();
        let long = registration(40, 60, 400).unwrap();
        for row in 0..400 {
            for column in 0..100 { assert_eq!(long.get(column, row), column == 0); }
        }
        for row in 0..40 {
            for column in 0..100 { assert_eq!(short.get(column, row), long.get(column, row + 400)); }
        }
        assert!(short.get(99, 20));
        assert!(!short.get(99, 19));
    }

    #[test]
    fn continuous_frame_has_ink_in_every_row() {
        let raster = corner_frame(1600, 1549, 40, 0).unwrap();
        for row in 0..1549 {
            assert!(raster.get(0, row) && raster.get(1599, row));
        }
    }

    #[test]
    fn corner_frame_has_four_squares_and_exact_centre_gaps() {
        let raster = corner_frame(1600, 1549, 40, 400).unwrap();
        for row in 0..1549 {
            for column in 0..1600 {
                let corner = (column < 40 || column >= 1560) && (row < 40 || row >= 1509);
                let horizontal = (row == 0 || row == 1548) && (column < 600 || column >= 1000);
                let vertical = (column == 0 || column == 1599) && (row < 574 || row >= 974);
                assert_eq!(raster.get(column, row), corner || horizontal || vertical, "at {column},{row}");
            }
        }
        assert!(corner_frame(100, 100, 40, 40).is_err());
    }

    #[test]
    fn seven_squares_have_exact_edges_and_no_locator() {
        let raster = limit_squares(40, 20, 2).unwrap();
        assert_eq!((raster.width, raster.height), (400, 52));
        assert_eq!(raster.bits.iter().filter(|pixel| **pixel).count(), 7 * 40 * 40);
        for index in 0..7 {
            let left = index * 60;
            let top = index * 2;
            assert!(raster.get(left, top));
            assert!(raster.get(left + 39, top + 39));
            if top > 0 { assert!(!raster.get(left, top - 1)); }
            if top + 40 < raster.height { assert!(!raster.get(left, top + 40)); }
        }
        assert!(limit_squares(0, 20, 2).is_err());
    }

    #[test]
    fn locator_and_stairs_share_one_raster_and_reference() {
        let raster = located_limit_stairs(400, 20, 40, 40).unwrap();
        assert_eq!((raster.width, raster.height), (400, 100));
        for row in 0..100 {
            assert!(raster.get(0, row));
        }
        assert!(raster.get(39, 39));
        assert!(!raster.get(40, 39));
        assert!(!raster.get(1, 40));
        assert!(raster.get(19, 80));
        assert!(!raster.get(20, 80));
        assert!(raster.get(399, 99));
        assert!(located_limit_stairs(20, 20, 40, 40).is_err());
    }

    #[test]
    fn stairs_have_one_row_steps_and_a_zero_line() {
        let raster = limit_stairs(1600, 120).unwrap();
        for row in 0..120 {
            for column in 0..1600 {
                assert_eq!(raster.get(column, row), column == 0 || (row * 13..(row + 1) * 13).contains(&column));
            }
        }
        assert!(limit_stairs(100, 0).is_err());
        assert!(limit_stairs(100, 101).is_err());
    }

    #[test]
    fn gap_rows_are_empty_and_squares_full() {
        let r = blank_rows(100, 100);
        assert_eq!((r.width, r.height), (300, 300));
        assert!(r.get(0, 0) && r.get(99, 99) && r.get(200, 200) && r.get(299, 299));
        for y in 100..200 {
            assert!((0..300).all(|x| !r.get(x, y)), "row {y} must be blank");
        }
    }

    #[test]
    fn gap_holds_whole_all_zero_k6_chunks() {
        // 300 px rows pack to 38 bytes. Blank rows 100..200 = bytes 3800..7600.
        let r = blank_rows(100, 100);
        let row_bytes = (r.width as usize + 7) / 8;
        let (start, end) = (100 * row_bytes, 200 * row_bytes);
        let whole = (0..end).step_by(1900).filter(|c| *c >= start && c + 1900 <= end).count();
        assert_eq!(whole, 2, "chunks 3800..5700 and 5700..7600 carry only zeros");
    }
}
