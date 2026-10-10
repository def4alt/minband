"""Depth for the DEM pipeline: Depth Anything V2 (relative inverse depth, affine-invariant), MoGe2-Aerial (metric),
Depth Anything 3 (one view, or a window of views conditioned on SLAM poses).

usage: python depth.py FRAMES_DIR [--model small|base|da3small|da3metric] [--n 20]   -> prints latency, writes a preview png
"""
import argparse, os, time, numpy as np, torch
from PIL import Image

MODELS = {
    "small": "depth-anything/Depth-Anything-V2-Small-hf",  # 24.8M params
    "base": "depth-anything/Depth-Anything-V2-Base-hf",    # 97.5M params
}


def device():
    if torch.cuda.is_available():
        return "cuda"
    return "mps" if torch.backends.mps.is_available() else "cpu"


def sync():
    """Wait for queued GPU work so latencies are real."""
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    elif torch.backends.mps.is_available():
        torch.mps.synchronize()


class RelDepth:
    """img (H,W,3 uint8) -> disparity-like map (H,W) float32; larger = closer, up to scale and shift."""

    def __init__(self, name="small", input_size=518):
        from transformers import AutoModelForDepthEstimation
        self.dev = device()
        self.model = AutoModelForDepthEstimation.from_pretrained(MODELS[name]).to(self.dev).eval()
        self.size = input_size
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=self.dev).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device=self.dev).view(1, 3, 1, 1)
        self.params = sum(p.numel() for p in self.model.parameters())

    @torch.no_grad()
    def __call__(self, img):
        H, W = img.shape[:2]
        # keep aspect, short side = input_size, both sides multiple of 14 (ViT patch)
        s = self.size / min(H, W)
        h, w = int(round(H * s / 14)) * 14, int(round(W * s / 14)) * 14
        x = torch.from_numpy(img).to(self.dev).permute(2, 0, 1)[None].float() / 255
        x = torch.nn.functional.interpolate(x, (h, w), mode="bicubic", align_corners=False)
        x = (x - self.mean) / self.std
        d = self.model(pixel_values=x).predicted_depth[:, None]
        d = torch.nn.functional.interpolate(d, (H, W), mode="bilinear", align_corners=False)
        return d[0, 0].float().cpu().numpy()


AERIAL_METRIC = os.environ.get("AERIAL_METRIC", os.path.expanduser("~/src/github.com/kuieless/AerialMetric"))


class MogeAerial:
    """MoGe2-Aerial (AerialMetric, MoGe-2 ViT-L + LoRA r96 fine-tuned on UAV views): metric depth.
    __call__ returns 1/z (metres^-1) so it drops into the same affine fit as RelDepth; .last_depth keeps metres."""

    def __init__(self, fov_x=82.4, resolution_level=9):
        import sys
        sys.path.insert(0, os.path.join(AERIAL_METRIC, "MoGe"))
        from moge.scripts.a_infer_lora96_norm import MogeLoRAEngine
        self.dev = device()
        self.model = MogeLoRAEngine(os.path.join(AERIAL_METRIC, "weights", "Moge2-Aerial.pt"), device=self.dev, fp16=False).model
        self.fov_x, self.level = fov_x, resolution_level
        self.params = sum(p.numel() for p in self.model.parameters())
        self.last_depth = None

    @torch.no_grad()
    def __call__(self, img):
        t = torch.from_numpy(img).to(self.dev).permute(2, 0, 1).float() / 255
        out = self.model.infer(t, fov_x=self.fov_x, resolution_level=self.level, use_fp16=False)
        z = out["depth"].float().cpu().numpy()
        m = out["mask"].cpu().numpy().astype(bool) & np.isfinite(z) & (z > 0)
        self.last_depth = np.where(m, z, np.nan)
        return np.where(m, 1.0 / np.where(m, z, 1.0), 0.0).astype(np.float32)


DA3_MODELS = {
    "da3small": "depth-anything/DA3-SMALL",       # any-view, 34M params loaded (card says 0.08B), Apache 2.0
    "da3base": "depth-anything/DA3-BASE",         # any-view, Apache 2.0
    "da3metric": "depth-anything/DA3METRIC-LARGE",  # monocular metric, 0.35B, Apache 2.0
}


