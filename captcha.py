"""Lectura local de los seis caracteres blancos del CAPTCHA visual de EDUS."""

from collections import Counter
from functools import lru_cache
from string import ascii_uppercase, digits

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication, QImage, QPainter


def white_pixels(image, threshold=224, inset=0):
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    data = gray.constBits()
    stride = gray.bytesPerLine()
    return {
        (x, y)
        for y in range(inset, gray.height() - inset)
        for x in range(inset, gray.width() - inset)
        if data[y * stride + x] >= threshold
    }


def components(pixels):
    while pixels:
        glyph = {pixels.pop()}
        pending = list(glyph)
        while pending:
            x, y = pending.pop()
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, -1), (1, -1), (-1, 1)):
                neighbor = (x + dx, y + dy)
                if neighbor in pixels:
                    pixels.remove(neighbor)
                    glyph.add(neighbor)
                    pending.append(neighbor)
        yield glyph


def glyph_shape(pixels):
    x_values, y_values = zip(*pixels)
    left, top = min(x_values), min(y_values)
    width, height = max(x_values) - left + 1, max(y_values) - top + 1
    shape = {
        (x, y)
        for y in range(32)
        for x in range(24)
        if (left + (2 * x + 1) * width // 48, top + (2 * y + 1) * height // 64) in pixels
    }
    return shape, width / height


@lru_cache(maxsize=1)
def templates():
    result = {}
    for char in ascii_uppercase + digits:
        samples = []
        for family in ("Arial", "Verdana", "Segoe UI", "Tahoma"):
            image = QImage(64, 64, QImage.Format.Format_RGB32)
            image.fill(Qt.GlobalColor.black)
            painter = QPainter(image)
            font = QFont(family)
            font.setPixelSize(34)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(4, 44, char)
            painter.end()
            samples.append(glyph_shape(white_pixels(image)))
        result[char] = samples
    return result


def rank_glyph(pixels):
    shape, aspect = glyph_shape(pixels)
    return sorted(
        (min(len(shape ^ sample) / len(shape | sample) + 0.25 * abs(aspect - ratio)
             for sample, ratio in samples), char)
        for char, samples in templates().items()
    )


def read_mask(image, threshold, margin):
    letters = []
    for glyph in components(white_pixels(image, threshold, inset=2)):
        if len(glyph) < 55:
            continue
        xs, ys = zip(*glyph)
        if not (4 <= max(xs) - min(xs) <= 35 and 16 <= max(ys) - min(ys) <= 32):
            return None
        letters.append((min(xs), glyph))
    if len(letters) != 6:
        return None
    code = []
    for _, glyph in sorted(letters, key=lambda letter: letter[0]):
        (score, char), (runner_up, _) = rank_glyph(glyph)[:2]
        if score > 0.32 or runner_up - score < margin:
            return None
        code.append(char)
    return "".join(code)


def read_captcha(png):
    image = QImage.fromData(png, "PNG")
    width, height = image.width(), image.height()
    if not QGuiApplication.instance() or not (130 <= width <= 1080 and 3.55 * height <= width <= 4.15 * height):
        return None
    if not (267 <= width <= 273 and 68 <= height <= 72):
        image = image.scaled(270, 70, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    direct = read_mask(image, 224, 0.045)
    if direct:
        return direct
    votes = Counter(
        code for threshold in (205, 224, 245) if (code := read_mask(image, threshold, 0.03))
    )
    if len(votes) == 1:
        code, count = votes.most_common(1)[0]
        if count >= 2:
            return code
    return None
