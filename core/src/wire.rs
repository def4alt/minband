//! Frames and records, byte-exact (proto/PROTOCOL.md §1-4). Every record encodes to a fixed
//! layout plus optional trailing fields announced by flag bits; a frame is a 10-byte header, TLV
//! records and an optional CRC-16/CCITT-FALSE. Unknown record types are kept as `Unknown` so a
//! receiver can skip what it does not understand.

use serde::{Deserialize, Serialize};

pub const MAGIC: u8 = 0xB2;
pub const HEADER_LEN: usize = 10;
pub const CRC_LEN: usize = 2;
pub const TLV_LEN: usize = 2;
/// Carrier overhead the budget counts per frame (UDP/IPv4).
pub const UDP_IP_OVERHEAD: usize = 28;

pub const T_SESSION: u8 = 0x01;
pub const T_EGO: u8 = 0x02;
pub const T_POSE: u8 = 0x03;
pub const T_CONTACT: u8 = 0x04;
pub const T_CHIP_HEAD: u8 = 0x05;
pub const T_CHIP_SYM: u8 = 0x06;
pub const T_NOTE: u8 = 0x07;
pub const T_DIGEST: u8 = 0x81;
pub const T_FOCUS: u8 = 0x82;
pub const T_CLOCK: u8 = 0x83;
pub const T_CHIP_ACK: u8 = 0x84;

// Contact flags.
pub const F_MOTION_MASK: u8 = 0b11;
pub const F_CONFIRMED: u8 = 1 << 2;
pub const F_LOST: u8 = 1 << 3;
pub const F_DEPARTED: u8 = 1 << 4;
pub const F_FOCUSED: u8 = 1 << 5;
pub const F_GROUP: u8 = 1 << 6;
pub const F_VELOCITY: u8 = 1 << 7;
// Contact ext.
pub const X_PARENT: u8 = 1 << 0;
pub const X_ALT: u8 = 1 << 1;
pub const X_THERMAL: u8 = 1 << 2;
pub const X_MOTION_ONLY: u8 = 1 << 3;
pub const X_VERIFIED: u8 = 1 << 4;
pub const X_CHILD: u8 = 1 << 5;
pub const X_RAY: u8 = 1 << 6;
pub const X_BBOX: u8 = 1 << 7;
// Focus modes.
pub const FOCUS_TRACK: u8 = 1 << 0;
pub const FOCUS_SPLIT: u8 = 1 << 1;
pub const FOCUS_CHIP: u8 = 1 << 2;
pub const FOCUS_RELEASE: u8 = 1 << 3;
// Session caps.
pub const CAP_GNSS: u16 = 1 << 0;
pub const CAP_BARO: u16 = 1 << 1;
pub const CAP_MAG: u16 = 1 << 2;
pub const CAP_IMU: u16 = 1 << 3;
pub const CAP_RANGEFINDER: u16 = 1 << 4;
pub const CAP_THERMAL: u16 = 1 << 5;
pub const CAP_GIMBAL: u16 = 1 << 6;
pub const CAP_CHIPS: u16 = 1 << 7;
pub const CAP_UTC: u16 = 1 << 8;
pub const CAP_VO: u16 = 1 << 9;
pub const CAP_UPLINK: u16 = 1 << 10;
pub const CAP_VIDEO: u16 = 1 << 11;

pub const MOTION_UNKNOWN: u8 = 0;
pub const MOTION_STATIC: u8 = 1;
pub const MOTION_MOVING: u8 = 2;
pub const MOTION_STOPPED: u8 = 3;

pub fn motion_name(m: u8) -> &'static str {
    match m & F_MOTION_MASK { MOTION_STATIC => "static", MOTION_MOVING => "moving", MOTION_STOPPED => "stopped", _ => "unknown" }
}
pub fn nav_name(mode: u8) -> &'static str {
    match mode & 7 { 0 => "manual", 1 => "auto", 2 => "loiter", 3 => "rth", 4 => "landing", 5 => "failsafe", 6 => "lostlink", _ => "other" }
}
pub fn gnss_name(nav: u8) -> &'static str {
    match (nav >> 3) & 3 { 0 => "none", 1 => "degraded", 2 => "fix", _ => "rtk" }
}
pub fn link_name(nav: u8) -> &'static str {
    match (nav >> 5) & 3 { 0 => "hears", 1 => "silent", _ => "never" }
}

// ---- small encodings ---------------------------------------------------------------------------

