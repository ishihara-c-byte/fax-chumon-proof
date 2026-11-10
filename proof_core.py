"""FAX注文書 校正ロジック

中面PDF(メニューパンフレット中面)とカレンダーを「正」として、
FAX注文書PDF(1ページ=1店舗)の以下を照合する。
  ① 年・月・期間(例: 12/1〜12/31)
  ② 曜日に合った日付が入っているか / 店休日
  ③ 旗日の文字(フェア名・プレミアム・スペシャル、贅沢版は混ぜご飯)
  ④ イベントWeekの表記と期間
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass, field

import pdfplumber

# ---------------------------------------------------------------- 共通

WEEKDAYS = "月火水木金土日"


def norm(s: str) -> str:
    """比較用に正規化(全角半角・空白・改行の揺れを吸収)"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("~", "〜").replace("～", "〜")
    return re.sub(r"\s+", "", s)


def color_kind(c) -> str:
    """文字の塗り色を black / white / gray / other に分類"""
    if c is None:
        return "black"
    if isinstance(c, (int, float)):
        c = (c,)
    c = tuple(float(v) for v in c)
    if len(c) == 4:  # CMYK -> 明るさ
        k = 1 - min(1.0, c[3] + max(c[:3]))
        lum = k
    elif len(c) == 3:
        lum = sum(c) / 3
    elif len(c) == 1:
        lum = c[0]
    else:
        return "other"
    if lum > 0.95:
        return "white"
    if lum < 0.15:
        return "black"
    if len(c) == 3 and max(c) - min(c) > 0.15:
        return "other"  # 有彩色
    return "gray"


def group_chars_to_numbers(chars, max_gap=None):
    """隣り合う数字1文字ずつを数字トークンにまとめる"""
    uniq = []
    for c in chars:  # 同じ位置に重ねて置かれた同じ文字は1つに(描画順は手前のものを残す)
        ck = color_kind(c["non_stroking_color"])
        for u in uniq:
            if (u["text"] == c["text"] and abs(u["x0"] - c["x0"]) < 1.5
                    and abs(u["top"] - c["top"]) < 1.5 and color_kind(u["non_stroking_color"]) == ck):
                if c.get("_order", 0) > u.get("_order", 0):
                    uniq[uniq.index(u)] = c
                break
        else:
            uniq.append(c)
    chars = sorted(uniq, key=lambda c: (round(c["top"]), color_kind(c["non_stroking_color"]), c["x0"]))
    tokens = []
    for c in chars:
        ck = color_kind(c["non_stroking_color"])
        if tokens:
            t = tokens[-1]
            if (abs(t["top"] - c["top"]) < 2 and t["color"] == ck
                    and -1.0 <= c["x0"] - t["x1"] < (max_gap if max_gap is not None else c["size"] * 0.4)):
                t["text"] += c["text"]
                t["x1"] = c["x1"]
                t["order"] = max(t["order"], c.get("_order", 0))
                continue
        tokens.append({"text": c["text"], "x0": c["x0"], "x1": c["x1"], "size": c["size"],
                       "order": c.get("_order", 0),
                       "top": c["top"], "bottom": c["bottom"], "color": ck})
    return tokens


def cluster(values, tol):
    """近い値をまとめ、代表値(最小値)のリストを返す"""
    out = []
    for v in sorted(values):
        if not out or v - out[-1][-1] > tol:
            out.append([v])
        else:
            out[-1].append(v)
    return [g[0] for g in out]


def vertical_text(chars):
    """縦書きの文字を読む(右の列から、上から下へ)"""
    if not chars:
        return ""
    cols = cluster([c["x0"] for c in chars], tol=4)
    groups = {x: [] for x in cols}
    for c in chars:
        x = max(v for v in cols if v <= c["x0"] + 0.01)
        groups[x].append(c)
    out = ""
    for x in sorted(groups, reverse=True):
        out += "".join(c["text"] for c in sorted(groups[x], key=lambda c: c["top"]))
    return out


# ---------------------------------------------------------------- 中面

@dataclass
class DayInfo:
    day: int
    flag: str = ""          # "スタミナフェア" / "プレミアム" / "スペシャル" / ""
    kind: str = ""          # "fair" / "premium" / "special" / ""
    rice: str = ""          # 贅沢弁当の「混ぜご飯」/「ふりかけ」/ ""
    closed: bool = False
    label: str = ""         # 旗日以外の表記(例: 中華)。日付は黒地にしない


