"""FlintDock: original Obsidian / Portal / Ignition design system."""
import tkinter as tk
from portal_art import portal_scene, draw_shapes

BG = '#0e0b14'
SIDEBAR = '#120e1b'
PANEL = '#17121f'
CARD = '#20192b'
INPUT = '#100c18'
LINE = '#3d304d'
FG = '#f6efff'
FG_DIM = '#c4b6d1'
FG_FAINT = '#a997ba'
GREEN = '#72e2ad'
BLUE = '#9bc4f7'
AMBER = '#ffc783'
RED = '#f28f91'
PURPLE = '#c79dff'
PORTAL = PURPLE
IGNITION = '#ffb56b'
IGNITION_HOVER = '#ffd09a'
FOCUS = '#e0b9ff'
SELECTED = '#3b2654'
HOVER = '#30233e'


def icon(canvas, name, x=4, y=4, size=20, color=FG_DIM):
    """Small consistent line icons, independent of platform symbol fonts."""
    def line(*points):
        canvas.create_line(*[v * size / 24 + (x if i % 2 == 0 else y)
                             for i, v in enumerate(points)], fill=color, width=1.6,
                           capstyle='round', joinstyle='round')
    def rect(a, b, c, d):
        canvas.create_rectangle(x+a*size/24, y+b*size/24, x+c*size/24, y+d*size/24,
                                outline=color, width=1.5)
    def oval(a, b, c, d):
        canvas.create_oval(x+a*size/24, y+b*size/24, x+c*size/24, y+d*size/24,
                           outline=color, width=1.5)
    if name == 'dashboard':
        for a, b in ((2, 2), (14, 2), (2, 14), (14, 14)):
            rect(a, b, a+8, b+8)
    elif name == 'console':
        rect(1, 3, 23, 21); line(5, 8, 9, 12, 5, 16); line(12, 16, 18, 16)
    elif name == 'players':
        oval(8, 2, 16, 10); line(4, 22, 4, 18, 7, 14, 17, 14, 20, 18, 20, 22)
    elif name in ('history', 'schedule'):
        oval(2, 2, 22, 22); line(12, 6, 12, 12, 17, 15)
    elif name == 'mods':
        line(12, 1, 23, 7, 23, 18, 12, 24, 1, 18, 1, 7, 12, 1)
        line(1, 7, 12, 13, 23, 7); line(12, 13, 12, 24)
    elif name == 'backups':
        rect(3, 8, 21, 22); rect(1, 3, 23, 8); line(9, 13, 15, 13)
    elif name == 'settings':
        for a, b in ((5, 7), (12, 16), (19, 10)):
            line(a, 2, a, b-3); line(a, b+3, a, 22); oval(a-3, b-3, a+3, b+3)
    elif name == 'update':
        line(12, 16, 12, 2, 7, 7); line(12, 2, 17, 7)
        line(3, 15, 3, 22, 21, 22, 21, 15)


def cube(canvas, x, y, size=26, top=GREEN, left='#359474', right='#246b57'):
    half = size / 2
    canvas.create_polygon(x, y, x+size, y-half, x+size*2, y, x+size, y+half,
                           fill=top, outline='')
    canvas.create_polygon(x, y, x+size, y+half, x+size, y+size*1.5, x, y+size,
                           fill=left, outline='')
    canvas.create_polygon(x+size, y+half, x+size*2, y, x+size*2, y+size,
                           x+size, y+size*1.5, fill=right, outline='')


class WorldArtwork(tk.Canvas):
    """State-driven gate. No idle animation, redraw loop, or external textures."""
    def __init__(self, master, **kwargs):
        self.state = 'offline'
        super().__init__(master, bg=kwargs.pop('bg', CARD), highlightthickness=0, **kwargs)
        self.bind('<Configure>', self.draw)

    def set_state(self, state):
        normalized = {'Running': 'online', 'Starting': 'starting', 'Stopping': 'stopping'}.get(state, 'offline')
        if normalized != self.state:
            self.state = normalized
            self.draw()

    def draw(self, _event=None):
        self.delete('portal-art')
        w, h = self.winfo_width(), self.winfo_height()
        if w > 1 and h > 1:
            draw_shapes(self, portal_scene(self.state), 0, 0, w, h)


class FlowRow(tk.Frame):
    """Wrap small controls to the next row instead of clipping at narrow widths."""
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.items = []
        self._pending_layout = False
        self.bind('<Configure>', self.schedule)

    def add(self, widget):
        self.items.append(widget)
        widget.bind('<Configure>', self.schedule, add='+')
        self.schedule()
        return widget

    def schedule(self, _event=None):
        if not self._pending_layout:
            self._pending_layout = True
            self.after_idle(self.layout)

    def layout(self):
        self._pending_layout = False
        if not self.winfo_exists():
            return
        width = max(1, self.winfo_width())
        y = occupied = row_height = 0
        for widget in self.items:
            item_width = widget.winfo_reqwidth()
            item_height = widget.winfo_reqheight()
            needed = item_width+8
            if occupied and occupied+needed > width:
                y += row_height+4
                occupied = row_height = 0
            widget.place(x=occupied, y=y, width=item_width, height=item_height)
            occupied += needed
            row_height = max(row_height, item_height)
        self.configure(height=y+row_height+4)
