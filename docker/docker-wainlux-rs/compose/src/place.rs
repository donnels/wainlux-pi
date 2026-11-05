//! Where the piece sits on the bed, where its guide goes, how much of it we can etch.
//! Millimetres, top-left origin. Nothing is clipped: what does not fit is left out.
//!
//! Placement is always set. The guide is optional; its shape follows from the fit:
//! small piece -> full outline, card wider than the reach -> two or three sides, huge piece -> none.

use laser::Rect;

/// Guide line thickness, mm.
pub const GUIDE_T: f32 = 0.5;
/// Gap between card edge and guide, mm. Keeps the mark visible with the card in place.
pub const GUIDE_GAP: f32 = 0.2;
/// Default card distance from the left edge, mm: room for gap and guide.
pub const LEFT_MARGIN: f32 = 1.0;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Placement {
    pub reach: (f32, f32),
    /// The piece on the bed. May overhang the reach.
    pub piece: Rect,
}

impl Placement {
    /// Piece centred on the reach: the biggest centred etch, the fewest guide sides.
    pub fn centred(reach: (f32, f32), w: f32, h: f32) -> Self {
        Placement { reach, piece: Rect { x: (reach.0 - w) / 2.0, y: (reach.1 - h) / 2.0, w, h } }
    }

    /// Piece `margin` from the left edge: room for a left guide, smaller etch. The default (ADR-025 B).
    pub fn left(reach: (f32, f32), w: f32, h: f32, margin: f32) -> Self {
        Placement { reach, piece: Rect { x: margin, y: (reach.1 - h) / 2.0, w, h } }
    }

    /// Guide strips `t` mm thick, `gap` mm outside the piece. Only sides that fit wholly across.
    pub fn guide(&self, t: f32, gap: f32) -> Vec<Rect> {
        let (rw, rh) = self.reach;
        let p = self.piece;
        let span_x = ((p.x - gap - t).max(0.0), (p.x + p.w + gap + t).min(rw));
        let span_y = ((p.y - gap - t).max(0.0), (p.y + p.h + gap + t).min(rh));
        let (top, bottom) = (p.y - gap - t, p.y + p.h + gap);
        let (left, right) = (p.x - gap - t, p.x + p.w + gap);
        let mut out = Vec::new();
        if span_x.1 > span_x.0 {
            if top >= 0.0 && top + t <= rh {
                out.push(Rect { x: span_x.0, y: top, w: span_x.1 - span_x.0, h: t });
            }
            if bottom >= 0.0 && bottom + t <= rh {
                out.push(Rect { x: span_x.0, y: bottom, w: span_x.1 - span_x.0, h: t });
            }
        }
        if span_y.1 > span_y.0 {
            if left >= 0.0 && left + t <= rw {
                out.push(Rect { x: left, y: span_y.0, w: t, h: span_y.1 - span_y.0 });
            }
            if right >= 0.0 && right + t <= rw {
                out.push(Rect { x: right, y: span_y.0, w: t, h: span_y.1 - span_y.0 });
            }
        }
        out
    }

    /// Largest area centred on the piece, inside both piece and reach.
    pub fn etch_area(&self) -> Option<Rect> {
        let (rw, rh) = self.reach;
        let p = self.piece;
        let (cx, cy) = (p.x + p.w / 2.0, p.y + p.h / 2.0);
        let hw = (p.w / 2.0).min(cx).min(rw - cx);
        let hh = (p.h / 2.0).min(cy).min(rh - cy);
        (hw > 0.0 && hh > 0.0).then(|| Rect { x: cx - hw, y: cy - hh, w: 2.0 * hw, h: 2.0 * hh })
    }

    /// Item given in the card frame (card top-left = 0, 0), returned in the bed frame.
    /// Refused if any part leaves the etch area.
    pub fn item_on_bed(&self, item: Rect) -> Result<Rect, String> {
        let bed = Rect { x: self.piece.x + item.x, y: self.piece.y + item.y, ..item };
        let a = self.etch_area().ok_or("piece is outside the reach")?;
        let inside = bed.x >= a.x - EPS
            && bed.y >= a.y - EPS
            && bed.x + bed.w <= a.x + a.w + EPS
            && bed.y + bed.h <= a.y + a.h + EPS;
        inside.then_some(bed).ok_or_else(|| {
            format!(
                "item {:.1}x{:.1} mm at ({:.1}, {:.1}) on the card leaves the etch area",
                item.w, item.h, item.x, item.y
            )
        })
    }
}

/// Rounding slack for millimetre sums.
const EPS: f32 = 0.001;

