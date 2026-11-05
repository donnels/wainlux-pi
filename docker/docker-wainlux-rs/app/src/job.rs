//! A job: a piece, an optional guide on the backing, items on the piece.
//! Pure: request and material list in, stages out. Each stage gets its own goal and verdict.

use crate::ladder::{describe, done_text};
use crate::materials::{Material, Role};
use compose::place::{Placement, GUIDE_GAP, GUIDE_T, LEFT_MARGIN};
use laser::{Info, Machine, Progress, Raster, Rect, Values};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Where {
    /// Card 1 mm from the left: U guide (ADR-025 B).
    #[default]
    Left,
    /// Card centred: biggest etch, top and bottom guide only (ADR-025 A).
    Centred,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Content {
    Image { asset_id: String },
    Checkboxes { count: u32, columns: u32, box_mm: f32, gap_mm: f32, line_mm: f32 },
    Text { text: String, font_size_mm: f32, #[serde(default)] font: Option<String> },
    Qr {
        text: String,
    },
    Wifi {
        ssid: String,
        #[serde(default)]
        password: String,
        #[serde(default = "wpa")]
        security: String,
        #[serde(default)]
        hidden: bool,
    },
}

fn wpa() -> String {
    "WPA".into()
}


fn yes() -> bool {
    true
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Item {
    #[serde(default)]
    pub side: Side,
    #[serde(default)]
    pub id: Option<String>,
    pub content: Content,
    /// Top-left on the piece, mm. Missing: centred in the etch area.
    pub x_mm: Option<f32>,
    pub y_mm: Option<f32>,
    /// Side of the square item, mm. Missing: the largest square that fits.
    pub size_mm: Option<f32>,
    pub width_mm: Option<f32>,
    pub height_mm: Option<f32>,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Side { #[default] Front, Back }

impl Side {
    pub fn name(self) -> &'static str { match self { Self::Front => "front", Self::Back => "back" } }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct JobReq {
    #[serde(default)]
    pub template: Option<String>,
    pub piece: String,
    pub backing: Option<String>,
    #[serde(default)]
    pub placement: Where,
    #[serde(default = "yes")]
    pub guide: bool,
    pub items: Vec<Item>,
    /// On top of the piece material's settings.
    #[serde(default)]
    pub values: Values,
}

impl JobReq {
    /// Safe to log: no passwords, no QR text (it may hold recovery codes).
    pub fn redacted(&self) -> JobReq {
        let mut j = self.clone();
        for it in &mut j.items {
            match &mut it.content {
                Content::Qr { text } | Content::Text { text, .. } => *text = format!("<{} chars>", text.chars().count()),
                Content::Wifi { ssid, password, .. } => {
                    *ssid = "<hidden>".into();
                    *password = "<hidden>".into();
                }
                Content::Image { .. } => {},
                Content::Checkboxes { .. } => {},
            }
        }
        j
    }
}

#[derive(Debug)]
pub struct Burn {
    pub raster: Raster,
    pub x_mm: f32,
    pub y_mm: f32,
}

#[derive(Debug)]
pub struct Stage {
    pub side: Option<Side>,
    pub name: String,
    pub goal: String,
    pub values: Values,
    pub burns: Vec<Burn>,
}

fn find<'a>(list: &'a [Material], id: &str, role: Role) -> Result<&'a Material, String> {
    let m = list.iter().find(|m| m.id == id).ok_or_else(|| format!("no material '{id}'"))?;
    if m.role != role {
        return Err(format!("material '{id}' is a {:?}, not a {role:?}", m.role));
    }
    Ok(m)
}

pub fn placement(job: &JobReq, list: &[Material], info: &Info) -> Result<Placement, String> {
    let piece = find(list, &job.piece, Role::Piece)?;
    let (width, height) = (piece.size_mm.0 as f32, piece.size_mm.1 as f32);
    Ok(match job.placement {
        Where::Left => Placement::left(info.reach_mm, width, height, LEFT_MARGIN),
        Where::Centred => Placement::centred(info.reach_mm, width, height),
    })
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CardFile { card: CardMaterial, wifi: Option<WifiFile>, content: CardContent }

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CardMaterial { material: String, backing: Option<String>, #[serde(default)] placement: Where }

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WifiFile {
    ssid: String,
    #[serde(default)] password: String,
    #[serde(default = "wpa")] security: String,
    #[serde(default)] hidden: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CardContent {
    qr_kind: String,
    #[serde(default)] url_or_text: String,
    #[serde(default)] text: String,
    font_size_mm: f32,
}

pub fn from_toml(source: &str, template: &crate::templates::Template, list: &[Material], info: &Info) -> Result<JobReq, String> {
    let file: CardFile = toml::from_str(source).map_err(|error: toml::de::Error| format!("invalid card TOML: {}", error.message()))?;
    let content = match file.content.qr_kind.as_str() {
        "wifi" => {
            let wifi = file.wifi.ok_or("WiFi QR needs a [wifi] section")?;
            Content::Wifi { ssid: wifi.ssid, password: wifi.password, security: wifi.security, hidden: wifi.hidden }
        }
        "qr" | "url" => Content::Qr { text: file.content.url_or_text },
        _ => return Err("qr_kind must be wifi, qr or url".into()),
    };
    let mut items = vec![Item {
        side: Side::Front,
        id: None,
        content, x_mm: None, y_mm: None,
        size_mm: None, width_mm: None, height_mm: None,
    }];
    if !file.content.text.trim().is_empty() {
        items.push(Item {
            side: Side::Front,
            id: None,
            content: Content::Text { text: file.content.text, font_size_mm: file.content.font_size_mm, font:None },
            x_mm: None, y_mm: None,
            size_mm: None, width_mm: None, height_mm: None,
        });
    }
    let job = JobReq { template: Some(template.id.clone()), piece: file.card.material, backing: file.card.backing, placement: file.card.placement, guide: true, items, values: Values::new() };
    let job = template.compose(&job, &template.values(&job)?)?;
    stages(&job, list, info)?;
    Ok(job)
}

pub fn stages(job: &JobReq, list: &[Material], i: &Info) -> Result<Vec<Stage>, String> {
    if !job.items.iter().any(|item| item.side == Side::Back) { return side_stages(job, list, i); }
    let mut stages = Vec::new();
    for side in [Side::Front, Side::Back] {
        let mut surface = job.clone();
        surface.items.retain(|item| item.side == side);
        surface.guide = side == Side::Front && job.guide;
        if surface.items.is_empty() && !surface.guide { continue; }
        for mut stage in side_stages(&surface, list, i)? {
            if stage.name != "guide" {
                stage.name = side.name().into(); stage.side = Some(side);
                stage.goal = format!("{} side: {}", side.name(), stage.goal);
            }
            stages.push(stage);
        }
    }
    Ok(stages)
}

fn side_stages(job: &JobReq, list: &[Material], i: &Info) -> Result<Vec<Stage>, String> {
    if job.items.is_empty() && !job.guide {
        return Err("a job needs an item or a guide".into());
    }
    let piece = find(list, &job.piece, Role::Piece)?;
    let place = placement(job, list, i)?;
    let etch = place.etch_area().ok_or("the piece is outside the reach")?;
    let mut values = piece.values(i)?;
    values.extend(job.values.clone());
    let values = i.resolve(&values).map_err(|e| e.to_string())?;
    let mut out = Vec::new();

    if job.guide {
        let id = job.backing.as_deref().ok_or("a guide needs a backing material")?;
        let backing = find(list, id, Role::Backing)?;
        let strips = place.guide(GUIDE_T, GUIDE_GAP);
        if !strips.is_empty() {
            let bv = backing.values(i)?;
            let shape = match strips.len() {
                3 => "a connected U guide".to_string(),
                4 => "a connected outline guide".to_string(),
                count => format!("{count} guide strip(s)"),
            };
            out.push(Stage {
                side: None,
                name: "guide".into(),
                goal: format!(
                    "Burns {shape} on {}, {GUIDE_GAP} mm outside where {} will lie. {}. Markings visible with the card in place.",
                    backing.name,
                    piece.name,
                    describe(i, &bv)
                ),
                burns: vec![merge(&strips.iter().map(|s| Burn { raster: Raster::filled(i.px(s.w), i.px(s.h)), x_mm: s.x, y_mm: s.y }).collect::<Vec<_>>(), i)?],
                values: bv,
            });
        }
    }

    for (n, item) in job.items.iter().enumerate() {
        let is_text = matches!(&item.content, Content::Text { .. } | Content::Image { .. } | Content::Checkboxes { .. });
        let checklist = if let Content::Checkboxes { count, columns, box_mm, gap_mm, line_mm } = &item.content {
            Some(compose::pattern::checkboxes(*count,*columns,i.px(*box_mm),i.px(*gap_mm),i.px(*line_mm))?)
        } else { None };
        let intrinsic = checklist.as_ref().map(|raster| (i.mm(raster.width),i.mm(raster.height)));
        let width = item.width_mm.or(item.size_mm).or(intrinsic.map(|size|size.0)).unwrap_or(if is_text { etch.w } else { etch.w.min(etch.h) });
        let height = item.height_mm.or(item.size_mm).or(intrinsic.map(|size|size.1)).unwrap_or(if is_text { etch.h } else { width });
        if !width.is_finite() || !height.is_finite() || width <= 0.0 || height <= 0.0 {
            return Err(format!("item {n}: dimensions must be finite and positive"));
        }
        if !is_text && (width - height).abs() > 0.001 {
            return Err(format!("item {n}: QR boxes must be square"));
        }
        let x = item.x_mm.unwrap_or(etch.x - place.piece.x + (etch.w - width) / 2.0);
        let y = item.y_mm.unwrap_or(etch.y - place.piece.y + (etch.h - height) / 2.0);
        if !x.is_finite() || !y.is_finite() { return Err(format!("item {n}: coordinates must be finite")); }
        let on_bed = place.item_on_bed(Rect { x, y, w: width, h: height }).map_err(|error| format!("item {n}: {error}"))?;
        let (text, what, proof) = match &item.content {
            Content::Image { .. } => (String::new(), "prepared image".into(), "Image shape and orientation match the preview."),
            Content::Checkboxes { count, .. } => (String::new(), format!("{count} blank checkboxes"), "Boxes remain open for manual checkmarks."),
            Content::Text { text, .. } => (text.clone(), "raster text".to_string(), "All lines readable, no clipping."),
            Content::Qr { text } => (text.clone(), "QR".to_string(), "Phone scans it."),
            Content::Wifi { ssid, password, security, hidden } => (
                compose::wifi::text(ssid, password, security, *hidden)?,
                "WiFi QR".to_string(),
                "Phone joins the network.",
            ),
        };
        let raster = match &item.content {
            Content::Image { asset_id } => crate::assets::raster(asset_id, i.px(width), i.px(height)),
            Content::Checkboxes { .. } => Ok(checklist.unwrap()),
            Content::Text { font_size_mm, font, .. } => compose::text::raster_with_font(&text, font.as_deref().unwrap_or(compose::text::DEFAULT_FONT), *font_size_mm * i.px_per_mm, i.px(width), i.px(height)),
            _ => compose::qr::qr(&text, i.px(width)),
        }.map_err(|error| format!("item {n}: {error}"))?;
        let pad_x = (width - i.mm(raster.width)) / 2.0;
        let pad_y = (height - i.mm(raster.height)) / 2.0;
        out.push(Stage {
            side: Some(Side::Front),
            name: format!("item {n}"),
            goal: format!(
                "Burns a {:.1} mm {what} on {}, top-left at ({x:.1}, {y:.1}) mm on the card. {}. {proof}",
                i.mm(raster.width),
                piece.name,
                describe(i, &values)
            ),
            burns: vec![Burn { raster, x_mm: on_bed.x + pad_x, y_mm: on_bed.y + pad_y }],
            values: values.clone(),
        });
    }
    if job.items.len() > 1 {
        let content_start = out.iter().position(|stage| stage.name.starts_with("item ")).unwrap();
        let content = out.split_off(content_start);
        let burns: Vec<Burn> = content.iter().flat_map(|stage| stage.burns.iter()).map(|burn| Burn { raster: burn.raster.clone(), x_mm: burn.x_mm, y_mm: burn.y_mm }).collect();
        out.push(Stage {
            side: Some(Side::Front),
            name: "content".into(),
            goal: content.iter().map(|stage| stage.goal.as_str()).collect::<Vec<_>>().join(" "),
            values,
            burns: vec![merge(&burns, i)?],
        });
    }
    if out.is_empty() { return Err("job has no reachable guide and no content".into()); }
    Ok(out)
}

fn merge(burns: &[Burn], info: &Info) -> Result<Burn, String> {
    let left = burns.iter().map(|burn| info.px(burn.x_mm)).min().ok_or("nothing to compose")?;
    let top = burns.iter().map(|burn| info.px(burn.y_mm)).min().unwrap();
    let right = burns.iter().map(|burn| info.px(burn.x_mm) + burn.raster.width).max().unwrap();
    let bottom = burns.iter().map(|burn| info.px(burn.y_mm) + burn.raster.height).max().unwrap();
    let mut raster = Raster::new(right - left, bottom - top);
    for burn in burns {
        let offset_x = info.px(burn.x_mm) - left;
        let offset_y = info.px(burn.y_mm) - top;
        for row in 0..burn.raster.height {
            for column in 0..burn.raster.width {
                if burn.raster.get(column, row) { raster.set(offset_x + column, offset_y + row, true); }
            }
        }
    }
    Ok(Burn { raster, x_mm: info.mm(left), y_mm: info.mm(top) })
}

/// Runs every burn of one stage. Returns what the software saw.
pub fn run(m: &mut dyn Machine, s: &Stage, progress: &mut dyn FnMut(Progress)) -> Result<String, String> {
    m.home().map_err(|error| error.to_string())?;
    let t = std::time::Instant::now();
    let mut seen = Vec::new();
    for b in &s.burns {
        let job = laser::Job { raster: &b.raster, x_mm: b.x_mm, y_mm: b.y_mm, values: &s.values };
        let done = m.burn(&job, progress).map_err(|e| e.to_string())?;
        seen.push(done_text(&done));
    }
    Ok(format!("{}: {} burn(s) in {:.0} s: {}", s.name, s.burns.len(), t.elapsed().as_secs_f32(), seen.join("; ")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::materials::parse;
    use laser::sim::Sim;

    fn list() -> Vec<Material> {
        parse(include_str!("../../materials.toml")).unwrap()
    }

    fn wifi_job() -> JobReq {
        serde_json::from_str(
            r#"{"piece":"bamboo-ntag215-light","backing":"backing-first",
                "items":[{"content":{"kind":"wifi","ssid":"Guest","password":"s3cret"}}]}"#,
        )
        .unwrap()
    }

    #[test]
    fn wifi_card_is_guide_then_qr_inside_the_etch_area() {
        let i = k6::info();
        let s = stages(&wifi_job(), &list(), &i).unwrap();
        assert_eq!(s.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["guide", "item 0"]);
        assert_eq!(s[0].burns.len(), 1, "U guide is one raster");
        assert_eq!((s[0].values["power"], s[1].values["power"]), (500, 1000), "backing vs card settings");
        let b = &s[1].burns[0];
        let (w, h) = (i.mm(b.raster.width), i.mm(b.raster.height));
        let etch = placement(&wifi_job(), &list(), &i).unwrap().etch_area().unwrap();
        assert!(b.x_mm >= etch.x && b.y_mm >= etch.y);
        assert!(b.x_mm + w <= etch.x + etch.w + 0.001 && b.y_mm + h <= etch.y + etch.h + 0.001);
        assert!(!s[1].goal.contains("s3cret"));
    }

    #[test]
    fn guide_off_or_centred_changes_the_stages() {
        let i = k6::info();
        let mut j = wifi_job();
        j.guide = false;
        j.backing = None;
        assert_eq!(stages(&j, &list(), &i).unwrap().len(), 1);
        let mut j = wifi_job();
        j.placement = Where::Centred;
        assert_eq!(stages(&j, &list(), &i).unwrap()[0].burns.len(), 1, "top and bottom in one raster");
    }

    #[test]
    fn left_guide_is_a_connected_u_in_one_k6_band() {
        let stages = stages(&wifi_job(), &list(), &k6::info()).unwrap();
        assert!(stages[0].goal.contains("connected U guide"));
        let raster = &stages[0].burns[0].raster;
        assert!((0..raster.width).all(|column| raster.get(column, 0)));
        assert!((0..raster.width).all(|column| raster.get(column, raster.height - 1)));
        assert!((0..raster.height).all(|row| raster.get(0, row)));
        assert!(!raster.get(raster.width - 1, raster.height / 2), "right side stays open");
        assert_eq!(k6::protocol::bands(raster).len(), 1);
        assert!(!k6::protocol::has_zero_chunk(&k6::protocol::pack(raster)));
    }

    #[test]
    fn wrong_requests_are_refused() {
        let i = k6::info();
        let l = list();
        let mut j = wifi_job();
        j.backing = None;
        assert!(stages(&j, &l, &i).unwrap_err().contains("backing"));
        let mut j = wifi_job();
        j.piece = "backing-first".into();
        assert!(stages(&j, &l, &i).unwrap_err().contains("not a Piece"));
        let mut j = wifi_job();
        j.items[0].x_mm = Some(0.0);
        j.items[0].size_mm = Some(20.0);
        assert!(stages(&j, &l, &i).unwrap_err().contains("etch area"));
        let mut j = wifi_job();
        j.items.clear();
        assert_eq!(stages(&j, &l, &i).unwrap()[0].name, "guide");
    }

    #[test]
    fn redaction_hides_secrets() {
        let mut j = wifi_job();
        j.items.push(Item { side: Side::Front, id: None, content: Content::Qr { text: "code-1234".into() }, x_mm: None, y_mm: None, size_mm: None, width_mm: None, height_mm: None });
        let s = serde_json::to_string(&j.redacted()).unwrap();
        assert!(!s.contains("s3cret") && !s.contains("code-1234") && !s.contains("Guest"));
    }

    #[test]
    fn stages_run_on_the_simulator() {
        let i = laser::sim::info();
        let mut m = Sim::default();
        for s in stages(&wifi_job(), &list(), &i).unwrap() {
            run(&mut m, &s, &mut |_| {}).unwrap();
        }
        assert_eq!(m.calls.iter().filter(|c| c.starts_with("burn")).count(), 2);
    }
}
