//! Configured by environment: LISTEN, DATA_DIR, MACHINE (k6, sim), LASER_PORT, MATERIALS.

fn main() {
    match app::serve(app::Config::from_env()) {
        Ok(_) => loop {
            std::thread::park();
        },
        Err(e) => {
            eprintln!("laser-app: {e}");
            std::process::exit(1);
        }
    }
}
