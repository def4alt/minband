// V1, video on the same link (docs/HACKATHON_PLAN.md section 5), with the S23 thumbnail competitor.
// While the link is unshaped, video flows and the panel is live. When something limits the link,
// the panel shows what H.264 would deliver through it: one frame of bitrate / 8 / fps bytes painted
// in row by row at the link rate (a 30 KB still takes 2 min at 2 kbit/s), with the time to the next
// frame. Average frame size is the generous assumption for video (an I-frame is several times
// larger), so the comparison never flatters MinBand. The competitor is an AI thumbnail: one 150 B
// chip plus the UDP/IP header every (150 + 28) x 8 / rate seconds, at the link rate or the edge
// budget, whichever is lower. Time is the snapshot clock, so a frozen mock freezes the painting.
import type { Halftone, Lum } from './halftone';
import { THUMB_BYTES, VIDEO_FPS, fmtBps, fmtBytes, fmtClock, fmtRate, fmtSeconds, h264, linkDown, linkRate, thumbnailEvery } from './link';
import type { Snapshot } from './types';

export interface VideoLinkView {
  mode: 'flows' | 'paint' | 'stalled';
  /** Countdown to the next complete frame (paint), else ''. */
  clock: string;
  /** Headline value for the overlay row. */
  value: string;
  /** How the frame size was computed, and from what. */
  detail: string;
  /** `150 B every 0.9 s`, or null when nothing limits the link. */
  thumb: string | null;
}

export class VideoOnLink {
  private mode: VideoLinkView['mode'] | null = null;
  private next: Lum | null = null;
  private prev: Lum | null = null;
  /** Bits of the current frame delivered so far. */
  private bits = 0;
  private lastT: number | null = null;
  private drawn = -1;
  private thumbT = -Infinity;
  frames = 0;

  constructor(private ht: Halftone, private chipCanvas: HTMLCanvasElement) {}

  /** The panel was opened or closed, or the camera changed: start over from the current picture. */
  reset() { this.mode = null; this.next = this.prev = null; this.bits = 0; this.lastT = null; this.drawn = -1; this.thumbT = -Infinity; this.frames = 0; }

  update(snap: Snapshot, res: string): VideoLinkView {
    const v = h264(snap, res), frameBits = v.bps / VIDEO_FPS;
    const rate = linkRate(snap), down = linkDown(snap);
    const dt = this.lastT === null ? 0 : Math.min(1, Math.max(0, (snap.t - this.lastT) / 1000));
    this.lastT = snap.t;
    const source = `${res}p ${fmtBps(v.bps)} ÷ ${VIDEO_FPS} fps = ${fmtBytes(frameBits / 8)} a frame`;
    const thumbS = thumbnailEvery(snap);
    const thumb = thumbS === null ? null : `${THUMB_BYTES} B every ${fmtSeconds(thumbS)}`;
    if (thumbS !== null && snap.t - this.thumbT >= Math.max(500, thumbS * 1000)) {
      this.thumbT = snap.t;
      this.ht.chip(this.chipCanvas, this.ht.capture());
    }

    if (rate === null && !down) {
      if (this.mode !== 'flows') { this.reset(); this.mode = 'flows'; this.ht.live(); }
      return { mode: 'flows', clock: '', value: 'flows normally', detail: `link unshaped · ${source}`, thumb };
    }
    if (this.mode === null || this.mode === 'flows') {
      // From live (or nothing) to a limited link: the last picture stays, dimmed, and the frame on
      // screen now is the next one being sent.
      this.next = this.prev = this.ht.capture(); this.bits = 0; this.drawn = -1;
    }
    const how = `computed from the ${v.measured ? 'measured' : 'configured'} bitrate · ${source}`;
    if (down) {
      // Nothing gets through: the partial frame stays where it is.
      if (this.mode !== 'stalled') { this.mode = 'stalled'; this.drawn = -1; }
      this.paint(this.bits / frameBits);
      return { mode: 'stalled', clock: '–:––', value: 'stalled · link down', detail: how, thumb };
    }
    this.mode = 'paint';
    this.bits += rate! * dt;
    while (this.bits >= frameBits) { this.bits -= frameBits; this.prev = this.next; this.next = this.ht.capture(); this.frames++; this.drawn = -1; }
    this.paint(this.bits / frameBits);
    const left = (frameBits - this.bits) / rate!;
    return { mode: 'paint', clock: fmtClock(left), value: `next frame ${fmtClock(left)}`, detail: `${how} · link ${fmtRate(rate!)}`, thumb };
  }

  /** The whole panel once per frame, then only the rows the written row crossed. */
  private paint(progress: number) {
    if (!this.next) return;
    if (this.drawn < 0) this.ht.paint(this.next, this.prev, progress); else this.ht.paintTo(progress);
    this.drawn = 1;
  }
}
