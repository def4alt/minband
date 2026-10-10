"""Motion state of the receiver's contacts on matched inventory objects and mover sightings: a parked car the operator
sees moving is a ghost, a mover it sees static or unknown is late. python motion_check.py RUN DUMP GT MOVERS"""
import collections, json, sys
import numpy as np
from score_e2e import match, project

run, dump, gtp, mvp = sys.argv[1:5]
summ = json.loads(open(f"{run}/scene/summary.json").read())
W, H, f = summ["width"], summ["height"], summ["f_px"]
steps = {round(json.loads(l)["t"] * 10): json.loads(l) for l in open(dump)}
pts = [(fr["frame"], fr["objects"]) for fr in json.load(open(gtp))["frames"]]
by = {}
for m in json.load(open(mvp))["movers"]:
    for s_ in m["sightings"]:
        by.setdefault(s_["frame"], []).append({"box": s_["box"], "moving": True})
pts += list(by.items())
res = collections.Counter()
for frame, objs in pts:
    s = steps.get(round(frame / 30 * 10)); C = s["rx"]["contacts"]
    P = np.array([[c["e"], c["n"], c["u"] or .75] for c in C]); px, z = project(P, s["rx"]["pose"], f, W, H)
    m = match(objs, [(c["id"], px[j], z[j]) for j, c in enumerate(C) if not c["group"] and z[j] > 0], [], f)
    byid = {c["id"]: c for c in C}
    for i, o in enumerate(objs):
        if i in m:
            res[("mover" if o.get("moving") else "static", byid[m[i][1]]["motion"])] += 1
st = sum(v for k, v in res.items() if k[0] == "static"); mv = sum(v for k, v in res.items() if k[0] == "mover")
print(f"parked shown moving: {res[('static', 'moving')]}/{st} ({100 * res[('static', 'moving')] / max(st, 1):.0f} %); "
      f"movers shown moving: {res[('mover', 'moving')]}/{mv} ({100 * res[('mover', 'moving')] / max(mv, 1):.0f} %)  {dict(res)}")
