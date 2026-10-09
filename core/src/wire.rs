
use serde::{Deserialize, Serialize};

/// 1: `theta_q` in `Delta` and `Keyframe` (S14); paced keyframe parts (S16).
pub const PROTOCOL_VERSION: u8 = 1;
/// Max payload per datagram; keyframes larger than this are split.
pub const MAX_DATAGRAM: usize = 1200;
/// UDP (8) + IPv4 (20) header bytes every datagram costs on the link. The budget controller, the
/// server metrics and `tools/eval` all count it; `EdgeStats::bytes_total` does not (payload only).
pub const UDP_IP_OVERHEAD: usize = 28;

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

/// `theta_q`: the position threshold (θ_pos x theta_scale) the edge was using when it sent the
/// datagram, see [`theta_q`]. A keyframe's `tick` is the tick its entity list was taken at and is
/// shared by all its parts; each entity carries the tick it was sampled at, which for a paced
/// part sent later is that part's send tick.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum Message {
    Hello { device_id: u32, session_nonce: u32, caps: u8, tick: u32 },
    Delta { seq: u32, tick: u32, theta_q: u8, updates: Vec<Update> },
    Keyframe { seq: u32, tick: u32, theta_q: u8, part: u8, of: u8, entities: Vec<EntityState> },
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
    // The version byte is checked before the body, so a peer on another protocol version fails
    // with BadVersion rather than with whatever its body happens to parse as.
    match bytes.first() {
        None => return Err(CodecError::Malformed),
        Some(&v) if v != PROTOCOL_VERSION => return Err(CodecError::BadVersion(v)),
        _ => {}
    }
    let env: Envelope = postcard::from_bytes(bytes).map_err(|_| CodecError::Malformed)?;
    Ok(env.msg)
}

/// Centimetres encoded by a `theta_q` byte: a minifloat with a 3-bit exponent `e` and a 5-bit
/// mantissa `m`, `m` cm for `e = 0`, else `(32 + m) << (e - 1)` cm. 1 cm steps up to 63 cm, then
/// steps of ~3 %, max 4032 cm. Strictly increasing in `q`.
pub fn theta_cm(q: u8) -> u32 {
    let (e, m) = ((q >> 5) as u32, (q & 31) as u32);
    if e == 0 { m } else { (32 + m) << (e - 1) }
}

/// `theta_q` decoded to metres.
pub fn theta_m(q: u8) -> f32 {
    theta_cm(q) as f32 / 100.0
}

/// Quantise a threshold in metres, rounding up so the declared threshold is never below the one in
/// use (saturates at 40.32 m). The 0.01 mm slack keeps 0.15 m (15.000001 cm as f32) at 15 cm.
pub fn theta_q(theta_m: f32) -> u8 {
    let x = theta_m * 100.0 - 0.001;
    if x.is_nan() || x <= 0.0 {
        return 0;
    }
    let t = x as u32; // truncating, saturating: no libm
    let cm = if (t as f32) < x { t.saturating_add(1) } else { t };
    (0..=255u8).find(|&q| theta_cm(q) >= cm).unwrap_or(255)
}

/// One-line human-readable summary of a datagram, for packet logs (viewer, iOS debug HUD).
pub fn describe(bytes: &[u8]) -> String {
    match decode(bytes) {
        Ok(Message::Delta { seq, tick, updates, .. }) => format!("Delta seq={seq} tick={tick} updates={}", updates.len()),
        Ok(Message::Keyframe { seq, tick, part, of, entities, .. }) => format!("Keyframe seq={seq} tick={tick} part={part}/{of} entities={}", entities.len()),
        Ok(m) => format!("{m:?}"),
        Err(e) => format!("error {e:?}"),
    }
}

/// Structured summary of a datagram, for routing and packet logs without decoding in the host.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Peek {
    /// `hello`, `delta`, `keyframe`, `pose`, `ack`, `bye` or `malformed`.
    pub kind: &'static str,
    pub device_id: Option<u32>,
    pub nonce: Option<u32>,
    pub seq: Option<u32>,
    /// Newest edge tick the datagram carries: its send tick (for a paced keyframe part, the
    /// newest entity tick; the message's own `tick` names the keyframe).
    pub tick: Option<u32>,
    /// Entity ids carried by a delta (spawns, updates and despawns) or keyframe part.
    pub ids: Option<Vec<u32>>,
    pub part: Option<u8>,
    pub of: Option<u8>,
    pub theta_m: Option<f32>,
    pub error: Option<String>,
}