class DA3:
    """Depth Anything 3 (github.com/ByteDance-Seed/Depth-Anything-3, its own venv: it pins numpy<2 and wants xformers).
    __call__(img) is one view and returns 1/z like MogeAerial; window(imgs, w2c) conditions on known world-to-camera
    poses + intrinsics and returns per-view depth in the units of those poses. da3metric sets .last_depth in metres."""

    def __init__(self, name="da3small", process_res=504, K=None):
        from depth_anything_3.api import DepthAnything3
        self.dev = device()
        self.api = DepthAnything3.from_pretrained(DA3_MODELS[name]).to(self.dev).eval()
        self.metric = name == "da3metric"
        self.res, self.K = process_res, K  # K: 3x3 at the frame resolution, needed for pose conditioning and metric
        self.params = sum(p.numel() for p in self.api.parameters())
        self.last_depth = None

    @torch.no_grad()
    def _run(self, imgs, w2c=None):
        a = self.api
        Ks = None if w2c is None else np.repeat(self.K[None].astype(np.float32), len(imgs), 0)
        x, ex, ix = a._preprocess_inputs(list(imgs), w2c, Ks, self.res, "upper_bound_resize")
        x, ex_t, in_t = a._prepare_model_inputs(x, ex, ix)
        if ex_t is not None:  # first view at the origin, median camera distance 1 (as inference(), without its
            ex_t = ex_t @ torch.linalg.inv(ex_t[:, :1])  # 0.1 clamp, which small SLAM units hit)
            dist = torch.linalg.inv(ex_t)[..., :3, 3].norm(dim=-1)
            ex_t[..., :3, 3] /= torch.clamp(torch.median(dist), min=1e-9)
        pred = a._convert_to_prediction(a._run_model_forward(x, ex_t, in_t, [], False, False, "first"))
        H, W = imgs[0].shape[:2]
        up = lambda t: torch.nn.functional.interpolate(torch.from_numpy(np.asarray(t, np.float32)[:, None]), (H, W),
                                                        mode="bilinear", align_corners=False)[:, 0].numpy()
        d = up(pred.depth)
        self.last_conf = None if pred.conf is None else up(pred.conf)  # per view, for depth_video.py
        self.last_sky = None if pred.sky is None else up(pred.sky) > 0.5
        # depth comes out at the network's own scale whatever the pose scale: map it into the input pose units with
        # a similarity fit of the predicted poses to the given ones (what inference() does), when there are enough
        if w2c is not None and len(imgs) >= 3:
            from depth_anything_3.utils.pose_align import align_poses_umeyama
            self.last_scale = float(align_poses_umeyama(pred.extrinsics, ex.numpy(), ransac=len(imgs) >= 10,
                                                        return_aligned=True, random_state=42)[2])
            d = d / self.last_scale
        if self.metric:  # README: metres = focal (px, at the processed resolution) * output / 300
            d = d * (self.K[0, 0] * x.shape[-1] / W) / 300.0
        return d

    def __call__(self, img):
        z = self._run([img])[0]
        m = np.isfinite(z) & (z > 0)
        self.last_depth = np.where(m, z, np.nan) if self.metric else None
        return np.where(m, 1.0 / np.where(m, z, 1.0), 0.0).astype(np.float32)

    def window(self, imgs, w2c):
        return self._run(imgs, np.asarray(w2c, np.float32))


def load(name, **kw):
    if name == "moge2aerial":
        return MogeAerial(**kw)
    if name in DA3_MODELS:
        return DA3(name, **kw)
    return RelDepth(name)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("frames"); ap.add_argument("--model", default="small"); ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--size", type=int, default=518); ap.add_argument("--out", default=None)
    a = ap.parse_args()
    files = sorted(os.listdir(os.path.join(a.frames, "rgb")))[: a.n]
    if a.model in DA3_MODELS:  # --size is the long side here; K is the uav1 960x540 camera
        m = DA3(a.model, a.size, K=np.array([[548.0, 0, 480], [0, 548, 270], [0, 0, 1]]))
    else:
        m = load(a.model) if a.model == "moge2aerial" else RelDepth(a.model, a.size)
    ts = []
    for f in files:
        img = np.asarray(Image.open(os.path.join(a.frames, "rgb", f)).convert("RGB"))
        t = time.time(); d = m(img); sync(); ts.append(time.time() - t)
    print(f"{a.model}: {m.params/1e6:.1f}M params, input short side {a.size}, {img.shape[1]}x{img.shape[0]} frames, "
          f"latency median {np.median(ts[2:])*1000:.0f} ms (first {ts[0]*1000:.0f} ms) on {m.dev}")
    if a.out:
        v = (d - np.percentile(d, 1)) / (np.percentile(d, 99) - np.percentile(d, 1))
        import matplotlib.cm as cm
        Image.fromarray(np.hstack([img, (cm.magma(np.clip(v, 0, 1))[..., :3] * 255).astype(np.uint8)])).save(a.out)