@dataclass
class MenuMaster:
    month: int | None
    days: dict[int, DayInfo] = field(default_factory=dict)
    event_range: tuple[int, int] | None = None
    warnings: list[str] = field(default_factory=list)


# 中面のマス背景色(フェア=オレンジ、スペシャル=ピンク、プレミアム=黄)
KIND_COLORS = {
    "fair": (0.99, 0.897, 0.808),
    "special": (0.982, 0.862, 0.886),
    "premium": (1.0, 0.961, 0.685),
}
KIND_LABEL = {"premium": "プレミアム", "special": "スペシャル"}


def _rgb(c):
    if c is None or isinstance(c, (int, float)) or len(c) not in (3, 4):
        return None
    if len(c) == 4:
        cc, m, y, k = c
        return ((1 - cc) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return tuple(float(v) for v in c)


def _nearest_kind(c):
    """マスの背景色から旗日の種類を判定(黄=プレミアム、ピンク=スペシャル、オレンジ=フェア)"""
    rgb = _rgb(c)
    if rgb is None:
        return None
    r, g, b = rgb
    if max(rgb) - min(rgb) < 0.05 or min(rgb) < 0.4:
        return None  # 白・グレー・濃い色は対象外
    if g < b - 0.02:
        return "special"
    if g - b > 0.2:
        return "premium"
    if r - b > 0.1:
        return "fair"
    return None


def _undouble(t: str) -> str:
    """太字表現で2回ずつ重なった文字(中中華華)を1回に"""
    if len(t) >= 2 and len(t) % 2 == 0 and all(t[i] == t[i + 1] for i in range(0, len(t), 2)):
        return t[::2]
    return t


def parse_menu(path_or_file) -> MenuMaster:
    with pdfplumber.open(path_or_file) as pdf:
        page = pdf.pages[0]
        words = page.extract_words(extra_attrs=["size", "non_stroking_color"])
        rects = page.rects

        # 月(いちばん大きい数字)
        month = None
        nums = [w for w in words if re.fullmatch(r"\d{1,2}", w["text"])]
        if nums:
            big = max(nums, key=lambda w: w["size"])
            if big["size"] > 40:
                month = int(big["text"])

        # 日付マス = 塗りのある四角のうち、いちばん多い高さのもの
        # (月によって5週・6週でマスの高さが変わるため、高さは固定しない)
        cand_cells = []
        for r in rects + [c for c in page.curves if c.get("fill")]:
            w, h = r["x1"] - r["x0"], r["bottom"] - r["top"]
            if r.get("fill") and 60 < w < 230 and 60 < h < 230:
                cand_cells.append(r)
        heights = {}
        for r in cand_cells:
            hk = round(r["bottom"] - r["top"])
            heights[hk] = heights.get(hk, 0) + 1
        cell_h = max(heights, key=lambda k: sum(v for kk, v in heights.items() if abs(kk - k) <= 2)) if heights else 0
        # 月初の週は高さが半分のマスもあるため、高さの4割〜同じ高さまでを対象にする
        cells = [r for r in cand_cells if cell_h * 0.4 <= (r["bottom"] - r["top"]) <= cell_h + 4]
        cells.sort(key=lambda r: -(r["bottom"] - r["top"]))  # 大きいマスを優先

        master = MenuMaster(month=month)
        boxes = {}  # day -> (x0, top, x1, bottom)
        for r in cells:
            x0, top, x1, bottom = r["x0"], r["top"], r["x1"], r["bottom"]
            # マス左上の日付(後ろに文字がくっついている場合もある)
            cand = [w for w in words
                    if re.match(r"\d{1,2}(?!\d)", w["text"])
                    and x0 - 2 <= w["x0"] <= x0 + 40 and top - 2 <= w["top"] <= top + 30 and w["size"] >= 9]
            if not cand:
                continue
            day = int(re.match(r"\d{1,2}", min(cand, key=lambda w: (w["top"], w["x0"]))["text"]).group())
            info = master.days.setdefault(day, DayInfo(day))
            if day not in boxes or (x1 - x0) * (bottom - top) >= (boxes[day][2] - boxes[day][0]) * (boxes[day][3] - boxes[day][1]) - 1:
                boxes[day] = (x0, top, x1, bottom)
            k = _nearest_kind(r.get("non_stroking_color"))
            if k:
                info.kind = k

        for day, (x0, top, x1, bottom) in boxes.items():
            info = master.days[day]
            inside = [w for w in words if x0 <= w["x0"] < x1 and top <= w["top"] < bottom]
            white = [w for w in inside if color_kind(w["non_stroking_color"]) == "white"]
            wt = [w["text"] for w in white]
            if any(t in ("混ぜ", "混ぜご飯") for t in wt):
                info.rice = "混ぜご飯"
            elif any(w["text"] in ("ふり", "ふりかけ") and w["size"] < 7.5 for w in inside):
                info.rice = "ふりかけ"
            # 旗日以外の大きな表記(例: 中華)
            badges = [w for w in inside if w["size"] >= 11 and not re.search(r"\d", w["text"])
                      and color_kind(w["non_stroking_color"]) != "white"
                      and "店休" not in w["text"] and "休" not in _undouble(w["text"])]
            if badges:
                badges.sort(key=lambda w: (round(w["top"]), w["x0"]))
                info.label = norm("".join(_undouble(w["text"]) for w in badges))
            if any("店休日" in w["text"] for w in inside):
                info.closed = True
            if info.kind == "fair":
                name_words = [w for w in white
                              if not re.fullmatch(r"\d+", w["text"])
                              and w["text"] not in ("今月のフェア", "混ぜ", "ご飯")
                              and w["size"] >= 7.5]
                name_words.sort(key=lambda w: (round(w["top"]), w["x0"]))
                info.flag = norm("".join(w["text"] for w in name_words))
                if not info.flag:
                    master.warnings.append(f"{day}日: フェアの色ですがフェア名が読み取れません")
            elif info.kind in KIND_LABEL:
                info.flag = KIND_LABEL[info.kind]

        # イベントWeek = 複数マスにまたがる太枠(週をまたぐ場合は枠が2つ)
        cell_w = min((b[2] - b[0]) for b in boxes.values()) if boxes else 100
        ev_days = set()
        for r in rects + page.curves:
            w, h = r["x1"] - r["x0"], r["bottom"] - r["top"]
            if r.get("stroke") and w > cell_w * 1.8 and abs(h - cell_h) < cell_h * 0.15:
                inside = [d for d, (x0, top, x1, bottom) in boxes.items()
                          if x0 >= r["x0"] - 5 and x1 <= r["x1"] + 5
                          and top >= r["top"] - 5 and bottom <= r["bottom"] + 5]
                if len(inside) >= 2:
                    ev_days.update(inside)
        if ev_days:
            master.event_range = (min(ev_days), max(ev_days))

        if not master.days:
            master.warnings.append("中面から日付マスを読み取れませんでした")
        elif month:
            import datetime as _dt
            # 年が分からないので、2月は28/29日のどちらでもよしとする
            need = 28 if month == 2 else calendar.monthrange(2001, month)[1]
            missing = [d for d in range(1, need + 1) if d not in master.days]
            if missing:
                master.warnings.append(
                    "中面から読み取れなかった日付があります: " + "・".join(map(str, missing))
                    + "日(表の内容を確認してください)")
        return master


# ---------------------------------------------------------------- FAX注文書

@dataclass
class FaxCell:
    day: int
    weekday: str
    row: int
    hidden: bool = False      # 斜線で消されたマス
    black_box: bool = False   # 日付が黒地(白抜き数字)
    gray: bool = False        # グレーの日付
    flag: str = ""
    rice: str = ""
    closed: bool = False
    event_text: str = ""


@dataclass
class FaxPage:
    page_no: int
    store_no: str = ""
    store_name: str = ""
    year: int | None = None
    month: int | None = None
    period: str = ""
    columns: list[str] = field(default_factory=list)
    zeitaku: bool = False
    cells: list[FaxCell] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def label(self):
        """表示用の店舗名(番号が重ならないように)"""
        name = self.store_name
        if not name:
            return self.store_no
        if self.store_no and not name.startswith(self.store_no):
            return f"{self.store_no}_{name}"
        return name

    @property
    def variant(self):
        v = "贅沢版" if self.zeitaku else "通常版"
        if len(self.columns) > 5:
            v += "(土日祝あり)"
        return v


def _region(img, scale, x0, top, x1, bottom):
    h, w = img.shape
    a, b = max(0, int(top * scale)), min(h, int(bottom * scale) + 1)
    c, d = max(0, int(x0 * scale)), min(w, int(x1 * scale) + 1)
    return img[a:b, c:d]


def parse_fax_page(page, page_no, img=None, scale=2.0) -> FaxPage:
    """img: ページを描画したグレースケール画像(numpy)。隠れた文字の除外に使う"""
    fp = FaxPage(page_no=page_no)
    chars = page.chars
    for i, c in enumerate(chars):  # PDF内の描画順(後のものほど手前)
        c["_order"] = i

    def visible_dark(c):  # 黒い文字が実際に見えているか
        if img is None:
            return True
        r = _region(img, scale, c["x0"], c["top"], c["x1"], c["bottom"])
        return r.size > 0 and r.min() < 110

    def visible_white(c):  # 白抜き文字が黒地の上に見えているか
        if img is None:
            return True
        r = _region(img, scale, c["x0"], c["top"], c["x1"], c["bottom"])
        return r.size > 0 and r.mean() < 150
    text = page.extract_text() or ""

    m = re.search(r"店舗番号\s*(\d+)", text)
    fp.store_no = m.group(1) if m else ""
    first = text.splitlines()[0] if text else ""
    fp.store_name = re.sub(r"\s*\d{3}-\d{3,4}\s*$", "", first).strip()
    if "注文内容" in fp.store_name:
        fp.store_name = ""

    # 日付の数字(大きい数字) … 表(「ヘルシー」等)より下にある2桁以内の数字
    # 左上の大きな月の数字より下を日付エリアとする
    digits_all = [c for c in chars if c["text"].isdigit()]
    month_char = max(digits_all, key=lambda c: c["size"]) if digits_all else None
    table_top = (month_char["bottom"] + 15) if month_char else 0
    digit_chars = [c for c in chars if c["text"].isdigit() and c["size"] > 16 and c["top"] > table_top]
    tokens = [t for t in group_chars_to_numbers(digit_chars) if len(t["text"]) <= 2]

    # 同じ位置に重なっている数字は1つにまとめ、いちばん手前の色を見た目の色とする
    base = []
    for t in sorted(tokens, key=lambda t: t["order"]):
        for b in base:
            same_row = abs(b["top"] - t["top"]) < 4
            overlap = (abs(b["x0"] - t["x0"]) < 4
                       or (t["x0"] >= b["x0"] - 2 and t["x1"] <= b["x1"] + 2)
                       or (b["x0"] >= t["x0"] - 2 and b["x1"] <= t["x1"] + 2))
            if same_row and overlap:
                if len(t["text"]) > len(b["text"]):
                    b.update(text=t["text"], x0=t["x0"], x1=t["x1"])
                b["color"] = t["color"]
                b["order"] = t["order"]
                break
        else:
            base.append(dict(t))
    base.sort(key=lambda t: (t["top"], t["x0"]))
    head_bottom = table_top
    if not base:
        fp.warnings.append("日付が読み取れません")
        return fp

    col_x = cluster([t["x0"] for t in base], tol=25)
    row_y = cluster([t["top"] for t in base], tol=10)
    # 曜日見出しは文字データではないため、列数から判定(月曜始まり)
    if len(col_x) in (5, 6, 7):
        fp.columns = list(WEEKDAYS[:len(col_x)])
    else:
        fp.columns = list(WEEKDAYS[:len(col_x)])
        fp.warnings.append(f"日付の列数が想定外です({len(col_x)}列)")
    col_w = (col_x[1] - col_x[0]) if len(col_x) > 1 else 110
    row_h = (row_y[1] - row_y[0]) if len(row_y) > 1 else 110

    # 斜線(マスを消している線)
    diagonals = [(l["x0"], l["top"], l["x1"], l["bottom"]) for l in page.lines
                 if abs(l["x1"] - l["x0"]) > col_w * 0.6 and abs(l["bottom"] - l["top"]) > row_h * 0.6]
    diagonals += [(c["x0"], c["top"], c["x1"], c["bottom"]) for c in page.curves
                  if abs(c["x1"] - c["x0"]) > col_w * 0.6 and abs(c["bottom"] - c["top"]) > row_h * 0.6
                  and abs(c["x1"] - c["x0"]) < col_w * 1.6]

    def diag_visible(d):
        if img is None:
            return True
        dx0, dy0, dx1, dy1 = d
        for pts in ([(dx0, dy1), (dx1, dy0)], [(dx0, dy0), (dx1, dy1)]):
            (ax, ay), (bx, by) = pts
            ok = True
            for f in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
                x, y = ax + (bx - ax) * f, ay + (by - ay) * f
                r = _region(img, scale, x - 1.5, y - 1.5, x + 1.5, y + 1.5)
                if r.size == 0 or r.min() > 140:
                    ok = False
                    break
            if ok:
                return True
        return False

    diagonals = [d for d in diagonals if diag_visible(d)]

    # 旗日ラベル(縦書き・黒・やや太字)、白抜き文字、店休日
    small = [c for c in chars if c["size"] < 16 and c["top"] > head_bottom and c["text"].strip()]

    for t in base:
        ci = min(range(len(col_x)), key=lambda i: abs(col_x[i] - t["x0"]))
        ri = min(range(len(row_y)), key=lambda i: abs(row_y[i] - t["top"]))
        x0 = col_x[ci] - 4
        x1 = x0 + col_w
        y0 = row_y[ri] - 4
        y1 = y0 + row_h
        day = int(t["text"])
        cell = FaxCell(day=day, weekday=fp.columns[ci] if ci < len(fp.columns) else "?", row=ri)
        cell.hidden = any(dx0 <= t["x0"] <= dx1 and dy0 <= t["top"] <= dy1
                          for dx0, dy0, dx1, dy1 in diagonals)
        cell.black_box = t["color"] == "white"
        cell.gray = t["color"] == "gray"

        strip_x1 = t["x0"] + 32  # 日付の下の細い帯
        in_cell = [c for c in small if x0 <= c["x0"] < x1 and y0 <= c["top"] < y1]
        strip = [c for c in in_cell if c["x0"] < strip_x1]
        label = [c for c in strip if color_kind(c["non_stroking_color"]) == "black"
                 and c["size"] >= 8.5 and visible_dark(c)]
        white = [c for c in strip if color_kind(c["non_stroking_color"]) == "white"
                 and c["size"] > 3 and visible_white(c)]
        cell.flag = norm(vertical_text(label))
        rest = list(white)
        for word in ("混ぜご飯", "ふりかけ"):
            if all(any(c["text"] == ch for c in rest) for ch in word):
                cell.rice = word
                for ch in word + "贅沢弁当":
                    for c in rest:
                        if c["text"] == ch:
                            rest.remove(c)
                            break
                break
        if norm("".join(c["text"] for c in rest)):
            cell.event_text = norm(vertical_text(rest))
        # 店休日 = 日付がグレー(見た目)。「店休日」の文字は下に隠れていることがあるため使わない
        cell.closed = cell.gray
        fp.cells.append(cell)

    # ヘッダー(年・月・期間)
    # 左上の「12 2026 12/1〜12/31」の枠(最初の日付行より上、ページ左側)
    first_row = min(t["top"] for t in base)
    header = [c for c in chars if c["top"] < first_row - 40 and c["x0"] < page.width * 0.3]
    nums = group_chars_to_numbers([c for c in header if c["text"].isdigit()])
    if nums:
        big = max(nums, key=lambda t: t["size"])
        fp.month = int(big["text"]) if len(big["text"]) <= 2 else None
    for t in nums:
        if re.fullmatch(r"20\d\d", t["text"]):
            fp.year = int(t["text"])
    htxt = norm("".join(c["text"] for c in sorted(header, key=lambda c: (round(c["top"] / 4), c["x0"]))))
    m = re.search(r"(\d{1,2}/\d{1,2})〜?(\d{1,2}/\d{1,2})", htxt)
    if m:
        fp.period = f"{m.group(1)}〜{m.group(2)}"
    # 贅沢版 = 注文表に「贅沢弁当」の行が(見える状態で)ある
    fp.zeitaku = any(c["text"] == "贅" and c["size"] < 9 and c["top"] > table_top
                     and color_kind(c["non_stroking_color"]) == "black" and visible_dark(c)
                     for c in chars)
    return fp


def parse_fax(pdf_bytes: bytes, progress=None, scale=2.0) -> list[FaxPage]:
    import io
    import numpy as np
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception:
        doc = None
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            img = None
            if doc is not None:
                img = np.asarray(doc[i].render(scale=scale, grayscale=True).to_pil().convert("L"))
            pages.append(parse_fax_page(page, i + 1, img=img, scale=scale))
            page.flush_cache()
            if progress:
                progress(i + 1, n)
    return pages


# ---------------------------------------------------------------- 照合

@dataclass
class Issue:
    page: int
    store: str
    item: str       # ①〜④
    day: str
    expected: str
    actual: str
    level: str = "エラー"   # エラー / 要確認


def check_page(fp: FaxPage, master: MenuMaster, year: int, month: int,
               event_name: str = "") -> list[Issue]:
    issues = []
    store = fp.label

    def add(item, day, exp, act, level="エラー"):
        issues.append(Issue(fp.page_no, store, item, str(day), str(exp), str(act), level))

    last = calendar.monthrange(year, month)[1]

    # ① 年・月・期間
    if fp.year != year:
        add("①年月日", "", f"{year}", fp.year)
    if fp.month != month:
        add("①年月日", "", f"{month}月", f"{fp.month}月")
    exp_period = f"{month}/1〜{month}/{last}"
    if norm(fp.period) != norm(exp_period):
        add("①年月日", "", exp_period, fp.period or "(読み取れず)")

    # ② 曜日と日付
    visible = [c for c in fp.cells if not c.hidden]
    seen = {}
    rows = sorted({c.row for c in visible})
    for c in sorted(visible, key=lambda c: (c.row, WEEKDAYS.find(c.weekday))):
        # 1行目の大きい数字=前月、最後の行の小さい数字=翌月
        if rows and c.row == rows[0] and c.day > 7:
            add("②曜日", c.day, "前月の日付は斜線で消す", f"{c.day}日が表示されています")
            continue
        if rows and c.row in rows[-2:] and c.day < 7 and len(rows) > 2:
            add("②曜日", c.day, "翌月の日付は斜線で消す", f"{c.day}日が表示されています")
            continue
        if c.day in seen:
            add("②曜日", c.day, "同じ日付が2回あります", f"{c.day}日が重複")
            continue
        if not 1 <= c.day <= last:
            add("②曜日", c.day, f"{month}月は{last}日まで", f"{c.day}日が表示されています")
            continue
        seen[c.day] = c
        wd = WEEKDAYS[calendar.weekday(year, month, c.day)]
        if c.weekday != wd:
            add("②曜日", c.day, f"{c.day}日は{wd}曜", f"{c.weekday}曜の列にあります")
    for d in range(1, last + 1):
        wd = WEEKDAYS[calendar.weekday(year, month, d)]
        if wd in fp.columns and d not in seen:
            add("②曜日", d, f"{d}日({wd})が必要", "見つかりません")

    # 店休日は店舗ごとに違うためチェックしない(店休日のマスは③も対象外)

    # ③ 旗日(店休日のマスは対象外)
    for d, c in seen.items():
        if c.closed:
            continue
        mi = master.days.get(d) or DayInfo(d)
        exp_text = mi.flag or mi.label
        if norm(exp_text) != norm(c.flag):
            add("③旗日", d, exp_text or "(なし)", c.flag or "(なし)")
        if bool(mi.flag) != c.black_box:
            add("③旗日", d, "日付を黒地に" if mi.flag else "日付は白地",
                "黒地" if c.black_box else "白地")
        exp_rice = mi.rice if fp.zeitaku else ""
        if exp_rice != c.rice:
            add("③混ぜご飯", d, exp_rice or "(なし)", c.rice or "(なし)")

    # ④ イベントWeek
    events = [c for c in seen.values() if c.event_text]
    if master.event_range:
        s, e = master.event_range
        if not events:
            add("④イベント", f"{s}〜{e}", f"イベントWeek表記({s}日〜{e}日)", "表記なし")
        for c in events:
            m = re.search(r"(\d+)日〜(\d+)日", c.event_text)
            got = (int(m.group(1)), int(m.group(2))) if m else None
            if got != (s, e):
                add("④イベント", c.day, f"期間 {s}日〜{e}日", c.event_text)
            # 表記の位置:開始日、または週をまたぐ場合は各週の最初のマス
            row_firsts = {min((x for x in seen.values() if x.row == c.row), key=lambda x: x.day).day}
            if not (c.day == s or (s <= c.day <= e and c.day in row_firsts)):
                add("④イベント", c.day, f"表記は{s}日のマスに", f"{c.day}日のマスにあります")
            if event_name and norm(event_name) not in c.event_text:
                add("④イベント", c.day, event_name, c.event_text)
    else:
        for c in events:
            add("④イベント", c.day, "(中面にイベントなし)", c.event_text)

    for w in fp.warnings:
        add("読み取り", "", "", w)
    return issues