#[cfg(test)]
mod tests {
    use super::*;

    const K6: (f32, f32) = (80.0, 75.0);
    const CARD: (f32, f32) = (85.45, 53.72);

    fn close(a: f32, b: f32) -> bool {
        (a - b).abs() < 0.01
    }

    #[test]
    fn centred_card_gets_top_and_bottom_and_full_width() {
        let p = Placement::centred(K6, CARD.0, CARD.1);
        assert_eq!(p.guide(GUIDE_T, GUIDE_GAP).len(), 2);
        let a = p.etch_area().unwrap();
        assert!(close(a.w, 80.0) && close(a.h, 53.72));
    }

    #[test]
    fn card_on_the_left_gets_a_u_and_a_smaller_etch() {
        let p = Placement::left(K6, CARD.0, CARD.1, LEFT_MARGIN);
        let strips = p.guide(GUIDE_T, GUIDE_GAP);
        assert_eq!(strips.len(), 3);
        let (top, bottom, left) = (strips[0], strips[1], strips[2]);
        assert!(close(top.x, left.x) && close(bottom.x, left.x));
        assert!(close(top.y, left.y));
        assert!(close(bottom.y + bottom.h, left.y + left.h));
        let a = p.etch_area().unwrap();
        assert!(close(a.w, 72.55));
        assert!(close(a.x + a.w / 2.0, 1.0 + 42.725));
    }

    #[test]
    fn small_piece_gets_full_outline() {
        let p = Placement::centred(K6, 30.0, 30.0);
        let strips = p.guide(GUIDE_T, GUIDE_GAP);
        assert_eq!(strips.len(), 4);
        let (top, bottom, left, right) = (strips[0], strips[1], strips[2], strips[3]);
        assert!(close(top.x, left.x) && close(bottom.x, left.x));
        assert!(close(top.x + top.w, right.x + right.w));
        assert!(close(bottom.x + bottom.w, right.x + right.w));
        assert!(close(top.y, left.y) && close(top.y, right.y));
        assert!(close(bottom.y + bottom.h, left.y + left.h));
        assert!(close(bottom.y + bottom.h, right.y + right.h));
        assert_eq!(p.etch_area().unwrap().w, 30.0);
    }

    #[test]
    fn huge_piece_gets_no_guide_but_still_an_etch() {
        let p = Placement::centred(K6, 100.0, 100.0);
        assert!(p.guide(GUIDE_T, GUIDE_GAP).is_empty());
        let a = p.etch_area().unwrap();
        assert!(close(a.w, 80.0) && close(a.h, 75.0));
    }

    #[test]
    fn guide_strips_stay_inside_reach() {
        for p in [Placement::centred(K6, CARD.0, CARD.1), Placement::left(K6, CARD.0, CARD.1, LEFT_MARGIN)] {
            for g in p.guide(GUIDE_T, GUIDE_GAP) {
                assert!(g.x >= 0.0 && g.y >= 0.0 && g.x + g.w <= 80.0 && g.y + g.h <= 75.0, "{g:?}");
            }
        }
    }

    #[test]
    fn guide_never_touches_the_card() {
        let p = Placement::left(K6, CARD.0, CARD.1, LEFT_MARGIN);
        let c = p.piece;
        for g in p.guide(GUIDE_T, GUIDE_GAP) {
            let apart = g.x + g.w <= c.x - GUIDE_GAP + 0.001
                || g.y + g.h <= c.y - GUIDE_GAP + 0.001
                || g.y >= c.y + c.h + GUIDE_GAP - 0.001;
            assert!(apart, "{g:?}");
        }
    }

    #[test]
    fn items_move_with_the_card() {
        let p = Placement::left(K6, CARD.0, CARD.1, 1.0);
        let qr = p.item_on_bed(Rect { x: 10.0, y: 7.0, w: 40.0, h: 40.0 }).unwrap();
        assert!(close(qr.x, 11.0) && close(qr.y, 17.64));
        let text = p.item_on_bed(Rect { x: 52.0, y: 18.0, w: 22.0, h: 18.0 }).unwrap();
        assert!(close(text.x, 53.0) && close(text.y, 28.64));
    }

    #[test]
    fn item_outside_the_etch_area_is_refused() {
        let p = Placement::left(K6, CARD.0, CARD.1, 1.0);
        assert!(p.item_on_bed(Rect { x: 2.0, y: 7.0, w: 20.0, h: 20.0 }).is_err());
        assert!(p.item_on_bed(Rect { x: 70.0, y: 7.0, w: 10.0, h: 10.0 }).is_err());
    }
}