/// Metres minifloat: e = q>>5, m = q&31; m/4 m for e = 0, else (32+m)/4 * 2^(e-1) m. Max 1008 m.
pub fn m8_to_m(q: u8) -> f32 {
    let (e, m) = ((q >> 5) as u32, (q & 31) as u32);
    if e == 0 { m as f32 * 0.25 } else { ((32 + m) as f32 * 0.25) * (1u32 << (e - 1)) as f32 }
}
/// Round up: the declared value is never below the true one. NaN and negatives give 0.
pub fn m_to_m8(x: f32) -> u8 {
    if !(x > 0.0) { return 0; }
    for q in 0..=255u8 { if m8_to_m(q) >= x - 1e-4 { return q; } }
    255
}
pub fn deg_to_u8(deg: f32) -> u8 {
    let mut d = deg % 360.0; if d < 0.0 { d += 360.0; }
    ((d * 256.0 / 360.0) + 0.5) as u32 as u8
}
pub fn u8_to_deg(q: u8) -> f32 { q as f32 * 360.0 / 256.0 }
/// Depression below the horizon, 0..90 degrees.
pub fn el_to_u8(deg: f32) -> u8 { (deg.clamp(0.0, 90.0) * 255.0 / 90.0 + 0.5) as u8 }
pub fn u8_to_el(q: u8) -> f32 { q as f32 * 90.0 / 255.0 }
pub fn speed_to_u8(mps: f32) -> u8 { (mps.clamp(0.0, 63.75) * 4.0 + 0.5) as u8 }
pub fn u8_to_speed(q: u8) -> f32 { q as f32 * 0.25 }
pub fn climb_to_i8(mps: f32) -> i8 { (mps.clamp(-31.75, 31.75) * 4.0).round() as i8 }
pub fn nrm_to_u8(x: f32) -> u8 { (x.clamp(0.0, 1.0) * 255.0 + 0.5) as u8 }
pub fn u8_to_nrm(q: u8) -> f32 { q as f32 / 255.0 }
/// Metres in `pos_res` units (0 = 1 cm, 1 = 10 cm, 2 = 1 m, 3 = 10 m), saturating.
pub fn pos_res_m(pos_res: u8) -> f32 { match pos_res { 0 => 0.01, 1 => 0.1, 2 => 1.0, _ => 10.0 } }
pub fn m_to_pos(m: f32, pos_res: u8) -> i16 { (m / pos_res_m(pos_res)).round().clamp(-32768.0, 32767.0) as i16 }
pub fn pos_to_m(p: i16, pos_res: u8) -> f32 { p as f32 * pos_res_m(pos_res) }
pub fn secs_u16(ticks: u32) -> u16 { (ticks / crate::TICK_HZ).min(65535) as u16 }
pub fn age_u8(ticks: u32) -> u8 { (ticks / crate::TICK_HZ).min(255) as u8 }

pub fn crc16(bytes: &[u8]) -> u16 {
    let mut crc: u16 = 0xFFFF;
    for &b in bytes {
        crc ^= (b as u16) << 8;
        for _ in 0..8 { crc = if crc & 0x8000 != 0 { (crc << 1) ^ 0x1021 } else { crc << 1 }; }
    }
    crc
}

