"""Export Ultralytics detection weights to ONNX for detect.py (tools/footage/models/, gitignored).

  .venv/bin/python export_models.py yolo11s yolo11m yolo26n yolo26s yolo26m   # dynamic 640 exports
  .venv/bin/python export_models.py yolo26s --imgsz 960 --fixed               # fixed 960 input: 1.5x tiles
  .venv/bin/python export_models.py path/to/visdrone-yolov8l.pt --name vd_l   # any .pt with names

Weights (yoloNN{n,s,m}.pt) download from the Ultralytics release on first use. YOLO26 exports are
end-to-end (N x 300 x 6, NMS in the graph) and detect.py's `end2end` branch reads them; YOLO11 exports
are the classic (4 + classes) x anchors layout. Everything from Ultralytics is AGPL-3.0: fine for a
hackathon prototype, not for a closed product. Weights are never committed (.gitignore: *.pt, *.onnx).
"""
import argparse, os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, 'models')


def export(src: str, name: str, imgsz: int, fixed: bool, opset: int, half: bool = False) -> str:
    from ultralytics import YOLO
    os.chdir(MODELS)  # downloaded .pt files land here
    m = YOLO(src)
    out = m.export(format='onnx', imgsz=imgsz, dynamic=not fixed, simplify=True, opset=opset, half=half)
    dst = os.path.join(MODELS, f'{name}{"" if imgsz == 640 else f"-{imgsz}"}{"f" if fixed else ""}.onnx')
    shutil.move(out, dst)
    return dst


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('weights', nargs='+', help='yolo11s, yolo26m, ... or a path to a .pt')
    p.add_argument('--imgsz', type=int, default=640)
    p.add_argument('--fixed', action='store_true', help='fixed input size (detect.py then tiles at imgsz / 1.5)')
    p.add_argument('--opset', type=int, default=13)
    p.add_argument('--name', help='output stem (single weight only)')
    a = p.parse_args()
    os.makedirs(MODELS, exist_ok=True)
    for w in a.weights:
        src = w if w.endswith('.pt') else f'{w}.pt'
        name = a.name or os.path.splitext(os.path.basename(w))[0]
        print('->', export(src, name, a.imgsz, a.fixed, a.opset), flush=True)