pub fn peek(bytes: &[u8]) -> Peek {
    let p = Peek::default();
    match decode(bytes) {
        Ok(Message::Hello { device_id, session_nonce, tick, .. }) => Peek { kind: "hello", device_id: Some(device_id), nonce: Some(session_nonce), tick: Some(tick), ..p },
        Ok(Message::Delta { seq, tick, theta_q, updates }) => Peek {
            kind: "delta", seq: Some(seq), tick: Some(tick), theta_m: Some(theta_m(theta_q)),
            ids: Some(updates.iter().map(|u| u.id()).collect()), ..p
        },
        Ok(Message::Keyframe { seq, tick, theta_q, part, of, entities }) => Peek {
            kind: "keyframe", seq: Some(seq), tick: Some(newest_tick(tick, &entities)), theta_m: Some(theta_m(theta_q)),
            ids: Some(entities.iter().map(|e| e.id).collect()), part: Some(part), of: Some(of), ..p
        },
        Ok(Message::Pose { seq, tick, .. }) => Peek { kind: "pose", seq: Some(seq), tick: Some(tick), ..p },
        Ok(Message::Ack { .. }) => Peek { kind: "ack", ..p },
        Ok(Message::Bye { seq, tick }) => Peek { kind: "bye", seq: Some(seq), tick: Some(tick), ..p },
        Err(e) => Peek { kind: "malformed", error: Some(format!("{e:?}")), ..p },
    }
}

/// The newest of a keyframe's tick and its entities' sample ticks (wrap-aware): when it was sent.
pub fn newest_tick(tick: u32, entities: &[EntityState]) -> u32 {
    entities.iter().fold(tick, |t, e| if e.tick.wrapping_sub(t) < u32::MAX / 2 { e.tick } else { t })
}

