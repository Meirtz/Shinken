//! shinkend — the Shinken Guest Runtime.
//!
//! M0: a WebSocket server that performs the ACI v0 handshake and answers
//! `ping`/`query`. Action execution and observation capture land in M1 (#4).
//! Listens on `$SHINKEND_ADDR` (default `127.0.0.1:8765`).

mod protocol;

use anyhow::Result;
use futures_util::{SinkExt, StreamExt};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::Message as WsMessage;

use protocol::Message;

const DEFAULT_ADDR: &str = "127.0.0.1:8765";

#[tokio::main]
async fn main() -> Result<()> {
    let addr = std::env::var("SHINKEND_ADDR").unwrap_or_else(|_| DEFAULT_ADDR.to_string());
    let listener = TcpListener::bind(&addr).await?;
    eprintln!(
        "shinkend v{} listening on ws://{addr} (platform: {})",
        env!("CARGO_PKG_VERSION"),
        protocol::platform()
    );

    loop {
        let (stream, peer) = listener.accept().await?;
        tokio::spawn(async move {
            if let Err(e) = serve(stream).await {
                eprintln!("connection {peer} closed with error: {e}");
            }
        });
    }
}

/// Handle one client connection: ACI messages in, replies out.
async fn serve(stream: TcpStream) -> Result<()> {
    let ws = tokio_tungstenite::accept_async(stream).await?;
    let (mut tx, mut rx) = ws.split();

    while let Some(frame) = rx.next().await {
        match frame? {
            WsMessage::Text(text) => {
                if let Some(reply) = handle_text(&text) {
                    tx.send(WsMessage::Text(reply)).await?;
                }
            }
            WsMessage::Ping(payload) => tx.send(WsMessage::Pong(payload)).await?,
            WsMessage::Close(_) => break,
            _ => {}
        }
    }
    Ok(())
}

/// Parse one inbound text frame and produce the JSON reply, if any.
fn handle_text(text: &str) -> Option<String> {
    match serde_json::from_str::<Message>(text) {
        Ok(msg) => protocol::respond(msg).map(|reply| {
            serde_json::to_string(&reply).unwrap_or_else(|e| {
                protocol::error_result_text("?", &format!("failed to encode reply: {e}"))
            })
        }),
        Err(e) => Some(protocol::error_result_text(
            "?",
            &format!("bad message: {e}"),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn handle_text_hello_returns_welcome_json() {
        let reply = handle_text(r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"}}"#)
            .expect("reply");
        assert!(reply.contains("\"type\":\"welcome\""));
    }

    #[test]
    fn handle_text_garbage_returns_error_result() {
        let reply = handle_text("not json").expect("reply");
        assert!(reply.contains("\"ok\":false"));
    }
}
