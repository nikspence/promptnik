"""Trace supplied raster logos into real SVG paths (asset build tool only).

Requires Pillow and NumPy for tracing; the application needs neither.
Uses palette regions and simplified closed contours, preserving the source
letterforms rather than substituting a font. PNGs are rendered from the SVGs.
"""
from collections import defaultdict, deque
from pathlib import Path
import math
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
from PIL import Image
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'promptnik'/'assets'

def foreground(rgb, dark):
    possible_background = rgb.max(axis=2) < 25 if dark else rgb.min(axis=2) > 235
    h,w = possible_background.shape
    exterior = np.zeros((h,w),bool)
    pending = deque([(0,0)])
    exterior[0,0] = True
    while pending:
        y,x = pending.popleft()
        for ny,nx in ((y-1,x),(y+1,x),(y,x-1),(y,x+1)):
            if 0 <= ny < h and 0 <= nx < w and possible_background[ny,nx] and not exterior[ny,nx]:
                exterior[ny,nx] = True; pending.append((ny,nx))
    # Letter counters are backdrop; white/black regions enclosed by the
    # robot are artwork. The source wordmark starts at x ~= 498.
    exterior[:,498:] |= possible_background[:,498:]
    return ~exterior

def simplify(points, epsilon=.65):
    if len(points) <= 2: return points
    a,b = points[0],points[-1]; dx,dy = b[0]-a[0],b[1]-a[1]
    denom = math.hypot(dx,dy)
    distances = [abs(dx*(a[1]-p[1])-(a[0]-p[0])*dy)/denom if denom else math.dist(a,p) for p in points]
    index = max(range(len(points)),key=distances.__getitem__)
    if distances[index] <= epsilon: return [a,b]
    return simplify(points[:index+1],epsilon)[:-1]+simplify(points[index:],epsilon)

def contours(mask):
    padded = np.pad(mask,1)
    neighbors = (padded[:-2,1:-1],padded[1:-1,2:],padded[2:,1:-1],padded[1:-1,:-2])
    edges = defaultdict(list)
    for side,neighbor in enumerate(neighbors):
        ys,xs = np.nonzero(mask & ~neighbor)
        for y,x in zip(ys.tolist(),xs.tolist()):
            a,b = (((x,y),(x+1,y)),((x+1,y),(x+1,y+1)),((x+1,y+1),(x,y+1)),((x,y+1),(x,y)))[side]
            edges[a].append(b)
    while edges:
        start = next(iter(edges)); point = start; points = [start]
        while True:
            targets = edges[point]; following = targets.pop()
            if not targets: del edges[point]
            point = following; points.append(point)
            if point == start: break
        if len(points) < 8: continue
        area = abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points,points[1:])))/2
        if area < 3: continue
        mid = len(points)//2
        yield simplify(points[:mid+1])[:-1]+simplify(points[mid:])

def path_data(mask):
    pieces = []
    for contour in contours(mask):
        pieces.append('M'+' L'.join(f'{x},{y}' for x,y in contour)+'Z')
    return ''.join(pieces)

def render(svg, png, width=None, height=None):
    renderer = QSvgRenderer(str(svg)); size = renderer.defaultSize()
    image = QImage(width or size.width(),height or size.height(),QImage.Format.Format_ARGB32_Premultiplied); image.fill(0)
    painter = QPainter(image); renderer.render(painter,QRectF(0,0,image.width(),image.height())); painter.end()
    if not image.save(str(png)): raise RuntimeError('Cannot write '+str(png))

def trace(variant):
    original = Image.open(ROOT/f'promptnik-{variant}.png').convert('RGB')
    rgb = np.asarray(original); fg = foreground(rgb,variant=='dark')
    ys,xs = np.nonzero(fg)
    left,top,right,bottom = max(0,int(xs.min())-12),max(0,int(ys.min())-12),min(original.width,int(xs.max())+13),min(original.height,int(ys.max())+13)
    # Raster gradients become many compact vector-color regions.
    quantized = original.quantize(colors=96,method=Image.Quantize.FASTOCTREE)
    indices = np.asarray(quantized); palette = quantized.getpalette()
    pieces = []
    for index in np.unique(indices[fg]):
        index = int(index)
        path = path_data(fg & (indices==index))
        if path:
            r,g,b = palette[index*3:index*3+3]
            color = f'#{r:02x}{g:02x}{b:02x}'
            pieces.append(f'<path fill="{color}" stroke="{color}" stroke-width="0.8" stroke-linejoin="round" d="{path}"/>')
    paths = ''.join(pieces)
    # A foreground silhouette clip ensures palette contours cannot escape the
    # original outline. SVG has no background rectangle or embedded bitmap.
    clip = path_data(fg)
    body = f'<defs><clipPath id="outline"><path fill-rule="evenodd" d="{clip}"/></clipPath></defs><g clip-path="url(#outline)" fill-rule="evenodd"><path fill="#031027" d="{clip}"/>{paths}</g>'
    def svg_document(view):
        x,y,w,h = view
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="{x} {y} {w} {h}"><title>Promptnik {variant} logo</title>{body}</svg>\n'
    svg = OUT/f'promptnik-{variant}.svg'; svg.write_text(svg_document((left,top,right-left,bottom-top)),encoding='utf-8')
    render(svg,svg.with_suffix('.png'))
    if variant == 'light':
        icon = OUT/'promptnik-icon.svg'
        icon.write_text(svg_document((45,255,453,438)),encoding='utf-8')
        render(icon,icon.with_suffix('.png'),256,256)
        Image.open(icon.with_suffix('.png')).save(OUT/'promptnik-icon.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
    print(variant, svg.stat().st_size, 'bytes SVG',right-left,bottom-top)

if __name__ == '__main__':
    app = QApplication([]); OUT.mkdir(exist_ok=True)
    for variant in ('light','dark'): trace(variant)
