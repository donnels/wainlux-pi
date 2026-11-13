mod submit;

use clap::{Parser, Subcommand};
use std::path::PathBuf;

#[derive(Parser)]
#[command(about = "Submit card content to the Pi for operator review; never start a burn")]
struct Args {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Submit a JSON card job for review; never plan or start a burn.
    Submit {
        #[arg(long, value_name = "URL")]
        server: String,
        #[arg(value_name = "JOB_JSON")]
        input: PathBuf,
        #[arg(long, value_name = "ELEMENT=FILE")]
        text_file: Vec<String>,
    },
}

fn main() {
    // Keep process exit handling here; other functions return errors to callers.
    if let Err(error) = run(Args::parse()) {
        eprintln!("Error: {error}");
        std::process::exit(1);
    }
}

fn run(args: Args) -> Result<(), String> {
    match args.command {
        Command::Submit { server, input, text_file } => {
            let receipt = submit::run(&server, &input, &text_file)?;
            println!("Review {} submitted: raster generation {} ms, preview encoding {} ms, client round-trip {} ms. Inspect it in the operator UI; no burn was started.", receipt.review_id, receipt.raster_generation_ms, receipt.preview_encoding_ms, receipt.client_round_trip_ms);
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn offline_export_is_not_a_command() {
        assert!(Args::try_parse_from(["wainlux-client", "backup"]).is_err());
    }
}
