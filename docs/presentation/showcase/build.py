"""Embed img/*.jpg into index.html as data URIs -> build/index.html (one self-contained page)."""
import base64, os, re
here = os.path.dirname(os.path.abspath(__file__))
s = open(os.path.join(here, 'index.html')).read()
s = re.sub(r'\{\{IMG:([a-z-]+)\}\}', lambda m: 'data:image/jpeg;base64,' + base64.b64encode(
    open(os.path.join(here, 'img', m.group(1) + '.jpg'), 'rb').read()).decode(), s)
os.makedirs(os.path.join(here, 'build'), exist_ok=True)
open(os.path.join(here, 'build', 'index.html'), 'w').write(s)
print('wrote build/index.html')
