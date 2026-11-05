//! Read UTF-8 files or collect instructions one line at a time.

use crate::payload::{instruction_budget, prepare, Tag};
use std::fs;
use std::io::{self, BufRead, Write};
use std::path::Path;

pub(crate) fn read_text(path: &Path) -> Result<String, String> {
    // Translate an operating-system error into a short message. Do not include
    // file contents in errors: they may contain real recovery codes.
    fs::read_to_string(path).map_err(|_| "Cannot read an input file as UTF-8 text.".into())
}

/// Accept any buffered reader and writable destination, not just the terminal.
/// This lets tests supply an in-memory reader without changing the real prompt.
pub(crate) fn read_instructions(
    mut input: impl BufRead,
    feedback: &mut impl Write,
    codes: &str,
    tag: Tag,
) -> Result<String, String> {
    // Validate the codes before asking the user to type anything.
    let budget_bytes = instruction_budget(codes, tag)?;
    let instructions = collect_lines(&mut input, feedback, budget_bytes)
        .map_err(|_| "Cannot read instructions or write prompt feedback.".to_owned())?;
    // EOF does not make empty instructions valid. Reuse final validation.
    prepare(codes, &instructions, tag)?;
    Ok(instructions)
}

// Within this helper every fallible operation is I/O, so the standard library's
// io::Result is enough. Each `?` forwards a read or write error to the caller.
fn collect_lines(
    input: &mut impl BufRead,
    feedback: &mut impl Write,
    budget_bytes: usize,
) -> io::Result<String> {
    writeln!(
        feedback,
        "Instructions: {budget_bytes} UTF-8 bytes available after codes and NDEF overhead."
    )?;
    writeln!(
        feedback,
        "Enter service, account context, and instructions. Keep passwords out."
    )?;
    writeln!(
        feedback,
        "KISS choice: feedback after each line. Ctrl-D on an empty line finishes; Ctrl-C cancels."
    )?;
    writeln!(feedback, "Input is visible. Exact capacity finishes automatically. Oversized lines are rejected, never truncated.")?;

    let mut instructions = String::new();
    loop {
        write!(feedback, "> ")?;
        // The prompt has no newline. Flush it before waiting for input.
        feedback.flush()?;

        let mut line = String::new();
        let bytes_read = input.read_line(&mut line)?;
        // A zero-length read means EOF, including Ctrl-D on an empty line.
        if bytes_read == 0 {
            writeln!(feedback, "Input finished.")?;
            break;
        }
        if line.contains('\0') {
            writeln!(
                feedback,
                "Line rejected: NUL characters are not allowed. Try again."
            )?;
            continue;
        }

        // Build a candidate first. Do not change accepted text until it fits.
        let candidate = format!("{instructions}{line}");
        // Match the trimming used when the final payload is composed.
        let used_bytes = candidate.trim().len();
        if used_bytes > budget_bytes {
            let excess_bytes = used_bytes - budget_bytes;
            let available_bytes = budget_bytes - instructions.trim().len();
            writeln!(feedback, "Line rejected: {excess_bytes} bytes over limit. {available_bytes} bytes available. Try again or Ctrl-D to finish.")?;
            continue;
        }

        instructions = candidate;
        let available_bytes = budget_bytes - used_bytes;
        writeln!(feedback, "{available_bytes} UTF-8 bytes available.")?;
        if available_bytes == 0 {
            writeln!(
                feedback,
                "Tag capacity reached. Input finished automatically."
            )?;
            break;
        }
    }

    Ok(instructions)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::payload::{compose, ndef_size};
    use std::io::Cursor;

    #[test]
    fn prompt_finishes_at_eof_and_treats_dot_as_text() {
        let mut feedback = Vec::new();
        let instructions = read_instructions(
            Cursor::new("Use once.\n.\n"),
            &mut feedback,
            "DEMO-001",
            Tag::Ntag215,
        )
        .unwrap();
        assert_eq!(instructions, "Use once.\n.\n");
        let feedback = String::from_utf8(feedback).unwrap();
        assert!(feedback.contains("478 UTF-8 bytes available"));
        assert!(feedback.contains("467 UTF-8 bytes available"));
        assert!(feedback.contains("Ctrl-D on an empty line"));
        assert!(feedback.contains("KISS choice"));
        assert!(feedback.contains("Input finished."));
    }

    #[test]
    fn prompt_counts_utf8_and_internal_newlines() {
        let mut feedback = Vec::new();
        let instructions = read_instructions(
            Cursor::new("\u{e9}\nnext\n"),
            &mut feedback,
            "DEMO-001",
            Tag::Ntag215,
        )
        .unwrap();
        assert_eq!(instructions.trim().len(), 7);
        assert!(String::from_utf8(feedback)
            .unwrap()
            .contains("471 UTF-8 bytes available"));
    }

    #[test]
    fn prompt_rejects_whole_oversized_line_and_accepts_retry() {
        let input = format!("Use once.\n{}\nKeep safe.\n", "x".repeat(500));
        let mut feedback = Vec::new();
        let instructions =
            read_instructions(Cursor::new(input), &mut feedback, "DEMO-001", Tag::Ntag215).unwrap();
        assert_eq!(instructions, "Use once.\nKeep safe.\n");
        assert!(String::from_utf8(feedback)
            .unwrap()
            .contains("Line rejected: 32 bytes over limit"));
    }

    #[test]
    fn prompt_stops_at_exact_capacity_without_reading_more() {
        for tag in [Tag::Ntag213, Tag::Ntag215, Tag::Ntag216] {
            let budget = instruction_budget("DEMO-001", tag).unwrap();
            let input = format!("{}\nUNREAD\n", "x".repeat(budget));
            let mut reader = Cursor::new(input);
            let mut feedback = Vec::new();
            let instructions =
                read_instructions(&mut reader, &mut feedback, "DEMO-001", tag).unwrap();
            assert_eq!(
                ndef_size(compose("DEMO-001", &instructions).len()),
                tag.capacity()
            );
            let mut unread = String::new();
            reader.read_line(&mut unread).unwrap();
            assert_eq!(unread, "UNREAD\n");
            assert!(String::from_utf8(feedback)
                .unwrap()
                .contains("finished automatically"));
        }
    }

    #[test]
    fn prompt_rejects_empty_input_and_codes_that_leave_no_room() {
        assert!(
            read_instructions(Cursor::new("\n"), &mut Vec::new(), "DEMO-001", Tag::Ntag215)
                .is_err()
        );
        let mut reader = Cursor::new("UNREAD\n");
        assert!(
            read_instructions(&mut reader, &mut Vec::new(), &"x".repeat(489), Tag::Ntag215)
                .is_err()
        );
        assert_eq!(reader.position(), 0);
    }
}
