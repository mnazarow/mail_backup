"""
Минимальный генератор QR-кода (ISO/IEC 18004) без внешних зависимостей.

Нужен ровно для одного: показать ссылку ``otpauth://`` при включении
двухфакторной аутентификации, чтобы её можно было отсканировать приложением
(Яндекс Ключ, Google Authenticator, FreeOTP и т. п.). Поэтому поддержан только
байтовый режим — его хватает для любой строки. Размер (версия 1–40) выбирается
автоматически, маска — по штрафным правилам стандарта.

Результат — матрица модулей или готовый SVG. Отдельная библиотека (segno,
qrcode) не подключается намеренно: набор зависимостей сервиса зафиксирован в
requirements.txt, и тянуть новую ради одной картинки незачем.
"""
from __future__ import annotations

from typing import List

# Число кодовых слов коррекции на блок и число блоков: [уровень][версия].
# Уровни: 0 — L (7 %), 1 — M (15 %), 2 — Q (25 %), 3 — H (30 %).
_ECC_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28,
     28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
     26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30,
     28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28,
     30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
_NUM_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8,
     8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
     17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20,
     23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25,
     25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)
#: Биты уровня коррекции в служебной информации о формате.
_FORMAT_BITS = (1, 0, 3, 2)
_LEVELS = {"L": 0, "M": 1, "Q": 2, "H": 3}


def _num_raw_data_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        numalign = ver // 7 + 2
        result -= (25 * numalign - 10) * numalign - 55
        if ver >= 7:
            result -= 36
    return result


def _num_data_codewords(ver: int, ecl: int) -> int:
    return _num_raw_data_modules(ver) // 8 - _ECC_PER_BLOCK[ecl][ver] * _NUM_BLOCKS[ecl][ver]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> List[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: List[int], divisor: List[int]) -> List[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(ver: int) -> List[int]:
    if ver == 1:
        return []
    size = ver * 4 + 17
    numalign = ver // 7 + 2
    step = (ver * 8 + numalign * 3 + 5) // (numalign * 4 - 4) * 2
    return [6] + [size - 7 - i * step for i in reversed(range(numalign - 1))]


