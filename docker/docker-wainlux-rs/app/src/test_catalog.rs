use crate::ladder::Step;
use laser::Info;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TestDefinition {
    pub step: Step,
    pub title: String,
    #[serde(default)]
    pub controls: Vec<String>,
    #[serde(default)]
    pub settings: Vec<String>,
    #[serde(default)]
    pub materials: bool,
    #[serde(default)]
    pub requires_real: bool,
    #[serde(default)]
    pub requires_analyzer: bool,
    #[serde(default)]
    pub requires_sync_marker: bool,
    #[serde(default)]
    pub requires_recording_ack: bool,
    #[serde(default)]
    pub stock_confirmation: Option<String>,
    #[serde(default)]
    pub fixed_x_mm: Option<f32>,
    #[serde(default)]
    pub fixed_y_mm: Option<f32>,
    #[serde(default)]
    pub fixed_size_mm: Option<f32>,
    #[serde(default)]
    pub power_values: BTreeMap<String, Vec<u32>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogFile {
    version: u32,
    tests: Vec<TestDefinition>,
}

pub fn load(path: &str, info: &Info) -> Result<Vec<TestDefinition>, String> {
    let source = std::fs::read_to_string(path).map_err(|error| format!("test catalog {path}: {error}"))?;
    let catalog: CatalogFile = toml::from_str(&source).map_err(|error| format!("test catalog {path}: {error}"))?;
    if catalog.version != 1 || catalog.tests.is_empty() {
        return Err("test catalog needs version = 1 and at least one test".into());
    }
    let mut steps = Vec::new();
    let controls = ["text", "size_mm", "x_mm", "y_mm", "limit_start_mm", "limit_end_mm", "limit_width_mm", "limit_border_mm"];
    for test in &catalog.tests {
        if test.title.trim().is_empty() || steps.contains(&test.step) {
            return Err("test catalog titles must be non-empty and recipes unique".into());
        }
        steps.push(test.step);
        if test.requires_analyzer && (!test.requires_real || !test.requires_sync_marker) {
            return Err(format!("{} requires Real mode and a sync marker when analyzer-gated", test.step_id()));
        }
        if test.requires_sync_marker && !test.requires_real {
            return Err(format!("{} requires Real mode when sync-marked", test.step_id()));
        }
        if test.requires_real && test.stock_confirmation.as_deref().unwrap_or("").trim().is_empty() {
            return Err(format!("{} requires a human confirmation message", test.step_id()));
        }
        if test.controls.iter().any(|control| !controls.contains(&control.as_str())) {
            return Err(format!("{} declares an unknown UI control", test.step_id()));
        }
        let mut unique_settings = BTreeSet::new();
        if test.settings.iter().any(|setting| setting.trim().is_empty() || !unique_settings.insert(setting)) {
            return Err(format!("{} declares an invalid or duplicate setting", test.step_id()));
        }
        if test.fixed_x_mm.is_some_and(|value| !value.is_finite())
            || test.fixed_y_mm.is_some_and(|value| !value.is_finite())
            || test.fixed_size_mm.is_some_and(|value| !value.is_finite() || value <= 0.0)
        {
            return Err(format!("{} has invalid fixed geometry", test.step_id()));
        }
        let power_values = test.power_values_for(info);
        let valid_power_count = match test.step {
            Step::PowerSweep => power_values.len() == 10,
            Step::AudioPowerSweepLow | Step::AudioPowerSweepFull => power_values.len() == 1,
            _ => power_values.is_empty(),
        };
        if !valid_power_count {
            return Err(format!("{} has the wrong number of configured power values", test.step_id()));
        }
        if !power_values.is_empty() {
            let setting = info.settings.iter().find(|setting| setting.key == "power")
                .ok_or_else(|| format!("{} requires a power setting", test.step_id()))?;
            if power_values.iter().any(|value| *value < setting.min || *value > setting.max) {
                return Err(format!("{} has a power value outside {}..={}", test.step_id(), setting.min, setting.max));
            }
            if test.step == Step::PowerSweep && (power_values[0] != setting.max || power_values.last() != Some(&setting.min) || power_values.windows(2).any(|pair| pair[0] < pair[1])) {
                return Err(format!("{} power values must descend from the maximum to the minimum", test.step_id()));
            }
        }
    }
    Ok(catalog.tests)
}

impl TestDefinition {
    pub fn power_values_for(&self, info: &Info) -> &[u32] {
        self.power_values.get(info.model).map(Vec::as_slice).unwrap_or(&[])
    }

    pub fn step_id(&self) -> String {
        serde_json::to_value(self.step).unwrap().as_str().unwrap().to_string()
    }
}