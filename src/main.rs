//! Тонкая точка входа: парсинг аргументов → вызов lib → код возврата.
//!
//! Командный слой (clap-дерево `Cli`/`Cmd`, диспетчер, тела команд) живёт в
//! `src/cli.rs` и тематических модулях `src/cli/*` (DEF-3 — декомпозиция
//! `main.rs`, 2026-10-05). Здесь остаются только примитивы вывода в stdout
//! (`out`, `outln!`, `outp!` — ими пользуются модули `cli`) и `main`.

use anyhow::Result;
use clap::Parser;

/// Печать в stdout с юниксовой семантикой пайпа: читатель, закрывший пайп
/// раньше (`| head`, `| less -F`), — тихий выход с кодом 0, а не паника
/// «Broken pipe» (std ставит SIGPIPE в ignore, поэтому `outln!` паникует;
/// `unsafe` запрещён инвариантом AD-6, поэтому ловим ошибку записи).
fn out(args: std::fmt::Arguments) {
    use std::io::Write as _;
    if let Err(e) = std::io::stdout().lock().write_fmt(args) {
        if e.kind() == std::io::ErrorKind::BrokenPipe {
            std::process::exit(0);
        }
        // Прочие ошибки вывода — как у outln!: паника с причиной.
        panic!("failed printing to stdout: {e}");
    }
}

/// `outln!`, стойкий к закрытому пайпу (см. [`out`]).
macro_rules! outln {
    () => { $crate::out(format_args!("\n")) };
    ($($arg:tt)*) => { $crate::out(format_args!("{}\n", format_args!($($arg)*))) };
}

/// `print!`, стойкий к закрытому пайпу (см. [`out`]).
macro_rules! outp {
    ($($arg:tt)*) => { $crate::out(format_args!($($arg)*)) };
}

// Объявление после макроопределений: `outln!`/`outp!` видимы во всех
// модулях `cli` (текстовая область видимости macro_rules).
mod cli;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn")),
        )
        .with_writer(std::io::stderr)
        .init();

    cli::dispatch(cli::Cli::parse()).await
}
