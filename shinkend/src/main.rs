//! shinkend — the Shinken Guest Runtime.
//!
//! M1a: a WebSocket server doing the ACI v0 handshake + **pointer action execution**
//! (move/click/double_click/right_click/scroll) via a backend-pluggable [`Executor`].
//! `type_text`/`key`, `element_ref` resolution, observation, and `screenshot` land in
//! M1b. Listens on `$SHINKEND_ADDR` (default `127.0.0.1:8765`).

mod executor;
mod protocol;

use std::sync::Arc;

use anyhow::Result;
use futures_util::{SinkExt, StreamExt};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::Message as WsMessage;

use executor::{ActionSpec, Executor};
use protocol::Message;

const DEFAULT_ADDR: &str = "127.0.0.1:8765";

#[tokio::main]
async fn main() -> Result<()> {
    let addr = std::env::var("SHINKEND_ADDR").unwrap_or_else(|_| DEFAULT_ADDR.to_string());
    let listener = TcpListener::bind(&addr).await?;
    let exec = executor::default_executor();
    eprintln!(
        "shinkend v{} listening on ws://{addr} (platform: {}, backend: {})",
        env!("CARGO_PKG_VERSION"),
        protocol::platform(),
        exec.backend()
    );

    loop {
        let (stream, peer) = listener.accept().await?;
        let exec = exec.clone();
        tokio::spawn(async move {
            if let Err(e) = serve(stream, exec).await {
                eprintln!("connection {peer} closed with error: {e}");
            }
        });
    }
}

/// Handle one client connection: ACI messages in, replies out.
async fn serve(stream: TcpStream, exec: Arc<dyn Executor>) -> Result<()> {
    let ws = tokio_tungstenite::accept_async(stream).await?;
    let (mut tx, mut rx) = ws.split();

    while let Some(frame) = rx.next().await {
        match frame? {
            WsMessage::Text(text) => {
                if let Some(reply) = handle_message(&text, exec.as_ref()) {
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
fn handle_message(text: &str, exec: &dyn Executor) -> Option<String> {
    let msg: Message = match serde_json::from_str(text) {
        Ok(m) => m,
        Err(e) => {
            return Some(protocol::error_result_text(
                "?",
                &format!("bad message: {e}"),
            ))
        }
    };
    let reply: Option<Message> = match msg {
        Message::Action { call_id, action } => Some(dispatch_action(&call_id, action, exec)),
        other => protocol::respond(other),
    };
    reply.map(|m| {
        serde_json::to_string(&m).unwrap_or_else(|e| {
            protocol::error_result_text("?", &format!("failed to encode reply: {e}"))
        })
    })
}

/// Run one `action` message through the executor and build the `ack`.
fn dispatch_action(call_id: &str, action: serde_json::Value, exec: &dyn Executor) -> Message {
    let (ok, error) = match serde_json::from_value::<ActionSpec>(action) {
        Ok(spec) => match exec.execute(&spec) {
            Ok(_) => (true, None),
            Err(e) => (false, Some(e.to_string())),
        },
        Err(e) => (false, Some(format!("bad action: {e}"))),
    };
    Message::Ack {
        call_id: call_id.to_string(),
        ok,
        error,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use executor::VirtualExecutor;

    #[test]
    fn handle_message_hello_returns_welcome_json() {
        let ex = VirtualExecutor::default();
        let reply = handle_message(
            r#"{"type":"hello","v":0,"client":{"name":"t","version":"0"}}"#,
            &ex,
        )
        .expect("reply");
        assert!(reply.contains("\"type\":\"welcome\""));
    }

    #[test]
    fn handle_message_action_dispatches_to_executor() {
        let ex = VirtualExecutor::default();
        let msg = r#"{"type":"action","call_id":"c1","action":{"verb":"click","target":{"kind":"point_px","x":1,"y":2}}}"#;
        let reply = handle_message(msg, &ex).expect("reply");
        assert!(reply.contains("\"type\":\"ack\""));
        assert!(reply.contains("\"ok\":true"));
        assert_eq!(ex.log.lock().unwrap().as_slice(), ["click"]);
    }

    #[test]
    fn handle_message_garbage_returns_error_result() {
        let ex = VirtualExecutor::default();
        let reply = handle_message("not json", &ex).expect("reply");
        assert!(reply.contains("\"ok\":false"));
    }
}
