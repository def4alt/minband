
use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION: u8 = 0;
/// Max payload per datagram; keyframes larger than this are split.
pub const MAX_DATAGRAM: usize = 1200;

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct EntityState {
    pub id: u32,
    pub class: u8,
    pub pos: [f32; 3],
    pub vel: [f32; 3],
    pub conf: u8,
    pub tick: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub enum Update {
    Spawn(EntityState),
    Update(EntityState),
    Despawn { id: u32, tick: u32 },
}

impl Update {
    pub fn id(&self) -> u32 {
        match self {
            Update::Spawn(s) | Update::Update(s) => s.id,
            Update::Despawn { id, .. } => *id,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum Message {
    Hello { device_id: u32, session_nonce: u32, caps: u8, tick: u32 },
    Delta { seq: u32, tick: u32, updates: Vec<Update> },
    Keyframe { seq: u32, tick: u32, part: u8, of: u8, entities: Vec<EntityState> },
    Pose { seq: u32, tick: u32, pos: [f32; 3], quat: [f32; 4], origin_locked: bool },
    Ack { last_seq: u32, missing: Vec<u32>, budget_bps: u32 },
    Bye { seq: u32, tick: u32 },
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct Envelope {
    version: u8,
    msg: Message,
}

#[derive(Debug, PartialEq)]
pub enum CodecError {
    BadVersion(u8),
    Malformed,
}

pub fn encode(msg: &Message) -> Vec<u8> {
    let env = Envelope { version: PROTOCOL_VERSION, msg: msg.clone() };
    postcard::to_allocvec(&env).expect("postcard encode cannot fail for these types")
}

pub fn decode(bytes: &[u8]) -> Result<Message, CodecError> {
    let env: Envelope = postcard::from_bytes(bytes).map_err(|_| CodecError::Malformed)?;
    if env.version != PROTOCOL_VERSION {
        return Err(CodecError::BadVersion(env.version));
    }
    Ok(env.msg)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_delta() {
        let m = Message::Delta {
            seq: 7,
            tick: 1200,
            updates: vec![Update::Update(EntityState {
                id: 3, class: 0, pos: [1.0, 0.0, -2.5], vel: [0.5, 0.0, 0.0], conf: 200, tick: 1200,
            })],
        };
        let b = encode(&m);
        assert!(b.len() < 48, "delta with one update should be tiny, got {}", b.len());
        assert_eq!(decode(&b).unwrap(), m);
    }

    #[test]
    fn rejects_other_version() {
        let mut b = encode(&Message::Bye { seq: 1, tick: 1 });
        b[0] = 9;
        assert_eq!(decode(&b), Err(CodecError::BadVersion(9)));
    }
}
