//! Owns the attempt lifecycle, not HTTP, files, clocks or devices.
//! Each state carries exactly the data required by that phase. Rejected
//! transitions leave it unchanged, including the reviewed work to execute.

use crate::{job, ladder::{Params, Step}};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::sync::Arc;
use crate::prepared::Prepared;

#[derive(Clone)]
pub(crate) enum Work {
    Step(Step, Params, Arc<Prepared>),
    Job(job::JobReq, Arc<job::Stage>, Arc<Prepared>),
}

impl Work {
    pub fn prepared(&self) -> &Prepared {
        match self { Self::Step(_, _, prepared) | Self::Job(_, _, prepared) => prepared }
    }
    pub fn step(&self) -> Option<Step> {
        match self { Self::Step(step, ..) => Some(*step), _ => None }
    }
}

/// Safe operational metadata. Private input and raster data live only in Work.
#[derive(Clone, Serialize)]
pub(crate) struct AttemptRecord {
    pub id: String,
    pub goal: String,
    pub machine: String,
    pub mode: String,
    pub planned: u64,
    #[serde(flatten)]
    pub target: Target,
}

#[derive(Clone, Serialize)]
#[serde(untagged)]
pub(crate) enum Target {
    Test { step: Step, material: Option<String> },
    Card { review_id: String, stage: usize, stage_name: String, template: Option<String> },
}

impl AttemptRecord {
    pub fn step(&self) -> Option<Step> {
        match self.target { Target::Test { step, .. } => Some(step), _ => None }
    }
    pub fn material(&self) -> Option<&str> {
        match &self.target { Target::Test { material, .. } => material.as_deref(), _ => None }
    }
}

#[derive(Clone)]
pub(crate) struct Attempt {
    pub record: AttemptRecord,
    pub work: Work,
}

impl Attempt {
    pub fn view(&self) -> Value {
        let mut view = json!(self.record);
        match &self.work {
            Work::Step(_, params, _) => view["params"] = json!(params),
            Work::Job(job, _, _) => view["job"] = json!(job.redacted()),
        }
        view
    }
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct Outcome {
    pub ok: bool,
    pub saw: String,
    pub started: u64,
    pub ended: u64,
    pub elapsed_ms: u64,
    pub band_timings: Vec<Value>,
    pub receive_trace: Option<Value>,
}

#[derive(Clone, Copy, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum VerdictStatus { Ok, Deviation, Skipped }

#[derive(Default)]
pub(crate) enum Workflow {
    #[default]
    Idle,
    Planned(Attempt),
    Running(Attempt),
    AwaitingVerdict { attempt: Attempt, outcome: Outcome },
}

impl Workflow {
    pub fn attempt(&self) -> Option<&Attempt> {
        match self {
            Self::Idle => None,
            Self::Planned(attempt) | Self::Running(attempt) | Self::AwaitingVerdict { attempt, .. } => Some(attempt),
        }
    }

    pub fn outcome(&self) -> Option<&Outcome> {
        match self { Self::AwaitingVerdict { outcome, .. } => Some(outcome), _ => None }
    }

    pub fn running(&self) -> bool { matches!(self, Self::Running(_)) }
    pub fn idle(&self) -> bool { matches!(self, Self::Idle) }
    pub fn phase(&self) -> &'static str {
        match self { Self::Idle => "idle", Self::Planned(_) => "planned", Self::Running(_) => "running", Self::AwaitingVerdict { .. } => "awaiting_verdict" }
    }

