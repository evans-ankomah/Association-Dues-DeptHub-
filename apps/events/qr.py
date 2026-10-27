"""QR codes as inline SVG, drawn with reportlab (already a dependency for receipts)."""

from reportlab.graphics import renderSVG
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing


def qr_svg(data, size=280):
    widget = QrCodeWidget(data, barLevel="M")
    x0, y0, x1, y1 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    drawing.add(widget)
    svg = renderSVG.drawToString(drawing)
    # Drop the XML prolog and DOCTYPE so the markup can sit inside an HTML page.
    return svg[svg.index("<svg") :]