/// `peek` as one JSON object: `{"kind":..,"deviceId"?,"nonce"?,"seq"?,"tick"?,"ids"?,"part"?,
/// "of"?,"thetaM"?,"error"?}`; absent fields are omitted.
pub fn peek_json(bytes: &[u8]) -> String {
    let p = peek(bytes);
    let mut s = format!("{{\"kind\":\"{}\"", p.kind);
    let mut num = |k: &str, v: Option<u32>| {
        if let Some(v) = v {
            s.push_str(&format!(",\"{k}\":{v}"));
        }
    };
    num("deviceId", p.device_id);
    num("nonce", p.nonce);
    num("seq", p.seq);
    num("tick", p.tick);
    num("part", p.part.map(u32::from));
    num("of", p.of.map(u32::from));
    if let Some(ids) = &p.ids {
        let ids: Vec<String> = ids.iter().map(|i| i.to_string()).collect();
        s.push_str(&format!(",\"ids\":[{}]", ids.join(",")));
    }
    if let Some(t) = p.theta_m {
        s.push_str(&format!(",\"thetaM\":{t}"));
    }
    if let Some(e) = &p.error {
        // CodecError's Debug output is alphanumeric plus parentheses: no JSON escaping needed.
        s.push_str(&format!(",\"error\":\"{e}\""));
    }
    s.push('}');
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    fn st(id: u32, tick: u32) -> EntityState {
        EntityState { id, class: 0, pos: [1.0, 0.0, -2.5], vel: [0.5, 0.0, 0.0], conf: 200, tick }
    }

    #[test]
    fn roundtrip_delta() {
        let m = Message::Delta { seq: 7, tick: 1200, theta_q: theta_q(0.15), updates: vec![Update::Update(st(3, 1200))] };
        let b = encode(&m);
        assert!(b.len() < 48, "delta with one update should be tiny, got {}", b.len());
        assert_eq!(decode(&b).unwrap(), m);
    }

    /// The sizes quoted in proto/PROTOCOL.md, docs/DESIGN.md and the naive baseline (31 B per
    /// entity, 40 B per message including 28 B UDP/IP). Ten minutes into a session: tick 72 000
    /// (3-byte varint), seq 5 000 (2 bytes), id < 128. Before tick 16 384 (136 s) ticks take 2 bytes.
    #[test]
    fn typical_sizes() {
        let s = EntityState { id: 7, class: 0, pos: [1.0, 0.0, -2.5], vel: [0.5, 0.0, 0.0], conf: 200, tick: 72_000 };
        let update = postcard::to_allocvec(&Update::Update(s)).unwrap().len();
        let entity = postcard::to_allocvec(&s).unwrap().len();
        let delta = encode(&Message::Delta { seq: 5000, tick: 72_000, theta_q: 15, updates: vec![Update::Update(s)] }).len();
        let kf = |n: usize| encode(&Message::Keyframe { seq: 5000, tick: 72_000, theta_q: 15, part: 0, of: 1, entities: vec![s; n] }).len();
        let despawn = postcard::to_allocvec(&Update::Despawn { id: 7, tick: 72_000 }).unwrap().len();
        assert_eq!((update, entity, despawn), (31, 30, 5));
        assert_eq!(delta, 40, "one-update Delta: 9 B header + 31 B; 68 B on the wire");
        assert_eq!((kf(1), kf(3)), (11 + 30, 11 + 3 * 30));
        let early = EntityState { tick: 1000, ..s };
        assert_eq!(postcard::to_allocvec(&Update::Update(early)).unwrap().len(), 30);
    }

    #[test]
    fn rejects_other_version() {
        let mut b = encode(&Message::Bye { seq: 1, tick: 1 });
        b[0] = 9;
        assert_eq!(decode(&b), Err(CodecError::BadVersion(9)));
        // A version-0 Delta (no theta_q byte) is a BadVersion, not a Malformed body.
        let v0 = [0u8, 1, 5, 0x96, 0x01, 1, 2, 0, 3, 0];
        assert_eq!(decode(&v0), Err(CodecError::BadVersion(0)));
        assert_eq!(decode(&[]), Err(CodecError::Malformed));
        assert_eq!(decode(&[PROTOCOL_VERSION, 1, 5]), Err(CodecError::Malformed));
    }

    #[test]
    fn theta_quantisation() {
        assert_eq!(theta_q(0.15), 15, "default threshold is exact");
        assert_eq!(theta_m(15), 0.15);
        assert_eq!((theta_q(0.0), theta_q(-1.0), theta_q(f32::NAN)), (0, 0, 0));
        assert_eq!(theta_q(0.001), 1, "rounds up, never down");
        assert_eq!(theta_q(1e9), 255);
        assert_eq!(theta_m(255), 40.32);
        assert_eq!(theta_cm(31) + 1, theta_cm(32));
        for q in 1..=255u8 {
            assert!(theta_cm(q) > theta_cm(q - 1), "strictly increasing at {q}");
            // Round trip: a decoded value encodes to itself.
            assert_eq!(theta_q(theta_m(q)), q, "q {q}");
        }
        // Over the range the budget controller spans (0.05 .. 1.95 m by default, up to 26 m for a
        // 2 m sweep at the max scale) the declared value is >= the true one and within 3.2 % + 1 cm.
        let mut x = 0.01f32;
        while x < 40.0 {
            let d = theta_m(theta_q(x));
            assert!(d >= x - 1e-5 && d <= x * 1.032 + 0.01, "{x} -> {d}");
            x *= 1.07;
        }
    }

    #[test]
    fn peek_reports_kind_ids_and_parts() {
        let kf = encode(&Message::Keyframe { seq: 3, tick: 240, theta_q: 15, part: 1, of: 2, entities: vec![st(4, 250), st(9, 252)] });
        assert_eq!(peek_json(&kf), r#"{"kind":"keyframe","seq":3,"tick":252,"part":1,"of":2,"ids":[4,9],"thetaM":0.15}"#);
        let d = encode(&Message::Delta { seq: 12, tick: 3456, theta_q: 40, updates: vec![Update::Spawn(st(1, 3456)), Update::Despawn { id: 2, tick: 3456 }] });
        assert_eq!(peek_json(&d), r#"{"kind":"delta","seq":12,"tick":3456,"ids":[1,2],"thetaM":0.4}"#);
        let h = encode(&Message::Hello { device_id: 7, session_nonce: 123, caps: 0, tick: 5 });
        assert_eq!(peek_json(&h), r#"{"kind":"hello","deviceId":7,"nonce":123,"tick":5}"#);
        let p = encode(&Message::Pose { seq: 5, tick: 600, pos: [0.0; 3], quat: [0.0, 0.0, 0.0, 1.0], origin_locked: true });
        assert_eq!(peek_json(&p), r#"{"kind":"pose","seq":5,"tick":600}"#);
        assert_eq!(peek_json(&encode(&Message::Ack { last_seq: 4, missing: vec![], budget_bps: 0 })), r#"{"kind":"ack"}"#);
        assert_eq!(peek_json(&encode(&Message::Bye { seq: 9, tick: 700 })), r#"{"kind":"bye","seq":9,"tick":700}"#);
        assert_eq!(peek_json(&[]), r#"{"kind":"malformed","error":"Malformed"}"#);
        assert_eq!(peek_json(&[0, 4, 0, 0, 0]), r#"{"kind":"malformed","error":"BadVersion(0)"}"#);
    }
}
