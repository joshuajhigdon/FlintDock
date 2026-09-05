"""Export original portal geometry as SVG and Windows icons (build-only Pillow)."""
from pathlib import Path
import sys
import html

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
from portal_art import portal_scene, portal_mark


def svg(shapes, width, height, background=None):
    items = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">']
    if background:
        items.append(f'<rect width="{width}" height="{height}" fill="{background}"/>')
    for _, points, color in shapes:
        pairs = ' '.join(f'{points[i]},{points[i+1]}' for i in range(0, len(points), 2))
        items.append(f'<polygon points="{pairs}" fill="{color}"/>')
    return '\n'.join(items) + '\n</svg>\n'


def render(shapes, viewbox, size, background=(0, 0, 0, 0)):
    from PIL import Image, ImageDraw
    scale = size * 4 / max(viewbox)
    image = Image.new('RGBA', (size*4, size*4), background)
    drawing = ImageDraw.Draw(image)
    dx, dy = (size*4-viewbox[0]*scale)/2, (size*4-viewbox[1]*scale)/2
    for _, points, color in shapes:
        drawing.polygon([(points[i]*scale+dx, points[i+1]*scale+dy)
                         for i in range(0, len(points), 2)], fill=color)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main():
    assets = ROOT / 'src/branding'
    assets.mkdir(exist_ok=True)
    (assets / 'flintdock-mark.svg').write_text(svg(portal_mark(), 64, 64), encoding='utf-8')
    (assets / 'flintdock-portal.svg').write_text(svg(portal_scene(), 240, 200), encoding='utf-8')
    mark = render(portal_mark(), (64, 64), 256)
    mark.save(assets / 'flintdock-icon.png')
    mark.save(assets / 'flintdock.ico', sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    render(portal_scene(), (240, 200), 720, '#20192b').save(assets / 'flintdock-portal-preview.png')
    print('Exported original FlintDock SVG, PNG and ICO assets.')


if __name__ == '__main__':
    main()
