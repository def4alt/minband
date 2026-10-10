"""stream_sharp: SYNTHESIS of the verified sharpness angles on top of stream_best_pkt (500 B packet streamer,
LOD bands 0.5/1/2/4 m, context-coded octree + 16-colour palette, split_records(), 1 s slots at 20 kbit/s).

KEPT (verified, replay-controlled and official live_eval_pkt run):
  * RECEIVER: stream_sharp_render.sharp_map() - decoder-only.  Every received voxel is drawn as a flat patch of
    0.25 m squares (finest band / SHARP_LAT=2) at the cell centres with splat = 0.25 m * SHARP_SPL (1.25): a 0.5 m
    voxel = 2x2 squares, 1 m = 4x4, 2 m = 8x8, 4 m = 16x16.  Edges land where the reference (0.25 m splats) draws
    them instead of being smeared half a voxel right/down, and the horizontal striping of coarse rows disappears.
    No blending, no smoothing, no invented voxels, 16 colours: every square carries its own voxel's palette colour.
    Official run (PKT=500 --rate 20 --slot 1): FINAL 19.29/0.672 vs 19.10/0.655, TIMEAVG 17.56/0.562 vs 17.53/0.547,
    near-field (lower half of frame) ALL 17.70/0.492 vs 17.52/0.466.
  * SENDER (SHARP_ENC, default set by the measurement below): "bytes" = stream_sharp_bytes' encoder (2x2x2 colour
    blocks in the 4 m band only, BLK=1,1,1,2; compact record framing HDR=1; +0.05 dB TIMEAVG / +0.04 dB near-field in
    its own run, within noise) or "best" = stream_best_pkt's encoder, byte for byte.  Both keep the 500 B packet cut,
    nearest-to-drone-first records, split_records() and the stream header.

REJECTED (measured, see FINDINGS / the angle files): colour blocks in the 1 m or 2 m band (-0.2..-0.6 dB LIVE,
visibly blocky), palette refit per slot (never triggers), LOD reallocation by distance shaping (stream_sharp_alloc
ZG=1.3/1.5, ZCUR_H, per-band CONF: -0.3..-0.6 dB full frame for +0.03 dB near-field, more holes), lattice 0.125 m
(-0.4 dB), overhang/corner lattice, ground-level placement, cube shells (-1.2..-1.7 dB).

Env: SHARP_ENC (bytes|best), SHARP_LAT / SHARP_SPL / SHARP_EDGE / SHARP_GROUND / SHARP_CUBE (stream_sharp_render),
BLK / HDR / PALREFIT (stream_sharp_bytes, only with SHARP_ENC=bytes), PKT and the stream_best_pkt / stream_best knobs.
API: make_encoder(rate, P, C) / enc.update(k, traj, dt) -> bytes; make_decoder() / dec.apply(bytes) / dec.map() ->
(pts, rgb, splat); split_records(chunk, first); header_len.  sharp_map() and the bytes coder are importable free
functions / classes so stream_rt.py can take either piece on its own.  Python 3.11."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stream_sharp_render as SRN
from stream_sharp_render import sharp_map

ENC = os.environ.get("SHARP_ENC", "bytes")

if ENC == "bytes":
    import stream_sharp_bytes as SBY
    make_encoder = SBY.make_encoder; split_records = SBY.split_records; header_len = SBY.header_len

    class Decoder(SBY.Decoder):
        """stream_sharp_bytes' decoder (block colours, compact framing, decoded RGB kept per voxel) + sharp_map."""
        def map(self):
            return sharp_map(self.levels, self.st.known, self.crgb, self.o, self.R)
else:
    make_encoder = SRN.make_encoder; split_records = SRN.split_records; header_len = SRN.header_len
    Decoder = SRN.Decoder

def make_decoder(): return Decoder()
