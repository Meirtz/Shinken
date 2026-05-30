//! shinkend — the Shinken Guest Runtime.
//!
//! A WebSocket server speaking the ACI: handshake (`hello`→`welcome`) + pointer/
//! keyboard action execution + screenshot capture, via a backend-pluggable
//! [`executor::Executor`]. Connections run a [`connection::Session`] state machine
//! (handshake-first, optional dev-token auth). Listens on `$SHINKEND_ADDR`
//! (default `127.0.0.1:8765`); a non-loopback bind requires `$SHINKEND_TOKEN`.

mod connection;
mod executor;
mod protocol;

use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use futures_util::{SinkExt, StreamExt};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::Message as WsMessage;

use connection::Session;
use executor::Executor;

const DEFAULT_ADDR: &str = "127.0.0.1:8765";
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);

#[tokio::main]
async fn main() -> Result<()> {
    let addr = std::env::var("SHINKEND_ADDR").unwrap_or_else(|_| DEFAULT_ADDR.to_string());
    let token = std::env::var("SHINKEND_TOKEN")
        .ok()
        .filter(|t| !t.is_empty());

    if !is_loopback(&addr) && token.is_none() {
        eprintln!(
            "shinkend: refusing to bind non-loopback address {addr} without SHINKEND_TOKEN.\n         \
             Set SHINKEND_TOKEN to require a dev bearer token, or bind a loopback address (127.0.0.1)."
        );
        std::process::exit(1);
    }

    let listener = TcpListener::bind(&addr).await?;
    let exec = executor::default_executor();
    eprintln!(
        "shinkend v{} listening on ws://{addr} (platform: {}, backend: {}, auth: {})",
        env!("CARGO_PKG_VERSION"),
        protocol::platform(),
        exec.backend(),
        if token.is_some() {
            "token"
        } else {
            "none (loopback)"
        },
    );

    loop {
        let (stream, peer) = listener.accept().await?;
        let exec = exec.clone();
        let token = token.clone();
        tokio::spawn(async move {
            if let Err(e) = serve(stream, exec, token).await {
                eprintln!("connection {peer} closed with error: {e}");
            }
        });
    }
}

/// True if `addr`'s host is a loopback address (no token required to bind).
fn is_loopback(addr: &str) -> bool {
    let host = addr.rsplit_once(':').map_or(addr, |(h, _)| h);
    let host = host.trim_start_matches('[').trim_end_matches(']');
    host == "::1" || host == "localhost" || host.starts_with("127.")
}

/// Handle one client connection through its [`Session`] state machine.
async fn serve(stream: TcpStream, exec: Arc<dyn Executor>, token: Option<String>) -> Result<()> {
    let ws = tokio_tungstenite::accept_async(stream).await?;
    let (mut tx, mut rx) = ws.split();
    let mut session = Session::new(token, exec);

    loop {
        // Bound the time a client may stay unauthenticated.
        let next = if session.is_authenticated() {
            rx.next().await
        } else {
            match tokio::time::timeout(HANDSHAKE_TIMEOUT, rx.next()).await {
                Ok(frame) => frame,
                Err(_) => {
                    let _ = tx
                        .send(WsMessage::Text(protocol::error_result_text(
                            "?",
                            "handshake timeout",
                        )))
                        .await;
                    break;
                }
            }
        };
        let Some(frame) = next else { break };
        match frame? {
            WsMessage::Text(text) => {
                let step = session.on_text(&text);
                if let Some(reply) = step.reply {
                    tx.send(WsMessage::Text(reply)).await?;
                }
                if step.close {
                    break;
                }
            }
            WsMessage::Ping(payload) => tx.send(WsMessage::Pong(payload)).await?,
            WsMessage::Close(_) => break,
            _ => {}
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::is_loopback;

    #[test]
    fn loopback_detection() {
        assert!(is_loopback("127.0.0.1:8765"));
        assert!(is_loopback("localhost:8765"));
        assert!(is_loopback("[::1]:8765"));
        assert!(!is_loopback("0.0.0.0:8765"));
        assert!(!is_loopback("10.0.0.5:8765"));
    }
}
