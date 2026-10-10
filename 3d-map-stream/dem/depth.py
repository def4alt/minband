"""Monocular depth for the DEM pipeline: Depth Anything V2 (relative inverse depth, affine-invariant).

usage: python depth.py FRAMES_DIR [--model small|base] [--n 20]   -> prints latency, writes a preview png
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


def load(name):
    return MogeAerial() if name == "moge2aerial" else RelDepth(name)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("frames"); ap.add_argument("--model", default="small"); ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--size", type=int, default=518); ap.add_argument("--out", default=None)
    a = ap.parse_args()
    files = sorted(os.listdir(os.path.join(a.frames, "rgb")))[: a.n]
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