    pub fn plan(&mut self, attempt: Attempt) -> Result<(), &'static str> {
        if !self.idle() { return Err("finish the active attempt first"); }
        *self = Self::Planned(attempt);
        Ok(())
    }

    pub fn start(&mut self, expected_id: Option<&str>) -> Result<Attempt, &'static str> {
        let Self::Planned(attempt) = self else { return Err("plan first; already running or awaiting verdict"); };
        if expected_id.is_some_and(|id| attempt.record.id != id) { return Err("planned attempt changed; reload before starting"); }
        let attempt = attempt.clone();
        *self = Self::Running(attempt.clone());
        Ok(attempt)
    }

    pub fn finish(&mut self, id: &str, outcome: Outcome) -> Result<(), &'static str> {
        let Self::Running(attempt) = self else { return Err("no running attempt to finish"); };
        if attempt.record.id != id { return Err("completion belongs to a different attempt"); }
        *self = Self::AwaitingVerdict { attempt: attempt.clone(), outcome };
        Ok(())
    }

    pub fn judge(&mut self, status: VerdictStatus, comment: &str) -> Result<(Attempt, bool), &'static str> {
        if status != VerdictStatus::Ok && comment.trim().is_empty() { return Err("deviation and skipped need a comment"); }
        match self {
            Self::Idle => return Err("nothing planned"),
            Self::Running(_) => return Err("still running"),
            Self::Planned(_) if status != VerdictStatus::Skipped => return Err("run first"),
            Self::AwaitingVerdict { .. } if status == VerdictStatus::Skipped => return Err("the test ran; record OK or Deviation, not Skipped"),
            _ => {},
        }
        let accepted = status == VerdictStatus::Ok && self.outcome().is_some_and(|outcome| outcome.ok);
        let previous = std::mem::take(self);
        let attempt = match previous { Self::Planned(attempt) | Self::AwaitingVerdict { attempt, .. } => attempt, _ => unreachable!() };
        Ok((attempt, accepted))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ladder::{Params, Step};

    fn attempt(id: &str) -> Attempt {
        Attempt { record: AttemptRecord { id:id.into(), goal:"talk".into(), machine:"sim".into(), mode:"simulator".into(), planned:0, target:Target::Test { step:Step::Talk, material:None } }, work:Work::Step(Step::Talk, Params::default(), Arc::new(Step::Talk.prepare(&Params::default(), &laser::sim::info()).unwrap())) }
    }
    fn outcome(ok: bool) -> Outcome { Outcome { ok, saw: "synthetic outcome".into(), started: 1, ended: 2, elapsed_ms:1000, band_timings: vec![], receive_trace: None } }

    #[test]
    fn rejected_transitions_preserve_the_plan_and_require_a_matching_start() {
        let mut workflow = Workflow::default();
        assert!(workflow.start(None).is_err());
        workflow.plan(attempt("first")).unwrap();
        assert!(workflow.plan(attempt("replacement")).is_err());
        assert!(workflow.start(Some("stale")).is_err());
        assert!(workflow.judge(VerdictStatus::Ok, "").is_err());
        assert_eq!(workflow.attempt().unwrap().record.id, "first");
        workflow.start(Some("first")).unwrap();
        assert!(workflow.start(None).is_err());
        assert!(workflow.judge(VerdictStatus::Skipped, "not allowed while running").is_err());
        assert!(workflow.finish("stale", outcome(true)).is_err());
        assert!(workflow.running());
        workflow.finish("first", outcome(true)).unwrap();
        assert!(workflow.judge(VerdictStatus::Skipped, "already ran").is_err());
        assert!(workflow.judge(VerdictStatus::Ok, "").unwrap().1);
        assert!(workflow.idle());
    }

    #[test]
    fn failures_do_not_accept_stages_and_only_unperformed_plans_can_be_skipped() {
        let mut workflow = Workflow::default();
        workflow.plan(attempt("failed")).unwrap();
        workflow.start(None).unwrap();
        workflow.finish("failed", outcome(false)).unwrap();
        assert!(!workflow.judge(VerdictStatus::Ok, "operator observation").unwrap().1);
        workflow.plan(attempt("unperformed")).unwrap();
        assert!(workflow.judge(VerdictStatus::Skipped, "").is_err());
        assert!(!workflow.judge(VerdictStatus::Skipped, "not performed").unwrap().1);
        assert!(workflow.idle());
    }
}