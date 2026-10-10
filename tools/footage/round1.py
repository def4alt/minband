"""The round-1 tracker, for before/after comparisons: track.py as frozen at commit d59938b (companions
within 8 m at any size, no parallax test, 3 s re-acquisition at the predicted position only, no
static coasting, no rider association), on the same detection caches. The old file is read from
git, so nothing old is kept in the tree.

  python -I round1.py track DIR --sources det,mti --out DIR/round1
"""
import sys, os, types, subprocess
NEW = os.path.dirname(os.path.abspath(__file__))
src = subprocess.run(['git', '-C', NEW, 'show', 'd59938b:tools/footage/track.py'], check=True, capture_output=True, text=True).stdout
old = types.ModuleType('track_round1')
old.__dict__['__file__'] = os.path.join(NEW, 'track.py')  # its sys.path entry: detect.py, mti.py next to it
exec(compile(src, 'track.py@d59938b', 'exec'), old.__dict__)
sys.argv = ['track.py'] + sys.argv[1:]
old.main()
