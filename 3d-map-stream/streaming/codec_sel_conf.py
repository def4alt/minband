"""Selection winner: LOD bands (0.5/1/2/4 m, SPL 1.0) + density-confidence pruning (CONF=0.4):
a voxel is dropped when its source-point count is < CONF x the median count expected at its viewing
distance (cnt * z^2 per band).  Removes low-density fringe/sky-line speckle so the bisection lands on a
finer ALPHA for everything that is real.  Causal (per-chunk statistics, known poses only) - see live_sim_sel.py.
Env overrides as in codec_sel_core.py (CONF 0.3 -> holes ~1.0%, 0.5 -> ~1.4% but +0.1 dB)."""
import os, importlib.util, pathlib
for k, v in dict(LEVELS="0.5,1.0,2.0,4.0", SPL="1.0", BUDGET="28900", CONF="0.4").items(): os.environ.setdefault(k, v)
_s = importlib.util.spec_from_file_location("codec_sel_core", str(pathlib.Path(__file__).with_name("codec_sel_core.py")))
_m = importlib.util.module_from_spec(_s); _s.loader.exec_module(_m)
encode, decode = _m.encode, _m.decode
