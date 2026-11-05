//! Start here: parse arguments, read inputs, validate, render, and save.
//! Each module owns one stage; the reading guide is in docs/rust-guide.adoc.

mod input;
mod output;
mod payload;
mod pdf;

use clap::Parser;
use input::{read_instructions, read_text};
use payload::{ndef_size, prepare, Tag};
use std::io;
use std::path::PathBuf;

// clap generates the parser and --help from this struct and its attributes.
#[derive(Parser)]
#[command(about = "Create matching text, A4 PDF, SVG and PNG recovery backups for an NFC tag")]
struct Args {
    #[arg(long, value_enum)]
    tag: Tag,
    #[arg(value_name = "CODES_FILE")]
    codes: PathBuf,
    #[arg(
        long,
        value_name = "FILE",
        help = "Read instructions from a file instead of prompting"
    )]
    instructions: Option<PathBuf>,
    #[arg(
        long,
        default_value = "backup",
        help = "Output path prefix; files must not exist"
    )]
    output: PathBuf,
}

fn main() {
    // Keep process exit handling here; other functions return errors to callers.
    if let Err(error) = run(Args::parse()) {
        eprintln!("Error: {error}");
        std::process::exit(1);
    }
}

fn run(args: Args) -> Result<(), String> {
    // 1. Read inputs. The optional filename decides whether to prompt.
    let codes = read_text(&args.codes)?;
    let instructions = match args.instructions {
        Some(path) => read_text(&path)?,
        None => read_instructions(
            io::stdin().lock(),
            &mut io::stderr().lock(),
            &codes,
            args.tag,
        )?,
    };
    // 2. Validate before rendering or opening any output files.
    let text = prepare(&codes, &instructions, args.tag)?;
    eprintln!(
        "Final payload: {} UTF-8 bytes; NDEF storage: {}/{} bytes. Generating outputs.",
        text.len(),
        ndef_size(text.len()),
        args.tag.capacity()
    );
    // 3. Render all formats, then save them with private file permissions.
    output::save(&args.output, &text)?;
    // 4. Report counts, never the recovery codes themselves.
    println!(
        "Created text, A4 PDF, SVG and PNG outputs. Payload: {} bytes; NDEF storage: {}/{} bytes.",
        text.len(),
        ndef_size(text.len()),
        args.tag.capacity()
    );
    println!("Write the text as one English NDEF Text record. Verify it on the phone before storing the tag.");
    Ok(())
}
