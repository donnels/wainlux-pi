//! Build one text payload and check how much NFC storage it needs.
//! These functions do not read files, print prompts, or write output.

use clap::ValueEnum;

/// An enum makes unsupported tag names impossible after argument parsing.
/// `Copy` lets us pass this small value without transferring ownership.
#[derive(Clone, Copy, ValueEnum)]
pub(crate) enum Tag {
    Ntag213,
    Ntag215,
    Ntag216,
}

impl Tag {
    /// User memory in bytes, including the record headers we will write.
    pub(crate) fn capacity(self) -> usize {
        match self {
            Self::Ntag213 => 144,
            Self::Ntag215 => 504,
            Self::Ntag216 => 888,
        }
    }
}

/// Count one English NDEF Text record inside a Type 2 tag's TLV envelope.
pub(crate) fn ndef_size(text_bytes: usize) -> usize {
    // The Text record adds a status byte and the two language bytes, "en".
    let payload_bytes = text_bytes + 3;

    // Longer payloads need a four-byte length field instead of a one-byte field.
    let record_header_bytes = if payload_bytes <= 255 { 4 } else { 7 };
    let record_bytes = payload_bytes + record_header_bytes;

    // TLV means type, length, value. Its length field can also grow.
    let envelope_bytes = if record_bytes < 255 { 2 } else { 4 };
    let terminator_bytes = 1;
    record_bytes + envelope_bytes + terminator_bytes
}

/// Borrow the inputs (`&str`) and return a new, owned string (`String`).
pub(crate) fn compose(codes: &str, instructions: &str) -> String {
    format!("{}\n\n{}\n", instructions.trim(), codes.trim())
}

/// `Result` is either the checked payload (`Ok`) or an explanation (`Err`).
pub(crate) fn prepare(codes: &str, instructions: &str, tag: Tag) -> Result<String, String> {
    if codes.trim().is_empty() || instructions.trim().is_empty() {
        return Err("Codes and instructions must both be nonempty.".into());
    }
    if codes.contains('\0') || instructions.contains('\0') {
        return Err("Input must be plain UTF-8 text without NUL characters.".into());
    }

    let text = compose(codes, instructions);
    // String::len counts UTF-8 bytes, not characters. Tag memory stores bytes.
    let required_bytes = ndef_size(text.len());
    if required_bytes > tag.capacity() {
        return Err(format!(
            "Payload needs {required_bytes} bytes as one English NDEF Text record; tag has {}. Shorten the instructions or choose a larger tag.",
            tag.capacity()
        ));
    }
    Ok(text)
}

pub(crate) fn instruction_budget(codes: &str, tag: Tag) -> Result<usize, String> {
    // Check the codes and reserve room for at least one instruction byte.
    // `?` returns early if preparation fails; it does not panic.
    prepare(codes, "x", tag)?;

    // Try text sizes from largest to smallest. This reuses the same NDEF rules
    // instead of maintaining another formula for maximum capacity.
    let mut maximum_text_bytes = 0;
    for text_bytes in (0..=tag.capacity()).rev() {
        if ndef_size(text_bytes) <= tag.capacity() {
            maximum_text_bytes = text_bytes;
            break;
        }
    }
    Ok(maximum_text_bytes - compose(codes, "").len())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ndef_accounts_for_header_and_tlv_boundaries() {
        assert_eq!(ndef_size(1), 11);
        assert_eq!(ndef_size(247), 257);
        assert_eq!(ndef_size(248), 260);
        assert_eq!(ndef_size(252), 264);
        assert_eq!(ndef_size(253), 268);
    }

    #[test]
    fn fits_exact_capacity_but_rejects_one_more_byte() {
        for tag in [Tag::Ntag213, Tag::Ntag215, Tag::Ntag216] {
            let overhead = if tag.capacity() < 255 { 10 } else { 15 };
            let codes = "x".repeat(tag.capacity() - overhead - 4);
            let text = prepare(&codes, "I", tag).unwrap();
            assert_eq!(ndef_size(text.len()), tag.capacity());
            assert!(prepare(&(codes + "x"), "I", tag).is_err());
        }
    }

    #[test]
    fn preserves_codes_and_counts_utf8_bytes() {
        let text = prepare("DEMO-001\nDEMO-002\n", "Use once.", Tag::Ntag215).unwrap();
        assert_eq!(text, "Use once.\n\nDEMO-001\nDEMO-002\n");
        assert_eq!(ndef_size("\u{e9}".len()), 12);
        assert!(prepare("", "Use once.", Tag::Ntag215).is_err());
        assert!(prepare("DEMO", " ", Tag::Ntag215).is_err());
        assert!(prepare("DEMO\0", "Use once.", Tag::Ntag215).is_err());
    }
}
