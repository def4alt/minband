"""Fetch single clips of the MEVA UAV drone footage without downloading the 28 GB archive.

MEVA (Multiview Extended Video with Activities, Kitware/IARPA, CC-BY-4.0) released 45 clips from
two DJI Inspire 1 v2 drones (Zenmuse X3, 3840x2160 at 30 fps, re-encoded at CRF 26) over the
Muscatatuck Urban Training Center as one uncompressed tar in a public S3 bucket. A tar stores
each file contiguously after a 512 B header, so walking the headers with HTTP range requests lists
the clips, and one range request fetches a clip.

  python meva.py index                       # clip offsets and sizes (~45 small requests)
  python meva.py fetch 2018-03-13.16-00-14 OUTDIR

Attribution: MEVA dataset, https://mevadata.org, licensed CC-BY-4.0.
"""
import sys, os, urllib.request

URL = 'https://s3.amazonaws.com/mevadata-public-01/uav-drop-01/meva-uav-drop-01.tar'


def get(off, n):
    req = urllib.request.Request(URL, headers={'Range': f'bytes={off}-{off + n - 1}'})
    return urllib.request.urlopen(req, timeout=60).read()


def index():
    off, seen = 0, set()
    while True:
        h = get(off, 512)
        if len(h) < 512 or h == b'\0' * 512: return
        name = h[0:100].rstrip(b'\0').decode(); prefix = h[345:500].rstrip(b'\0').decode()
        size = int(h[124:136].rstrip(b'\0 ').decode() or '0', 8)
        full = f'{prefix}/{name}' if prefix else name
        if full.endswith('.mp4') and full not in seen:  # the archive lists one clip twice
            seen.add(full); yield off + 512, size, full
        off += 512 + (size + 511) // 512 * 512


def fetch(key, out):
    for data_off, size, name in index():
        if key in name:
            os.makedirs(out, exist_ok=True)
            dest = os.path.join(out, os.path.basename(name))
            req = urllib.request.Request(URL, headers={'Range': f'bytes={data_off}-{data_off + size - 1}'})
            with urllib.request.urlopen(req, timeout=600) as r, open(dest, 'wb') as fh:
                while chunk := r.read(1 << 20): fh.write(chunk)
            got = os.path.getsize(dest)
            if got != size: raise SystemExit(f'{dest}: got {got} of {size} bytes')
            print(dest); return
    raise SystemExit(f'no clip matching {key!r}')


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == 'index':
        for off, size, name in index(): print(f'{off}\t{size / 1e6:8.1f} MB\t{name}')
    elif len(sys.argv) == 4 and sys.argv[1] == 'fetch':
        fetch(sys.argv[2], sys.argv[3])
    else:
        print(__doc__); sys.exit(2)
