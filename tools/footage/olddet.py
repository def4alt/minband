"""The old pipeline's detector, for before/after comparisons: detect.py as of commit 6f21d55 (NMS fed
x1 y1 x2 y2 as x, y, w, h; frames smaller than a tile stretched to the square input), driven by the
current track.py detect stage (whose registration does not crash on cuts or thermal), writing a
tagged cache. The old file is read from git, so nothing old is kept in the tree.

  python -I olddet.py detect VIDEO --model M --tag old --tile 640 --conf 0.15 --out DIR
"""
import sys, os, types, subprocess
NEW = os.path.dirname(os.path.abspath(__file__))
src = subprocess.run(['git', '-C', NEW, 'show', '6f21d55:tools/footage/detect.py'], check=True, capture_output=True, text=True).stdout
old = types.ModuleType('old_detect'); exec(compile(src, 'detect.py@6f21d55', 'exec'), old.__dict__)


class Shim(old.TiledDetector):
    names = 'original detect.py (6f21d55)'

    def __init__(self, model_path, tile=960, overlap=96, conf=0.25, threads=0, size=0):
        super().__init__(model_path, tile=tile, overlap=overlap, conf=conf, threads=threads, size=size)

    def tile_for(self, w, h):
        return self.tile

    def __call__(self, frame, raw=False):
        d = super().__call__(frame)
        return (d, d) if raw else d


mod = types.ModuleType('detect'); mod.TiledDetector = Shim
sys.modules['detect'] = mod
sys.path.insert(0, NEW)
import track  # noqa: E402
sys.argv = ['track.py'] + sys.argv[1:]
track.main()
