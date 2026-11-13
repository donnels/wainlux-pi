use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::path::Path;
use std::collections::BTreeMap;
use serde_json::{json, Value};
use crate::job::JobReq;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Display {
    #[serde(default, skip_deserializing)]
    pub masked_fields: Vec<String>,
    pub card_content_visible: bool,
    pub markings_visible: bool,
}

impl Default for Display {
    fn default() -> Self {
        Self {
            masked_fields: Vec::new(),
            card_content_visible: false,
            markings_visible: true,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Template {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub audio_recording_enabled: bool,
    pub fields: Vec<Field>,
    pub elements: Vec<Element>,
    #[serde(default)]
    pub exclusive_groups: Vec<Vec<String>>,
    pub display: Display,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Field {
    pub id: String,
    pub label: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub default: Value,
    pub editable: bool,
    pub revealable: bool,
    pub masked: bool,
    #[serde(default)]
    pub file_input: bool,
    #[serde(default)]
    pub options: Vec<Value>,
    pub min: Option<f64>,
    pub max: Option<f64>,
    pub step: Option<f64>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Element {
    pub id: String,
    pub side: crate::job::Side,
    pub kind: String,
    pub enabled_by: Option<String>,
    pub bindings: BTreeMap<String, String>,
}

impl Template {
    pub fn defaults(&self) -> BTreeMap<String, Value> {
        self.fields.iter().map(|field| (field.id.clone(), field.default.clone())).collect()
    }

    pub fn values(&self, job: &JobReq) -> Result<BTreeMap<String, Value>, String> {
        let mut values = self.defaults();
        let mut assigned = BTreeSet::new();
        for item in &job.items {
            let id = item.id.as_ref().ok_or("submitted item needs a declared element id")?;
            let element = self.elements.iter().find(|element| &element.id == id).ok_or("undeclared submitted element")?;
            if item.side != element.side { return Err("submitted element is on the wrong side".into()); }
            let item = serde_json::to_value(item).map_err(|error| error.to_string())?;
            if item["content"]["kind"] != element.kind { return Err("submitted element kind does not match template".into()); }
            for (target, field) in &element.bindings {
                let pointer = format!("/{}", target.replace('.', "/"));
                if let Some(value) = item.pointer(&pointer).filter(|value| !value.is_null()) {
                    if !assigned.insert(field.clone()) && values[field] != *value { return Err("conflicting values bound to one field".into()); }
                    values.insert(field.clone(), value.clone());
                }
            }
            if let Some(enabled) = &element.enabled_by { values.insert(enabled.clone(), json!(true)); }
        }
        self.validate_values(&values)?;
        Ok(values)
    }

    fn validate_values(&self, values: &BTreeMap<String, Value>) -> Result<(), String> {
        for field in &self.fields {
            let value = values.get(&field.id).ok_or("missing template field")?;
            let valid = match field.kind.as_str() {
                "text" | "multiline" => value.is_string(),
                "checkbox" => value.is_boolean(),
                "select" => field.options.contains(value),
                "number" => value.as_f64().map(|number| number.is_finite() && field.min.map(|min| number >= min).unwrap_or(true) && field.max.map(|max| number <= max).unwrap_or(true)).unwrap_or(false),
                _ => false,
            };
            if !valid { return Err(format!("invalid field '{}'", field.id)); }
        }
        for group in &self.exclusive_groups {
            if group.iter().filter(|id| values.get(*id) == Some(&json!(true))).count() > 1 { return Err("choose only one enabled option in an exclusive group".into()); }
        }
        Ok(())
    }

    pub fn edit(&self, values: &mut BTreeMap<String, Value>, changes: &BTreeMap<String, Value>) -> Result<(), String> {
        for (id, value) in changes {
            let field = self.fields.iter().find(|field| &field.id == id).ok_or("undeclared field")?;
            if !field.editable { return Err(format!("field '{id}' is not editable")); }
            values.insert(id.clone(), value.clone());
        }
        self.validate_values(values)
    }

    pub fn compose(&self, base: &JobReq, values: &BTreeMap<String, Value>) -> Result<JobReq, String> {
        self.validate_values(values)?;
        let mut job = base.clone(); job.template = Some(self.id.clone()); job.items.clear();
        for element in &self.elements {
            if element.enabled_by.as_ref().map(|id| values[id] != true).unwrap_or(false) { continue; }
            if element.kind == "text" && values.get(&element.bindings["content.text"]).and_then(Value::as_str).map(str::trim).unwrap_or("").is_empty() { continue; }
            let mut item = json!({"id":element.id,"side":element.side,"content":{"kind":element.kind}});
            for (target, field) in &element.bindings {
                if let Some(key) = target.strip_prefix("content.") { item["content"][key] = values[field].clone(); }
                else { item[target] = values[field].clone(); }
            }
            job.items.push(serde_json::from_value(item).map_err(|error| error.to_string())?);
        }
        Ok(job)
    }

    pub fn project(&self, values: &BTreeMap<String, Value>) -> Value {
        json!(self.fields.iter().map(|field| (field.id.clone(), json!({"provided":!values[&field.id].is_null() && values[&field.id] != "", "value":if field.revealable { values[&field.id].clone() } else { Value::Null }}))).collect::<BTreeMap<_,_>>())
    }
}

pub fn load(directory: &str) -> Result<Vec<Template>, String> {
    let mut paths = std::fs::read_dir(directory).map_err(|error| format!("templates: {error}"))?
        .map(|entry| entry.map(|entry| entry.path())).collect::<Result<Vec<_>, _>>().map_err(|error| error.to_string())?;
    paths.sort();
    let mut result = Vec::new();
    let mut ids = BTreeSet::new();
    for path in paths.into_iter().filter(|path| path.extension().and_then(|value| value.to_str()) == Some("toml")) {
        let template = parse(&path)?;
        if !ids.insert(template.id.clone()) { return Err(format!("duplicate template '{}'", template.id)); }
        result.push(template);
    }
    if result.is_empty() { return Err("at least one use-case template is required".into()); }
    Ok(result)
}

fn parse(path: &Path) -> Result<Template, String> {
    let source = std::fs::read_to_string(path).map_err(|error| error.to_string())?;
    let mut template: Template = toml::from_str(&source).map_err(|error| format!("{}: {error}", path.display()))?;
    if template.id.trim().is_empty() || template.name.trim().is_empty() || template.fields.is_empty() || template.elements.is_empty() {
        return Err(format!("{}: template needs id, name, fields and elements", path.display()));
    }
    let mut ids = BTreeSet::new();
    for field in &template.fields {
        if field.id.is_empty() || !field.id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-') || !ids.insert(&field.id) || (!field.revealable && !field.masked) {
            return Err("invalid/duplicate field id or unmasked non-revealable field".into());
        }
        if field.masked && !["text", "multiline"].contains(&field.kind.as_str()) { return Err("only text fields can be masked".into()); }
        if field.file_input && (!field.editable || field.kind != "multiline") { return Err("file input requires an editable multiline field".into()); }
    }
    let mut element_ids = BTreeSet::new();
    for element in &template.elements {
        if !element_ids.insert(&element.id) { return Err("duplicate element id".into()); }
        let required = match element.kind.as_str() {
            "wifi" => vec!["content.ssid","content.password","content.security","content.hidden","x_mm","y_mm","size_mm"],
            "qr" => vec!["content.text","x_mm","y_mm","size_mm"],
            "image" => vec!["content.asset_id","x_mm","y_mm","width_mm","height_mm"],
            "checkboxes" => vec!["content.count","content.columns","content.box_mm","content.gap_mm","content.line_mm","x_mm","y_mm"],
            "text" => vec!["content.text","content.font_size_mm","content.font","x_mm","y_mm","width_mm","height_mm"],
            _ => return Err("unsupported element kind".into()),
        };
        if element.bindings.len() != required.len() || required.iter().any(|key| !element.bindings.contains_key(*key)) || element.bindings.values().any(|id| !ids.contains(id)) { return Err("element must bind every content/geometry property to a declared field".into()); }
        for (target, id) in &element.bindings {
            let field = template.fields.iter().find(|field| &field.id == id).unwrap();
            let valid = match target.as_str() {
                "content.hidden" => field.kind == "checkbox",
                "content.count" | "content.columns" => field.kind == "number",
                "content.box_mm" | "content.gap_mm" | "content.line_mm" => field.kind == "number",
                "content.text" | "content.ssid" | "content.password" | "content.security" | "content.font" | "content.asset_id" => ["text","multiline","select"].contains(&field.kind.as_str()) && field.default.is_string(),
                _ => field.kind == "number",
            };
            if !valid { return Err(format!("field '{id}' has the wrong type for '{target}'")); }
        }
        if let Some(id) = &element.enabled_by {
            if !template.fields.iter().any(|field| &field.id == id && field.kind == "checkbox") { return Err("enabled_by needs a declared checkbox".into()); }
        }
    }
    for group in &template.exclusive_groups {
        if group.len() < 2 || group.iter().any(|id| !template.fields.iter().any(|field| &field.id == id && field.kind == "checkbox")) { return Err("exclusive groups require declared checkbox fields".into()); }
    }
    for element in template.elements.iter().filter(|element| element.kind == "wifi") {
        let password = &element.bindings["content.password"];
        if template.elements.iter().any(|item| item.kind == "text" && item.bindings.get("content.text") == Some(password)) { return Err("WiFi password cannot be bound to printed text".into()); }
    }
    template.validate_values(&template.defaults())?;
    template.display.masked_fields = template.fields.iter().filter(|field| field.masked).map(|field| field.id.clone()).collect();
    Ok(template)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wifi_defines_private_fields_and_visible_markings() {
        let templates = load(concat!(env!("CARGO_MANIFEST_DIR"), "/../use-cases")).unwrap();
        let wifi = templates.iter().find(|template| template.id == "wifi-login").unwrap();
        assert!(wifi.audio_recording_enabled);
        assert!(wifi.display.masked_fields.contains(&"ssid".into()));
        assert!(wifi.display.masked_fields.contains(&"password".into()));
        assert!(wifi.display.masked_fields.contains(&"title".into()));
        assert!(!wifi.display.card_content_visible && wifi.display.markings_visible);
        assert!(wifi.fields.iter().find(|field| field.id == "security").unwrap().options.contains(&json!("WPA3")));
        assert!(!templates.iter().find(|template| template.id == "vcard").unwrap().audio_recording_enabled);
        let file_fields: Vec<_> = templates.iter().flat_map(|template| &template.fields).filter(|field| field.file_input).map(|field| field.id.as_str()).collect();
        assert_eq!(file_fields, ["recovery-payload", "contact"]);
    }

    #[test]
    fn another_template_can_choose_different_visibility_defaults() {
        let mut template = load(concat!(env!("CARGO_MANIFEST_DIR"), "/../use-cases")).unwrap().into_iter().find(|template| template.id == "wifi-login").unwrap();
        template.display.masked_fields.clear();
        template.display.card_content_visible = true; template.display.markings_visible = false;
        assert!(template.display.masked_fields.is_empty());
        assert!(template.display.card_content_visible && !template.display.markings_visible);
    }

    #[test]
    fn locked_fields_are_omitted_and_edits_are_rejected() {
        let mut template = load(concat!(env!("CARGO_MANIFEST_DIR"), "/../use-cases")).unwrap().into_iter().find(|template| template.id == "wifi-login").unwrap();
        let password = template.fields.iter_mut().find(|field| field.id == "password").unwrap();
        password.editable = false; password.revealable = false;
        let mut values = template.defaults(); values.insert("password".into(), json!("synthetic-private"));
        let projected = template.project(&values);
        assert!(projected["password"]["value"].is_null());
        assert_eq!(projected["password"]["provided"], true);
        assert!(template.edit(&mut values, &BTreeMap::from([("password".into(), json!("changed"))])).is_err());
        assert!(template.edit(&mut values, &BTreeMap::from([("not-defined".into(), json!("changed"))])).is_err());
        assert_eq!(values["password"], "synthetic-private");
    }

    #[test]
    fn readable_wifi_defaults_fill_the_etch_width_with_clearance() {
        let template = load(concat!(env!("CARGO_MANIFEST_DIR"), "/../use-cases")).unwrap().into_iter().find(|template| template.id == "wifi-login").unwrap();
        let mut values = template.defaults();
        for (id, value) in [("ssid","Guest"),("password","synthetic-private"),("title","Guest WLAN\nMain floor"),("note","Office")] { values.insert(id.into(), json!(value)); }
        values.insert("include-note".into(), json!(true)); values.insert("include-ssid".into(), json!(true));
        let base: JobReq = serde_json::from_value(json!({"template":"wifi-login","piece":"bamboo-ntag215-light","backing":"backing-first","guide":false,"items":[]})).unwrap();
        let job = template.compose(&base, &values).unwrap();
        let materials = crate::materials::parse(include_str!("../../materials.toml")).unwrap();
        let info = k6::info();
        let place = crate::job::placement(&job, &materials, &info).unwrap();
        let etch = place.etch_area().unwrap();
        for item in &job.items {
            let x = place.piece.x + item.x_mm.unwrap(); let y = place.piece.y + item.y_mm.unwrap();
            let width = item.size_mm.or(item.width_mm).unwrap(); let height = item.size_mm.or(item.height_mm).unwrap();
            assert!(x >= etch.x + 1.49 && y >= etch.y + 1.49);
            assert!(x + width <= etch.x + etch.w - 1.49 && y + height <= etch.y + etch.h - 1.49);
        }
        assert_eq!(values["qr-side"], 36.0); assert_eq!(values["font-size"], 4.25); assert_eq!(values["font-face"], "sans-bold");
        assert!(crate::job::stages(&job, &materials, &info).is_ok());
    }
}