// ---- records -----------------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct SessionRec {
    pub nonce: u32, pub device_id: u16, pub origin_lat: i32, pub origin_lon: i32, pub origin_alt: i16,
    pub pos_res: u8, pub caps: u16, pub utc_at_tick0: u32, pub hfov_x10: u16, pub img_w: u16, pub img_h: u16,
    pub video_frame0: u32, pub fps_x100: u16,
}
pub const SESSION_LEN: usize = 35;

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct EgoRec {
    pub dx: i16, pub dy: i16, pub alt_agl: i16, pub heading: u8, pub speed: u8, pub climb: i8, pub nav: u8,
    pub battery: u8, pub pos_ce: u8, pub fp_dx: i16, pub fp_dy: i16, pub fp_radius: u8,
    pub n_contacts: u8, pub n_moving: u8, pub n_dismount: u8, pub n_vehicle: u8, pub n_armour: u8, pub n_other: u8,
}
pub const EGO_LEN: usize = 23;
impl EgoRec {
    pub fn nav_byte(mode: u8, gnss: u8, link: u8, video: bool) -> u8 {
        (mode & 7) | ((gnss & 3) << 3) | ((link & 3) << 5) | if video { 0x80 } else { 0 }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct PoseRec { pub tick: u32, pub x: i32, pub y: i32, pub z: i32, pub yaw: i16, pub pitch: i16, pub roll: i16 }
pub const POSE_LEN: usize = 22;

#[derive(Clone, Copy, Debug, PartialEq, Default, Serialize, Deserialize)]
pub struct ContactRec {
    pub id: u16, pub rev: u8, pub flags: u8, pub ext: u8, pub dx: i16, pub dy: i16, pub ce: u8, pub radius: u8,
    pub n_dismount: u8, pub n_vehicle: u8, pub n_armour: u8, pub n_other: u8, pub conf: u8,
    pub first_seen: u16, pub since: u16, pub age: u8,
    pub course: u8, pub speed: u8, pub parent: u16, pub dz: i16, pub az: u8, pub el: u8, pub bbox: [u8; 4],
}
pub const CONTACT_BASE_LEN: usize = 21;
impl ContactRec {
    pub fn motion(&self) -> u8 { self.flags & F_MOTION_MASK }
    pub fn count(&self) -> u32 { self.n_dismount as u32 + self.n_vehicle as u32 + self.n_armour as u32 + self.n_other as u32 }
    pub fn has(&self, f: u8) -> bool { self.flags & f != 0 }
    pub fn ext_has(&self, x: u8) -> bool { self.ext & x != 0 }
    pub fn body_len(&self) -> usize {
        CONTACT_BASE_LEN + if self.has(F_VELOCITY) { 2 } else { 0 } + if self.ext_has(X_PARENT) { 2 } else { 0 }
            + if self.ext_has(X_ALT) { 2 } else { 0 } + if self.ext_has(X_RAY) { 2 } else { 0 } + if self.ext_has(X_BBOX) { 4 } else { 0 }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct ChipHead { pub contact: u16, pub chip: u8, pub fmt: u8, pub w: u8, pub h: u8, pub size: u16, pub k: u8, pub s: u8 }
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ChipSym { pub contact: u16, pub chip: u8, pub esi: u8, pub data: Vec<u8> }
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct NoteRec { pub kind: u8, pub text: String }
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct DigestRec { pub last_seq: u16, pub budget_10bps: u16, pub acked: Vec<(u16, u8)> }
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct FocusRec { pub id: u16, pub mode: u8, pub ttl: u8, pub chip_px: u8 }

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum Record {
    Session(SessionRec),
    Ego(EgoRec),
    Pose(PoseRec),
    Contact(ContactRec),
    ChipHead(ChipHead),
    ChipSym(ChipSym),
    Note(NoteRec),
    Digest(DigestRec),
    Focus(FocusRec),
    Clock { utc: u32 },
    ChipAck { contact: u16, chip: u8 },
    Unknown { kind: u8, body: Vec<u8> },
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Frame {
    pub session: u16, pub seq: u16, pub tick: u32, pub uplink: bool, pub cycle_end: bool,
    pub records: Vec<Record>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CodecError { BadMagic, Short, BadCrc, BadRecord(u8) }

struct W<'a>(&'a mut Vec<u8>);
impl<'a> W<'a> {
    fn u8(&mut self, v: u8) { self.0.push(v) }
    fn i8(&mut self, v: i8) { self.0.push(v as u8) }
    fn u16(&mut self, v: u16) { self.0.extend_from_slice(&v.to_le_bytes()) }
    fn i16(&mut self, v: i16) { self.0.extend_from_slice(&v.to_le_bytes()) }
    fn u32(&mut self, v: u32) { self.0.extend_from_slice(&v.to_le_bytes()) }
    fn i32(&mut self, v: i32) { self.0.extend_from_slice(&v.to_le_bytes()) }
}
struct R<'a> { b: &'a [u8], i: usize }
impl<'a> R<'a> {
    fn left(&self) -> usize { self.b.len() - self.i }
    fn take(&mut self, n: usize) -> Option<&'a [u8]> { if self.left() < n { None } else { let s = &self.b[self.i..self.i + n]; self.i += n; Some(s) } }
    fn u8(&mut self) -> Option<u8> { self.take(1).map(|s| s[0]) }
    fn i8(&mut self) -> Option<i8> { self.u8().map(|v| v as i8) }
    fn u16(&mut self) -> Option<u16> { self.take(2).map(|s| u16::from_le_bytes([s[0], s[1]])) }
    fn i16(&mut self) -> Option<i16> { self.u16().map(|v| v as i16) }
    fn u32(&mut self) -> Option<u32> { self.take(4).map(|s| u32::from_le_bytes([s[0], s[1], s[2], s[3]])) }
    fn i32(&mut self) -> Option<i32> { self.u32().map(|v| v as i32) }
}

impl Record {
    pub fn kind(&self) -> u8 {
        match self {
            Record::Session(_) => T_SESSION, Record::Ego(_) => T_EGO, Record::Pose(_) => T_POSE, Record::Contact(_) => T_CONTACT,
            Record::ChipHead(_) => T_CHIP_HEAD, Record::ChipSym(_) => T_CHIP_SYM, Record::Note(_) => T_NOTE,
            Record::Digest(_) => T_DIGEST, Record::Focus(_) => T_FOCUS, Record::Clock { .. } => T_CLOCK,
            Record::ChipAck { .. } => T_CHIP_ACK, Record::Unknown { kind, .. } => *kind,
        }
    }
    pub fn body_len(&self) -> usize {
        match self {
            Record::Session(_) => SESSION_LEN, Record::Ego(_) => EGO_LEN, Record::Pose(_) => POSE_LEN,
            Record::Contact(c) => c.body_len(), Record::ChipHead(_) => 10, Record::ChipSym(s) => 4 + s.data.len(),
            Record::Note(n) => 1 + n.text.len().min(40), Record::Digest(d) => 5 + 3 * d.acked.len().min(32),
            Record::Focus(_) => 5, Record::Clock { .. } => 4, Record::ChipAck { .. } => 3, Record::Unknown { body, .. } => body.len(),
        }
    }
    /// Bytes this record takes in a frame (TLV included).
    pub fn wire_len(&self) -> usize { TLV_LEN + self.body_len() }

    fn encode_body(&self, out: &mut Vec<u8>) {
        let mut w = W(out);
        match self {
            Record::Session(s) => {
                w.u32(s.nonce); w.u16(s.device_id); w.i32(s.origin_lat); w.i32(s.origin_lon); w.i16(s.origin_alt);
                w.u8(s.pos_res); w.u16(s.caps); w.u32(s.utc_at_tick0); w.u16(s.hfov_x10); w.u16(s.img_w); w.u16(s.img_h);
                w.u32(s.video_frame0); w.u16(s.fps_x100);
            }
            Record::Ego(e) => {
                w.i16(e.dx); w.i16(e.dy); w.i16(e.alt_agl); w.u8(e.heading); w.u8(e.speed); w.i8(e.climb); w.u8(e.nav);
                w.u8(e.battery); w.u8(e.pos_ce); w.i16(e.fp_dx); w.i16(e.fp_dy); w.u8(e.fp_radius);
                w.u8(e.n_contacts); w.u8(e.n_moving); w.u8(e.n_dismount); w.u8(e.n_vehicle); w.u8(e.n_armour); w.u8(e.n_other);
            }
            Record::Pose(p) => { w.u32(p.tick); w.i32(p.x); w.i32(p.y); w.i32(p.z); w.i16(p.yaw); w.i16(p.pitch); w.i16(p.roll); }
            Record::Contact(c) => {
                w.u16(c.id); w.u8(c.rev); w.u8(c.flags); w.u8(c.ext); w.i16(c.dx); w.i16(c.dy); w.u8(c.ce); w.u8(c.radius);
                w.u8(c.n_dismount); w.u8(c.n_vehicle); w.u8(c.n_armour); w.u8(c.n_other); w.u8(c.conf);
                w.u16(c.first_seen); w.u16(c.since); w.u8(c.age);
                if c.has(F_VELOCITY) { w.u8(c.course); w.u8(c.speed); }
                if c.ext_has(X_PARENT) { w.u16(c.parent); }
                if c.ext_has(X_ALT) { w.i16(c.dz); }
                if c.ext_has(X_RAY) { w.u8(c.az); w.u8(c.el); }
                if c.ext_has(X_BBOX) { for b in c.bbox { w.u8(b); } }
            }
            Record::ChipHead(h) => { w.u16(h.contact); w.u8(h.chip); w.u8(h.fmt); w.u8(h.w); w.u8(h.h); w.u16(h.size); w.u8(h.k); w.u8(h.s); }
            Record::ChipSym(s) => { w.u16(s.contact); w.u8(s.chip); w.u8(s.esi); w.0.extend_from_slice(&s.data); }
            Record::Note(n) => { w.u8(n.kind); let b = n.text.as_bytes(); w.0.extend_from_slice(&b[..b.len().min(40)]); }
            Record::Digest(d) => {
                w.u16(d.last_seq); w.u16(d.budget_10bps); let n = d.acked.len().min(32); w.u8(n as u8);
                for &(id, rev) in &d.acked[..n] { w.u16(id); w.u8(rev); }
            }
            Record::Focus(f) => { w.u16(f.id); w.u8(f.mode); w.u8(f.ttl); w.u8(f.chip_px); }
            Record::Clock { utc } => w.u32(*utc),
            Record::ChipAck { contact, chip } => { w.u16(*contact); w.u8(*chip); }
            Record::Unknown { body, .. } => w.0.extend_from_slice(body),
        }
    }

    fn decode_body(kind: u8, body: &[u8]) -> Result<Record, CodecError> {
        let mut r = R { b: body, i: 0 };
        let bad = CodecError::BadRecord(kind);
        let rec = match kind {
            T_SESSION => Record::Session(SessionRec {
                nonce: r.u32().ok_or(bad)?, device_id: r.u16().ok_or(bad)?, origin_lat: r.i32().ok_or(bad)?, origin_lon: r.i32().ok_or(bad)?,
                origin_alt: r.i16().ok_or(bad)?, pos_res: r.u8().ok_or(bad)?, caps: r.u16().ok_or(bad)?, utc_at_tick0: r.u32().ok_or(bad)?,
                hfov_x10: r.u16().ok_or(bad)?, img_w: r.u16().ok_or(bad)?, img_h: r.u16().ok_or(bad)?, video_frame0: r.u32().ok_or(bad)?,
                fps_x100: r.u16().ok_or(bad)?,
            }),
            T_EGO => Record::Ego(EgoRec {
                dx: r.i16().ok_or(bad)?, dy: r.i16().ok_or(bad)?, alt_agl: r.i16().ok_or(bad)?, heading: r.u8().ok_or(bad)?, speed: r.u8().ok_or(bad)?,
                climb: r.i8().ok_or(bad)?, nav: r.u8().ok_or(bad)?, battery: r.u8().ok_or(bad)?, pos_ce: r.u8().ok_or(bad)?,
                fp_dx: r.i16().ok_or(bad)?, fp_dy: r.i16().ok_or(bad)?, fp_radius: r.u8().ok_or(bad)?,
                n_contacts: r.u8().ok_or(bad)?, n_moving: r.u8().ok_or(bad)?, n_dismount: r.u8().ok_or(bad)?, n_vehicle: r.u8().ok_or(bad)?,
                n_armour: r.u8().ok_or(bad)?, n_other: r.u8().ok_or(bad)?,
            }),
            T_POSE => Record::Pose(PoseRec {
                tick: r.u32().ok_or(bad)?, x: r.i32().ok_or(bad)?, y: r.i32().ok_or(bad)?, z: r.i32().ok_or(bad)?,
                yaw: r.i16().ok_or(bad)?, pitch: r.i16().ok_or(bad)?, roll: r.i16().ok_or(bad)?,
            }),
            T_CONTACT => {
                let mut c = ContactRec {
                    id: r.u16().ok_or(bad)?, rev: r.u8().ok_or(bad)?, flags: r.u8().ok_or(bad)?, ext: r.u8().ok_or(bad)?,
                    dx: r.i16().ok_or(bad)?, dy: r.i16().ok_or(bad)?, ce: r.u8().ok_or(bad)?, radius: r.u8().ok_or(bad)?,
                    n_dismount: r.u8().ok_or(bad)?, n_vehicle: r.u8().ok_or(bad)?, n_armour: r.u8().ok_or(bad)?, n_other: r.u8().ok_or(bad)?,
                    conf: r.u8().ok_or(bad)?, first_seen: r.u16().ok_or(bad)?, since: r.u16().ok_or(bad)?, age: r.u8().ok_or(bad)?,
                    ..Default::default()
                };
                if c.has(F_VELOCITY) { c.course = r.u8().ok_or(bad)?; c.speed = r.u8().ok_or(bad)?; }
                if c.ext_has(X_PARENT) { c.parent = r.u16().ok_or(bad)?; }
                if c.ext_has(X_ALT) { c.dz = r.i16().ok_or(bad)?; }
                if c.ext_has(X_RAY) { c.az = r.u8().ok_or(bad)?; c.el = r.u8().ok_or(bad)?; }
                if c.ext_has(X_BBOX) { for i in 0..4 { c.bbox[i] = r.u8().ok_or(bad)?; } }
                Record::Contact(c)
            }
            T_CHIP_HEAD => Record::ChipHead(ChipHead {
                contact: r.u16().ok_or(bad)?, chip: r.u8().ok_or(bad)?, fmt: r.u8().ok_or(bad)?, w: r.u8().ok_or(bad)?, h: r.u8().ok_or(bad)?,
                size: r.u16().ok_or(bad)?, k: r.u8().ok_or(bad)?, s: r.u8().ok_or(bad)?,
            }),
            T_CHIP_SYM => Record::ChipSym(ChipSym { contact: r.u16().ok_or(bad)?, chip: r.u8().ok_or(bad)?, esi: r.u8().ok_or(bad)?, data: body[4.min(body.len())..].to_vec() }),
            T_NOTE => Record::Note(NoteRec { kind: r.u8().ok_or(bad)?, text: String::from_utf8_lossy(&body[1..]).into_owned() }),
            T_DIGEST => {
                let last_seq = r.u16().ok_or(bad)?; let budget_10bps = r.u16().ok_or(bad)?; let n = r.u8().ok_or(bad)? as usize;
                let mut acked = Vec::with_capacity(n);
                for _ in 0..n { acked.push((r.u16().ok_or(bad)?, r.u8().ok_or(bad)?)); }
                Record::Digest(DigestRec { last_seq, budget_10bps, acked })
            }
            T_FOCUS => Record::Focus(FocusRec { id: r.u16().ok_or(bad)?, mode: r.u8().ok_or(bad)?, ttl: r.u8().ok_or(bad)?, chip_px: r.u8().ok_or(bad)? }),
            T_CLOCK => Record::Clock { utc: r.u32().ok_or(bad)? },
            T_CHIP_ACK => Record::ChipAck { contact: r.u16().ok_or(bad)?, chip: r.u8().ok_or(bad)? },
            k => Record::Unknown { kind: k, body: body.to_vec() },
        };
        Ok(rec)
    }
}

impl Frame {
    pub fn wire_len(&self, crc: bool) -> usize {
        HEADER_LEN + self.records.iter().map(|r| r.wire_len()).sum::<usize>() + if crc { CRC_LEN } else { 0 }
    }

    pub fn encode(&self, crc: bool) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.wire_len(crc));
        {
            let mut w = W(&mut out);
            w.u8(MAGIC); w.u16(self.session); w.u16(self.seq); w.u32(self.tick);
            w.u8((self.uplink as u8) | ((crc as u8) << 1) | ((self.cycle_end as u8) << 2));
        }
        for r in &self.records {
            out.push(r.kind());
            let len_at = out.len(); out.push(0);
            r.encode_body(&mut out);
            let n = out.len() - len_at - 1;
            debug_assert!(n <= 255, "record body over 255 bytes");
            out[len_at] = n as u8;
        }
        if crc { let c = crc16(&out); out.extend_from_slice(&c.to_le_bytes()); }
        out
    }

    pub fn decode(bytes: &[u8]) -> Result<Frame, CodecError> {
        if bytes.len() < HEADER_LEN { return Err(CodecError::Short); }
        if bytes[0] != MAGIC { return Err(CodecError::BadMagic); }
        let mut r = R { b: bytes, i: 1 };
        let session = r.u16().unwrap(); let seq = r.u16().unwrap(); let tick = r.u32().unwrap(); let flags = r.u8().unwrap();
        let has_crc = flags & 2 != 0;
        let mut end = bytes.len();
        if has_crc {
            if end < HEADER_LEN + CRC_LEN { return Err(CodecError::Short); }
            end -= CRC_LEN;
            let want = u16::from_le_bytes([bytes[end], bytes[end + 1]]);
            if crc16(&bytes[..end]) != want { return Err(CodecError::BadCrc); }
        }
        let mut records = Vec::new();
        let mut i = HEADER_LEN;
        while i + TLV_LEN <= end {
            let kind = bytes[i]; let len = bytes[i + 1] as usize; i += 2;
            if i + len > end { return Err(CodecError::BadRecord(kind)); }
            records.push(Record::decode_body(kind, &bytes[i..i + len])?);
            i += len;
        }
        if i != end { return Err(CodecError::Short); }
        Ok(Frame { session, seq, tick, uplink: flags & 1 != 0, cycle_end: flags & 4 != 0, records })
    }
}

/// `{"session":..,"seq":..,"tick":..,"uplink":..,"records":[{"type":..,"len":..},..]}` or `{"error":".."}`.
pub fn peek_json(bytes: &[u8]) -> String {
    match Frame::decode(bytes) {
        Ok(f) => {
            let recs: Vec<String> = f.records.iter().map(|r| format!("{{\"type\":{},\"name\":\"{}\",\"len\":{}}}", r.kind(), record_name(r.kind()), r.body_len())).collect();
            format!("{{\"session\":{},\"seq\":{},\"tick\":{},\"uplink\":{},\"cycleEnd\":{},\"bytes\":{},\"records\":[{}]}}",
                f.session, f.seq, f.tick, f.uplink, f.cycle_end, bytes.len(), recs.join(","))
        }
        Err(e) => format!("{{\"error\":\"{e:?}\",\"bytes\":{}}}", bytes.len()),
    }
}

pub fn record_name(kind: u8) -> &'static str {
    match kind {
        T_SESSION => "Session", T_EGO => "Ego", T_POSE => "Pose", T_CONTACT => "Contact", T_CHIP_HEAD => "ChipHead",
        T_CHIP_SYM => "ChipSym", T_NOTE => "Note", T_DIGEST => "Digest", T_FOCUS => "Focus", T_CLOCK => "Clock",
        T_CHIP_ACK => "ChipAck", _ => "Unknown",
    }
}

/// One human-readable line per record, for packet logs.
pub fn describe(bytes: &[u8]) -> Vec<String> {
    let f = match Frame::decode(bytes) { Ok(f) => f, Err(e) => return vec![format!("error {e:?} ({} B)", bytes.len())] };
    let mut out = Vec::with_capacity(f.records.len() + 1);
    out.push(format!("frame seq={} tick={} {} B{}{}", f.seq, f.tick, bytes.len(), if f.uplink { " uplink" } else { "" }, if f.cycle_end { " cycle-end" } else { "" }));
    for r in &f.records { out.push(describe_record(r)); }
    out
}

pub fn describe_record(r: &Record) -> String {
    match r {
        Record::Session(s) => format!("Session nonce={:08x} dev={} origin={:.5},{:.5} res={} caps={:#06x} hfov={:.1} img={}x{} frame0={} fps={:.2}",
            s.nonce, s.device_id, s.origin_lat as f64 * 1e-7, s.origin_lon as f64 * 1e-7, pos_res_m(s.pos_res), s.caps, s.hfov_x10 as f32 / 10.0, s.img_w, s.img_h, s.video_frame0, s.fps_x100 as f32 / 100.0),
        Record::Ego(e) => format!("Ego pos=({},{}) agl={} hdg={:.0} spd={:.2} nav={} gnss={} link={}{} bat={} ce={:.2} fp=({},{}) r={:.0} n={} moving={} mix={}/{}/{}/{}",
            e.dx, e.dy, e.alt_agl, u8_to_deg(e.heading), u8_to_speed(e.speed), nav_name(e.nav), gnss_name(e.nav), link_name(e.nav), if e.nav & 0x80 != 0 { " video" } else { "" },
            e.battery, m8_to_m(e.pos_ce), e.fp_dx, e.fp_dy, m8_to_m(e.fp_radius), e.n_contacts, e.n_moving, e.n_dismount, e.n_vehicle, e.n_armour, e.n_other),
        Record::Pose(p) => format!("Pose tick={} xyz=({:.2},{:.2},{:.2}) ypr=({:.2},{:.2},{:.2})", p.tick, p.x as f32 / 100.0, p.y as f32 / 100.0, p.z as f32 / 100.0, p.yaw as f32 / 100.0, p.pitch as f32 / 100.0, p.roll as f32 / 100.0),
        Record::Contact(c) => {
            let mut s = format!("Contact id={} rev={} {}{}{}{}{}{} pos=({},{}) ce={:.2} r={:.2} mix={}/{}/{}/{} conf={} first={}s since={}s age={}s",
                c.id, c.rev, motion_name(c.flags), if c.has(F_CONFIRMED) { " confirmed" } else { "" }, if c.has(F_LOST) { " lost" } else { "" },
                if c.has(F_DEPARTED) { " departed" } else { "" }, if c.has(F_FOCUSED) { " focused" } else { "" }, if c.has(F_GROUP) { " group" } else { "" },
                c.dx, c.dy, m8_to_m(c.ce), m8_to_m(c.radius), c.n_dismount, c.n_vehicle, c.n_armour, c.n_other, c.conf, c.first_seen, c.since, c.age);
            if c.has(F_VELOCITY) { s += &format!(" crs={:.0} spd={:.2}", u8_to_deg(c.course), u8_to_speed(c.speed)); }
            if c.ext_has(X_PARENT) { s += &format!(" parent={}", c.parent); }
            if c.ext_has(X_ALT) { s += &format!(" dz={}", c.dz); }
            if c.ext_has(X_RAY) { s += &format!(" ray=({:.0},{:.1})", u8_to_deg(c.az), u8_to_el(c.el)); }
            if c.ext_has(X_BBOX) { s += &format!(" bbox=({:.2},{:.2},{:.2},{:.2})", u8_to_nrm(c.bbox[0]), u8_to_nrm(c.bbox[1]), u8_to_nrm(c.bbox[2]), u8_to_nrm(c.bbox[3])); }
            s
        }
        Record::ChipHead(h) => format!("ChipHead contact={} chip={} fmt={} {}x{} size={} K={} S={}", h.contact, h.chip, h.fmt, h.w, h.h, h.size, h.k, h.s),
        Record::ChipSym(s) => format!("ChipSym contact={} chip={} esi={} {} B", s.contact, s.chip, s.esi, s.data.len()),
        Record::Note(n) => format!("Note kind={} {:?}", n.kind, n.text),
        Record::Digest(d) => format!("Digest last_seq={} budget={} bit/s acked={:?}", d.last_seq, d.budget_10bps as u32 * 10, d.acked),
        Record::Focus(f) => format!("Focus id={} mode={:#04x} ttl={} chip_px={}", f.id, f.mode, f.ttl, f.chip_px),
        Record::Clock { utc } => format!("Clock utc={utc}"),
        Record::ChipAck { contact, chip } => format!("ChipAck contact={contact} chip={chip}"),
        Record::Unknown { kind, body } => format!("Unknown type={kind} {} B", body.len()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_contact() -> ContactRec {
        ContactRec { id: 7, rev: 3, flags: MOTION_MOVING | F_CONFIRMED | F_GROUP | F_VELOCITY, ext: X_RAY | X_BBOX, dx: -120, dy: 45, ce: m_to_m8(6.0), radius: m_to_m8(12.0),
            n_dismount: 0, n_vehicle: 4, n_armour: 1, n_other: 0, conf: 200, first_seen: 31, since: 80, age: 2, course: deg_to_u8(90.0), speed: speed_to_u8(3.25),
            az: deg_to_u8(200.0), el: el_to_u8(60.0), bbox: [120, 130, 10, 8], ..Default::default() }
    }

    #[test]
    fn m8_round_trips_and_rounds_up() {
        for q in 0..=255u8 { assert_eq!(m_to_m8(m8_to_m(q)), q, "q={q}"); }
        assert_eq!(m8_to_m(0), 0.0); assert_eq!(m8_to_m(255), 1008.0);
        assert!(m8_to_m(m_to_m8(6.1)) >= 6.1); assert!(m8_to_m(m_to_m8(6.1)) <= 6.5);
        assert_eq!(m_to_m8(0.0), 0); assert_eq!(m_to_m8(-1.0), 0); assert_eq!(m_to_m8(5000.0), 255);
        for q in 1..=255u8 { assert!(m8_to_m(q) > m8_to_m(q - 1)); }
    }

    #[test]
    fn sizes_match_the_spec() {
        let s = Record::Session(SessionRec { nonce: 1, device_id: 2, origin_lat: 0, origin_lon: 0, origin_alt: 0, pos_res: 2, caps: 0, utc_at_tick0: 0, hfov_x10: 850, img_w: 3840, img_h: 2160, video_frame0: 450, fps_x100: 2997 });
        assert_eq!(s.wire_len(), 37);
        let e = Record::Ego(EgoRec { dx: 0, dy: 0, alt_agl: 80, heading: 0, speed: 0, climb: 0, nav: 0, battery: 90, pos_ce: 0, fp_dx: 0, fp_dy: 0, fp_radius: 0, n_contacts: 0, n_moving: 0, n_dismount: 0, n_vehicle: 0, n_armour: 0, n_other: 0 });
        assert_eq!(e.wire_len(), 25);
        assert_eq!(Record::Pose(PoseRec { tick: 0, x: 0, y: 0, z: 0, yaw: 0, pitch: 0, roll: 0 }).wire_len(), 24);
        let plain = ContactRec { flags: MOTION_STATIC, ..Default::default() };
        assert_eq!(Record::Contact(plain).wire_len(), 23);
        assert_eq!(Record::Contact(sample_contact()).wire_len(), 23 + 2 + 2 + 4);
        assert_eq!(Record::Focus(FocusRec { id: 1, mode: 1, ttl: 60, chip_px: 1 }).wire_len(), 7);
        assert_eq!(Record::Digest(DigestRec { last_seq: 1, budget_10bps: 80, acked: vec![(1, 1); 8] }).wire_len(), 31);
        let f = Frame { session: 1, seq: 2, tick: 3, uplink: false, cycle_end: false, records: vec![] };
        assert_eq!(f.encode(false).len(), 10); assert_eq!(f.encode(true).len(), 12);
    }

    #[test]
    fn frame_round_trips_with_and_without_crc() {
        let f = Frame { session: 0xBEEF, seq: 65535, tick: 123456, uplink: false, cycle_end: true, records: vec![
            Record::Session(SessionRec { nonce: 0xDEADBEEF, device_id: 7, origin_lat: 393500000, origin_lon: -857000000, origin_alt: 200, pos_res: 2, caps: CAP_GNSS | CAP_VIDEO, utc_at_tick0: 1_800_000_000, hfov_x10: 850, img_w: 3840, img_h: 2160, video_frame0: 450, fps_x100: 2997 }),
            Record::Ego(EgoRec { dx: 23, dy: -16, alt_agl: 80, heading: 0, speed: 2, climb: -1, nav: EgoRec::nav_byte(2, 2, 0, true), battery: 83, pos_ce: m_to_m8(3.0), fp_dx: 0, fp_dy: 0, fp_radius: m_to_m8(60.0), n_contacts: 9, n_moving: 2, n_dismount: 3, n_vehicle: 10, n_armour: 0, n_other: 1 }),
            Record::Pose(PoseRec { tick: 123450, x: 2326, y: -1614, z: 8001, yaw: 0, pitch: -8309, roll: 12 }),
            Record::Contact(sample_contact()),
            Record::Contact(ContactRec { id: 9, rev: 250, flags: MOTION_STATIC | F_DEPARTED, ext: X_PARENT | X_ALT | X_CHILD, parent: 7, dz: -3, ..Default::default() }),
            Record::Note(NoteRec { kind: 0, text: "rth: battery".into() }),
            Record::Unknown { kind: 0x7f, body: vec![1, 2, 3] },
        ] };
        for crc in [false, true] {
            let bytes = f.encode(crc);
            assert_eq!(bytes.len(), f.wire_len(crc));
            let back = Frame::decode(&bytes).unwrap();
            assert_eq!(back, f);
        }
        let up = Frame { session: 1, seq: 1, tick: 9, uplink: true, cycle_end: false, records: vec![
            Record::Digest(DigestRec { last_seq: 41, budget_10bps: 80, acked: vec![(7, 3), (9, 250)] }),
            Record::Focus(FocusRec { id: 7, mode: FOCUS_TRACK | FOCUS_SPLIT, ttl: 60, chip_px: 1 }),
            Record::Clock { utc: 1_800_000_123 }, Record::ChipAck { contact: 7, chip: 1 },
            Record::ChipHead(ChipHead { contact: 7, chip: 1, fmt: 0, w: 64, h: 64, size: 1500, k: 48, s: 32 }),
            Record::ChipSym(ChipSym { contact: 7, chip: 1, esi: 5, data: vec![9; 32] }),
        ] };
        assert_eq!(Frame::decode(&up.encode(true)).unwrap(), up);
    }

    #[test]
    fn corruption_is_rejected() {
        let f = Frame { session: 1, seq: 1, tick: 1, uplink: false, cycle_end: false, records: vec![Record::Contact(sample_contact())] };
        let mut b = f.encode(true);
        b[15] ^= 0x40;
        assert_eq!(Frame::decode(&b), Err(CodecError::BadCrc));
        let mut b = f.encode(false);
        b[0] = 1;
        assert_eq!(Frame::decode(&b), Err(CodecError::BadMagic));
        let b = f.encode(false);
        assert!(matches!(Frame::decode(&b[..b.len() - 3]), Err(CodecError::BadRecord(T_CONTACT)) | Err(CodecError::Short)));
        assert_eq!(Frame::decode(&[]), Err(CodecError::Short));
    }

    #[test]
    fn crc16_ccitt_false_check_value() { assert_eq!(crc16(b"123456789"), 0x29B1); }

    #[test]
    fn describe_and_peek_work() {
        let f = Frame { session: 1, seq: 4, tick: 240, uplink: false, cycle_end: false, records: vec![Record::Contact(sample_contact())] };
        let lines = describe(&f.encode(false));
        assert_eq!(lines.len(), 2);
        assert!(lines[1].starts_with("Contact id=7 rev=3 moving confirmed group"), "{}", lines[1]);
        assert!(lines[1].contains("ray=(") && lines[1].contains("bbox=("));
        assert!(peek_json(&f.encode(false)).contains("\"name\":\"Contact\""));
        assert!(peek_json(&[1, 2]).contains("error"));
    }

    #[test]
    fn angle_codes() {
        assert_eq!(deg_to_u8(0.0), 0); assert_eq!(deg_to_u8(90.0), 64); assert_eq!(deg_to_u8(-90.0), 192); assert_eq!(deg_to_u8(359.9), 0);
        assert_eq!(el_to_u8(90.0), 255); assert_eq!(el_to_u8(0.0), 0); assert!((u8_to_el(el_to_u8(45.0)) - 45.0).abs() < 0.2);
        assert_eq!(speed_to_u8(100.0), 255); assert_eq!(u8_to_speed(speed_to_u8(3.25)), 3.25);
        assert_eq!(m_to_pos(-5.4, 2), -5); assert_eq!(m_to_pos(123.456, 1), 1235); assert_eq!(m_to_pos(1e9, 2), 32767);
    }
}
