"""Original vector artwork for FlintDock; no Minecraft textures or game assets.

The same simple shapes drive Tk canvases and the build-time SVG/Windows icon.
Artwork is decorative; plain-language server state is always displayed separately.
"""
import math

OBSIDIAN = '#252033'
VIOLET = '#bd91ff'
SPARK = '#ffb56b'


def portal_scene(state='online'):
    """Return bounded shapes in a 240 x 200 viewbox. No timers or random draws."""
    shapes = []
    def polygon(points, color):
        shapes.append(('polygon', points, color))
    def rect(x, y, w, h, color):
        polygon((x, y, x+w, y, x+w, y+h, x, y+h), color)

    lit = state == 'online'
    starting = state == 'starting'
    # Ground plane and reflected portal light.
    polygon((30, 167, 139, 138, 221, 168, 111, 199), '#17131f')
    polygon((60, 170, 143, 149, 197, 169, 114, 192), '#241b34' if lit else '#1e1928')
    polygon((89, 171, 144, 158, 175, 169, 119, 183), '#3e255f' if lit else '#27202f')
    # A rectangular obsidian gate, with visible right-hand depth.
    polygon((159, 18, 181, 29, 181, 170, 159, 180), '#181421')
    rect(55, 18, 105, 162, '#0c0a12')
    # Individually cut blocks, with bevels and original irregular surface marks.
    for row in range(6):
        for col in range(4):
            if row not in (0, 5) and col not in (0, 3):
                continue
            x, y = 55 + col*26, 18 + row*27
            tone = ('#332943', '#292237', '#3c2e4d')[(row+col) % 3]
            rect(x+1, y+1, 24, 25, tone)
            rect(x+3, y+3, 20, 3, '#514064')
            rect(x+4, y+20, 18, 3, '#1e192c')
            rect(x+8+(row % 2)*4, y+10, 7, 3, '#604571')
    # Recessed portal opening: an empty gate stays empty, never fake-online.
    rect(82, 45, 51, 107, '#120f1a')
    if lit or starting:
        rect(82, 45, 51, 107, '#4e2579' if lit else '#25172e')
        if lit:
            for row in range(18):
                for col in range(8):
                    dx, dy = col-3.5, (row-8.5)*.5
                    swirl = math.atan2(dy, dx) + math.hypot(dx, dy)*1.45
                    shade = min(4, int((math.sin(swirl)+1)*2.5))
                    color = ('#552580', '#693095', '#8242b3', '#9c56cb', '#b86cde')[shade]
                    rect(84+col*6, 46+row*5.8, 6, 5.8, color)
            rect(83, 46, 2, 105, '#d7a1ff')
            rect(131, 46, 2, 105, '#9250d0')
            for x, y, size in ((38, 58, 3), (185, 81, 4), (155, 6, 3),
                               (202, 128, 3), (39, 126, 2), (145, 101, 3)):
                rect(x, y, size, size, '#c799ff')
    # Flint shard and steel striker: original silhouettes beside the threshold.
    polygon((28, 147, 42, 130, 54, 137, 58, 153, 40, 163), '#9b9aae')
    polygon((28, 147, 42, 130, 43, 145, 40, 163), '#626174')
    polygon((43, 145, 54, 137, 58, 153), '#d3ccdf')
    polygon((52, 161, 66, 142, 83, 146, 89, 159, 79, 175, 63, 176), '#bbc3d4')
    polygon((62, 161, 69, 152, 77, 153, 79, 160, 74, 167, 65, 168), '#211b2c')
    polygon((52, 161, 63, 176, 79, 175, 76, 181, 60, 181, 48, 165), '#636a7c')
    if starting or lit:
        for x, y, size in ((67, 133, 4), (75, 119, 3), (57, 123, 2), (91, 134, 3)):
            rect(x, y, size, size, SPARK)
        polygon((77, 141, 79, 130, 83, 137, 93, 132, 87, 142, 90, 149, 81, 145, 74, 151), '#ffd59a')
    return shapes


def portal_mark():
    """Bold 64 x 64 small-size mark: obsidian, portal, ignition spark."""
    return [
        ('polygon', (10, 3, 49, 3, 56, 10, 56, 61, 10, 61), '#21182f'),
        ('polygon', (10, 3, 49, 3, 49, 61, 10, 61), '#59406e'),
        ('polygon', (15, 8, 44, 8, 44, 56, 15, 56), '#191122'),
        ('polygon', (21, 14, 39, 14, 39, 50, 21, 50), '#8346bf'),
        ('polygon', (24, 17, 35, 17, 35, 47, 24, 47), '#bb78ed'),
        ('polygon', (21, 14, 24, 14, 24, 50, 21, 50), '#e2afff'),
        ('polygon', (42, 34, 47, 42, 58, 36, 52, 47, 59, 54, 47, 52, 39, 62, 41, 50, 32, 45, 43, 43), '#ffb56b'),
        ('polygon', (46, 44, 50, 47, 46, 51, 43, 48), '#fff0cd'),
    ]


def draw_shapes(canvas, shapes, x, y, width, height, viewbox=(240, 200), tag='portal-art'):
    scale = min(width/viewbox[0], height/viewbox[1])
    dx, dy = x+(width-viewbox[0]*scale)/2, y+(height-viewbox[1]*scale)/2
    for kind, points, color in shapes:
        transformed = [value*scale + (dx if index % 2 == 0 else dy)
                       for index, value in enumerate(points)]
        canvas.create_polygon(*transformed, fill=color, outline='', tags=tag)


def apply_window_icon(window):
    """Keep the PhotoImage alive on its Tk interpreter; missing icon isn't fatal."""
    from pathlib import Path
    import tkinter as tk
    path = Path(__file__).resolve().parent / 'branding/flintdock-icon.png'
    if path.is_file():
        try:
            window._flintdock_icon = tk.PhotoImage(master=window, file=str(path))
            window.iconphoto(True, window._flintdock_icon)
        except tk.TclError:
            pass