class _Matrix:
    def __init__(self, ver: int) -> None:
        self.ver = ver
        self.size = ver * 4 + 17
        n = self.size
        self.dark = [[False] * n for _ in range(n)]
        self.func = [[False] * n for _ in range(n)]

    def set_func(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.func[y][x] = True

    # -- служебные узоры ------------------------------------------------------
    def draw_function_patterns(self) -> None:
        n = self.size
        for i in range(n):                      # синхронизирующие линии
            self.set_func(6, i, i % 2 == 0)
            self.set_func(i, 6, i % 2 == 0)
        self._finder(3, 3)
        self._finder(n - 4, 3)
        self._finder(3, n - 4)
        pos = _alignment_positions(self.ver)
        last = len(pos) - 1
        for i, px in enumerate(pos):
            for j, py in enumerate(pos):
                if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0):
                    continue
                self._alignment(px, py)
        self.draw_format(0, 0)                  # резервируем место; настоящие биты — позже
        self._version()

    def _finder(self, cx: int, cy: int) -> None:
        n = self.size
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < n and 0 <= y < n:
                    dist = max(abs(dx), abs(dy))
                    self.set_func(x, y, dist not in (2, 4))

    def _alignment(self, cx: int, cy: int) -> None:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self.set_func(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(self, ecl: int, mask: int) -> None:
        data = (_FORMAT_BITS[ecl] << 3) | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412
        bit = lambda i: ((bits >> i) & 1) != 0  # noqa: E731
        n = self.size
        for i in range(0, 6):
            self.set_func(8, i, bit(i))
        self.set_func(8, 7, bit(6))
        self.set_func(8, 8, bit(7))
        self.set_func(7, 8, bit(8))
        for i in range(9, 15):
            self.set_func(14 - i, 8, bit(i))
        for i in range(0, 8):
            self.set_func(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_func(8, n - 15 + i, bit(i))
        self.set_func(8, n - 8, True)           # «тёмный модуль»

    def _version(self) -> None:
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (self.ver << 12) | rem
        n = self.size
        for i in range(18):
            dark = ((bits >> i) & 1) != 0
            a, b = n - 11 + i % 3, i // 3
            self.set_func(a, b, dark)
            self.set_func(b, a, dark)

    # -- данные -------------------------------------------------------------
    def draw_codewords(self, data: List[int]) -> None:
        n = self.size
        i = 0
        total = len(data) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.func[y][x] and i < total:
                        self.dark[y][x] = ((data[i >> 3] >> (7 - (i & 7))) & 1) != 0
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        n = self.size
        for y in range(n):
            for x in range(n):
                if self.func[y][x]:
                    continue
                if mask == 0:
                    inv = (x + y) % 2 == 0
                elif mask == 1:
                    inv = y % 2 == 0
                elif mask == 2:
                    inv = x % 3 == 0
                elif mask == 3:
                    inv = (x + y) % 3 == 0
                elif mask == 4:
                    inv = (x // 3 + y // 2) % 2 == 0
                elif mask == 5:
                    inv = x * y % 2 + x * y % 3 == 0
                elif mask == 6:
                    inv = (x * y % 2 + x * y % 3) % 2 == 0
                else:
                    inv = ((x + y) % 2 + x * y % 3) % 2 == 0
                if inv:
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        n = self.size
        grid = self.dark
        score = 0
        lines = [grid[y] for y in range(n)] + [[grid[y][x] for y in range(n)] for x in range(n)]
        pattern_a = [True, False, True, True, True, False, True, False, False, False, False]
        pattern_b = pattern_a[::-1]
        for line in lines:
            # правило 1: серии одного цвета длиной 5+
            run_color, run_len = line[0], 1
            for v in line[1:]:
                if v == run_color:
                    run_len += 1
                else:
                    if run_len >= 5:
                        score += 3 + (run_len - 5)
                    run_color, run_len = v, 1
            if run_len >= 5:
                score += 3 + (run_len - 5)
            # правило 3: узоры, похожие на поисковый (1:1:3:1:1 со светлой каймой)
            for i in range(n - 10):
                seg = line[i:i + 11]
                if seg == pattern_a or seg == pattern_b:
                    score += 40
        # правило 2: блоки 2×2 одного цвета
        for y in range(n - 1):
            for x in range(n - 1):
                c = grid[y][x]
                if c == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                    score += 3
        # правило 4: баланс тёмных и светлых модулей
        dark = sum(sum(1 for v in row if v) for row in grid)
        total = n * n
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        score += max(0, k) * 10
        return score


def _encode_codewords(data: bytes, ver: int, ecl: int) -> List[int]:
    cap_bits = _num_data_codewords(ver, ecl) * 8
    bits: List[int] = []

    def append(value: int, length: int) -> None:
        for i in reversed(range(length)):
            bits.append((value >> i) & 1)

    append(0b0100, 4)                            # байтовый режим
    append(len(data), 8 if ver <= 9 else 16)
    for b in data:
        append(b, 8)
    append(0, min(4, cap_bits - len(bits)))      # терминатор
    append(0, (-len(bits)) % 8)
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(codewords) < cap_bits // 8:
        codewords.append(pad)
        pad ^= 0xEC ^ 0x11
    return codewords


def _add_ecc_and_interleave(data: List[int], ver: int, ecl: int) -> List[int]:
    numblocks = _NUM_BLOCKS[ecl][ver]
    ecclen = _ECC_PER_BLOCK[ecl][ver]
    raw = _num_raw_data_modules(ver) // 8
    numshort = numblocks - raw % numblocks
    shortlen = raw // numblocks
    divisor = _rs_divisor(ecclen)
    blocks = []
    k = 0
    for i in range(numblocks):
        take = shortlen - ecclen + (0 if i < numshort else 1)
        dat = data[k:k + take]
        k += take
        ecc = _rs_remainder(dat, divisor)
        if i < numshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - ecclen or j >= numshort:
                result.append(blk[i])
    return result


def encode(text: str, level: str = "M") -> List[List[bool]]:
    """Строка → матрица модулей (True — тёмный)."""
    data = text.encode("utf-8")
    ecl = _LEVELS.get(level.upper(), 1)
    for ver in range(1, 41):
        ccbits = 8 if ver <= 9 else 16
        if len(data) < (1 << ccbits) and 4 + ccbits + len(data) * 8 <= _num_data_codewords(ver, ecl) * 8:
            break
    else:
        raise ValueError("Слишком длинная строка для QR-кода")
    codewords = _add_ecc_and_interleave(_encode_codewords(data, ver, ecl), ver, ecl)
    best, best_score = None, None
    for mask in range(8):
        m = _Matrix(ver)
        m.draw_function_patterns()
        m.draw_codewords(codewords)
        m.apply_mask(mask)
        m.draw_format(ecl, mask)
        score = m.penalty()
        if best_score is None or score < best_score:
            best, best_score = m, score
    return best.dark


def to_svg(text: str, level: str = "M", scale: int = 6, border: int = 4) -> str:
    """QR-код в виде SVG (тёмные модули — одним путём, фон белый)."""
    grid = encode(text, level)
    n = len(grid)
    total = (n + border * 2)
    parts = []
    for y in range(n):
        for x in range(n):
            if grid[y][x]:
                parts.append(f"M{x + border},{y + border}h1v1h-1z")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
            f'width="{total * scale}" height="{total * scale}" shape-rendering="crispEdges">'
            f'<rect width="100%" height="100%" fill="#fff"/>'
            f'<path d="{"".join(parts)}" fill="#000"/></svg>')
