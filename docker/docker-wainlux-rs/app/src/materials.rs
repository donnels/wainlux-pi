//! Material list: what we etch on and what lies under it. Loaded from a TOML file.
//! Burn settings are kept per machine model, so the list works for any driver.

use crate::ladder::Params;
use laser::{Info, Values};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Piece,
    Backing,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Material {
    pub id: String,
    pub name: String,
    pub role: Role,
    pub size_mm: (f64, f64),
    #[serde(default)]
    pub note: String,
    /// Machine model -> settings.
    #[serde(default)]
    pub settings: BTreeMap<String, Values>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct File {
    material: Vec<Material>,
}

pub fn parse(text: &str) -> Result<Vec<Material>, String> {
    let list = toml::from_str::<File>(text).map_err(|e| e.to_string())?.material;
    for (i, m) in list.iter().enumerate() {
        if m.id.is_empty() || m.name.is_empty() {
            return Err(format!("material {i}: id and name are required"));
        }
        if list[..i].iter().any(|o| o.id == m.id) {
            return Err(format!("material id '{}' is used twice", m.id));
        }
        if m.size_mm.0 <= 0.0 || m.size_mm.1 <= 0.0 {
            return Err(format!("material '{}': size must be positive", m.id));
        }
    }
    Ok(list)
}

pub fn load(path: &str) -> Result<Vec<Material>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?;
    parse(&text).map_err(|e| format!("{path}: {e}"))
}

impl Material {
    /// Settings for this machine, checked against what its driver accepts.
    pub fn values(&self, i: &Info) -> Result<Values, String> {
        i.resolve(&self.settings.get(i.model).cloned().unwrap_or_default()).map_err(|e| format!("{}: {e}", self.id))
    }
}

/// Material settings first, then anything the caller set explicitly on top.
pub fn apply(p: &mut Params, id: &str, list: &[Material], i: &Info) -> Result<(), String> {
    let m = list.iter().find(|m| m.id == id).ok_or_else(|| format!("no material '{id}'"))?;
    let mut v = m.values(i)?;
    v.extend(p.values.clone());
    p.values = v;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const SHIPPED: &str = include_str!("../../materials.toml");

    #[test]
    fn shipped_list_parses_and_fits_the_k6() {
        let list = parse(SHIPPED).unwrap();
        let card = list.iter().find(|m| m.id == "bamboo-ntag215-light").unwrap();
        assert_eq!(card.role, Role::Piece);
        assert_eq!(card.size_mm, (85.45, 53.72));
        let v = card.values(&k6::info()).unwrap();
        assert_eq!((v["power"], v["depth"]), (1000, 10));
        assert!(list.iter().any(|m| m.role == Role::Backing));
    }

    #[test]
    fn unknown_machine_gets_driver_defaults() {
        let list = parse(SHIPPED).unwrap();
        let v = list[0].values(&laser::sim::info()).unwrap();
        assert_eq!(v["power"], 50);
    }

    #[test]
    fn explicit_values_win_over_material() {
        let list = parse(SHIPPED).unwrap();
        let mut p = Params::default();
        p.values.insert("depth".into(), 5);
        apply(&mut p, "bamboo-ntag215-light", &list, &k6::info()).unwrap();
        assert_eq!((p.values["power"], p.values["depth"]), (1000, 5));
    }

    #[test]
    fn bad_lists_are_refused() {
        let one = "[[material]]\nid='a'\nname='A'\nrole='piece'\nsize_mm=[10,10]\n";
        assert!(parse(&format!("{one}{one}")).unwrap_err().contains("twice"));
        assert!(parse(&one.replace("[10,10]", "[0,10]")).is_err());
        assert!(parse(&format!("{one}colour='red'\n")).is_err());
        assert!(parse(&one.replace("piece", "plate")).is_err());
    }

    #[test]
    fn setting_out_of_range_is_refused() {
        let bad = SHIPPED.replace("power = 1000", "power = 5000");
        let list = parse(&bad).unwrap();
        assert!(list[0].values(&k6::info()).is_err());
    }
}
