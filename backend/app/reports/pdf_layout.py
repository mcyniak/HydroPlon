"""Drukowany widok raportu HydroPlon, oparty na układzie kreatora."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from urllib.parse import urlsplit

from fpdf import FPDF

from app.config import APP_DIR, APP_VERSION
from app.models.economics import FinalPlan

_INK = (24, 38, 56)
_MUTED = (76, 96, 117)
_GREEN = (7, 75, 53)
_GREEN_PALE = (239, 249, 243)
_GREEN_LINE = (185, 220, 200)
_GOLD = (186, 131, 13)
_YELLOW_PALE = (255, 249, 233)
_YELLOW_LINE = (235, 212, 146)
_BLUE = (21, 92, 144)
_BLUE_PALE = (238, 247, 255)
_BLUE_LINE = (128, 182, 224)
_WHITE = (255, 255, 255)
_SOFT = (244, 247, 245)
_RULE = (203, 216, 208)
_LOGO = Path(__file__).with_name("logo-hydroplon.png")
# Statyczne odmiany 400 i 700 z dołączonych fontów Literata (OFL-Literata.txt).
_FONT_DIR = APP_DIR / "static" / "fonts"
_SYMBOLS = str.maketrans({"−": "-", "–": "-", "—": "-", "‑": "-", "→": "->", "≥": ">=", "≤": "<=",
                          "≈": "= ok.", "₀": "0"})
_TOP = 29
_CARD_PAD = 3.5
_GAP = 3

Block = tuple[str, str]
Color = tuple[int, int, int]


@dataclass(frozen=True)
class _Card:
    title: str
    lines: list[str]
    fill: Color = _WHITE
    border: Color = _RULE
    ink: Color = _GREEN
    metric: str = ""
    compact: bool = False
    bold_first: bool = False


class _ReportPdf(FPDF):
    def __init__(self, plan_id: str) -> None:
        super().__init__(format="A4")
        self.plan_id = plan_id
        self.set_margins(17, _TOP, 17)
        self.set_auto_page_break(auto=True, margin=21)
        self.set_title("Plan gospodarowania wodą")
        self.set_author("HydroPlon")
        self.set_lang("pl-PL")
        self.alias_nb_pages()
        self.add_font("Literata", "", _FONT_DIR / "literata-pdf-400.ttf")
        self.add_font("Literata", "B", _FONT_DIR / "literata-pdf-700.ttf")

    def header(self) -> None:
        self.image(_LOGO, x=self.l_margin, y=7, w=17)
        self.set_xy(self.l_margin + 18, 11)
        self.set_font("Literata", "B", 11)
        self.set_text_color(*_GREEN)
        self.cell(40, 7, "HydroPlon")
        self.set_font("Literata", "", 8.5)
        self.set_text_color(*_MUTED)
        self.cell(self.epw - 58, 7, "Raport dla działki rolnej", align="R")
        self.set_draw_color(*_RULE)
        self.line(self.l_margin, 24, self.w - self.r_margin, 24)
        self.set_y(_TOP)

    def footer(self) -> None:
        self.set_y(-16)
        self.set_draw_color(*_RULE)
        self.line(self.l_margin, self.y, self.w - self.r_margin, self.y)
        self.ln(2)
        self.set_font("Literata", "", 8)
        self.set_text_color(*_MUTED)
        self.cell(self.epw - 36, 5, f"HydroPlon {APP_VERSION}  |  {self.plan_id}")
        self.cell(36, 5, f"Strona {self.page_no()} / {{nb}}", align="R")

    def _ensure(self, height: float) -> None:
        if height <= self.page_break_trigger - _TOP and self.will_page_break(height):
            self.add_page()

    def _cell(self, text: str, width: float, line_height: float, *, bold: bool = False,
              size: float = 9.5, color: Color = _INK, measure: bool = False, markdown: bool = False) -> float:
        self.set_font("Literata", "B" if bold else "", size)
        self.set_text_color(*color)
        return self.multi_cell(width, line_height, text.translate(_SYMBOLS), align="L", markdown=markdown,
                               dry_run=measure, output="HEIGHT", new_x="LEFT", new_y="NEXT")

    def write_title(self, text: str) -> None:
        self._cell(text, self.epw, 10, size=23, bold=True)
        self.ln(3)

    def section(self, text: str, *, fill: Color = _SOFT, rule: Color = _GREEN_LINE, room: float = 0) -> None:
        height = self._cell(text, self.epw - 6, 6, bold=True, size=12.5, measure=True) + 4
        self._ensure(height + 6 + max(room, 10))
        self.ln(3)
        y = self.y
        self.start_section(text)
        self.set_fill_color(*fill)
        self.rect(self.l_margin, y, self.epw, height, "F")
        self.set_fill_color(*rule)
        self.rect(self.l_margin, y, self.epw, 0.8, "F")
        self.set_xy(self.l_margin + 3, y + 2.5)
        self._cell(text, self.epw - 6, 6, bold=True, size=12.5, color=_GREEN)
        self.set_xy(self.l_margin, y + height + 3)

    def _card_content(self, card: _Card, width: float, *, measure: bool) -> float:
        inner = width - 2 * _CARD_PAD
        height = 0.0
        if card.title:
            height += self._cell(card.title, inner, 4.9, size=10, bold=True, color=card.ink, measure=measure) + 1
            if not measure:
                self.set_xy(self.x, self.y + 1)
        if card.metric:
            height += self._cell(card.metric, inner, 6.1, size=13, bold=True, color=card.ink, measure=measure) + 1
            if not measure:
                self.set_xy(self.x, self.y + 1)
        size, line_height = (9.0, 4.2) if card.compact else (9.5, 4.5)
        for i, line in enumerate(card.lines):
            height += self._cell(line, inner, line_height, size=size, bold=card.bold_first and i == 0,
                                 measure=measure) + 0.7
            if not measure:
                self.set_xy(self.x, self.y + 0.7)
        return height + 2 * _CARD_PAD

    def row_height(self, cards: list[_Card], widths: list[float]) -> float:
        return max(self._card_content(card, width, measure=True) for card, width in zip(cards, widths))

    def cards(self, cards: list[_Card], widths: list[float]) -> None:
        height = self.row_height(cards, widths)
        self._ensure(height + _GAP)
        if height > self.page_break_trigger - _TOP - _GAP:
            for card in cards:
                if card.title:
                    self.band(card.title, card.fill, card.ink)
                for line in [*([card.metric] if card.metric else []), *card.lines]:
                    self.paragraph("p", line)
            return
        x, y = self.l_margin, self.y
        for card, width in zip(cards, widths):
            self.set_fill_color(*card.fill)
            self.set_draw_color(*card.border)
            self.rect(x, y, width, height, "DF")
            self.set_xy(x + _CARD_PAD, y + _CARD_PAD)
            self._card_content(card, width, measure=False)
            x += width + _GAP
        self.set_xy(self.l_margin, y + height + _GAP)

    def band(self, text: str, fill: Color, ink: Color) -> None:
        height = self._cell(text, self.epw - 6, 4.8, bold=True, color=ink, measure=True) + 3
        self._ensure(height + 11)
        y = self.y
        self.set_fill_color(*fill)
        self.rect(self.l_margin, y, self.epw, height, "F")
        self.set_xy(self.l_margin + 3, y + 1.5)
        self._cell(text, self.epw - 6, 4.8, bold=True, color=ink)
        self.set_xy(self.l_margin, y + height + 1.2)

    def paragraph(self, kind: str, text: str, *, keep_with_next: float = 0) -> None:
        indent = 5 if kind == "li" else 0
        width = self.epw - indent
        text = text.strip()
        markdown = kind == "task_compact"
        height = self._cell(text, width, 4.5, markdown=markdown, measure=True)
        self._ensure(height + 1 + keep_with_next)
        if indent:
            self.set_fill_color(*_GREEN)
            self.ellipse(self.l_margin + 1.8, self.y + 1.7, 0.75, 0.75, style="F")
        self.set_x(self.l_margin + indent)
        self._cell(text, width, 4.5, markdown=markdown)
        self.set_xy(self.l_margin, self.y + 1)

    def blocks(self, blocks: list[Block], *, fill: Color = _SOFT, accent: Color = _GREEN) -> None:
        for is_link, group in groupby(enumerate(blocks), key=lambda row: row[1][0] == "source"):
            if is_link:
                self._ensure(5)
                self.set_font("Literata", "", 9)
                self.set_text_color(*_BLUE)
                self.write(4.5, "Źródła: ")
                for i, (_, (_, url)) in enumerate(group):
                    if i:
                        self.write(4.5, " · ")
                    self.write(4.5, urlsplit(url).netloc, link=url)
                self.set_xy(self.l_margin, self.y + 5)
                continue
            for i, (kind, text) in group:
                if kind == "phase":
                    self.band(text, fill, accent)
                else:
                    has_link = i + 1 < len(blocks) and blocks[i + 1][0] == "source"
                    self.paragraph(kind, text, keep_with_next=5 if has_link else 0)


_SECTIONS = {"title", "overview", "recommendations", "profitability", "costs", "funding", "implementation",
             "appendix", "sources", "limitations", "notes"}


def render_layout(plan: FinalPlan, water_report: dict, blocks: list[Block]) -> bytes:
    sections: dict[str, tuple[str, list[Block]]] = {}
    for kind, text in blocks:
        if kind in _SECTIONS:
            section_blocks: list[Block] = []
            sections[kind] = (text, section_blocks)
        else:
            section_blocks.append((kind, text))

    pdf = _ReportPdf(plan.plan_id)
    pdf.add_page()
    full = [pdf.epw]
    pair = [(pdf.epw - _GAP) / 2] * 2
    thirds = [(pdf.epw - 2 * _GAP) / 3] * 3
    state, causes, actions, *_ = water_report["sections"]
    title, metadata = sections["title"]
    pdf.write_title(title)
    pdf.cards([_Card("", [text for _, text in metadata], _SOFT)], full)

    diagnosis = [_Card(part["title"], [entry["text"] for entry in part["entries"]], border=_GREEN_LINE,
                       bold_first=part is state) for part in (state, causes)]
    dates = [_Card(entry["label"], [], _GREEN_PALE, _GREEN_LINE, metric=entry["text"])
             for entry in water_report["dates"]]
    pdf.section(sections["overview"][0], fill=_GREEN_PALE, rule=_GOLD,
                room=pdf.row_height(diagnosis, pair) + pdf.row_height(dates, thirds) + 2 * _GAP)
    pdf.cards(diagnosis, pair)
    pdf.cards(dates, thirds)

    action_cards = [_Card(entry["label"], [entry["text"]], _BLUE_PALE, _BLUE_LINE, _BLUE)
                    for entry in actions["entries"]]
    rows = [action_cards[i:i + 2] for i in range(0, len(action_cards), 2)]
    if rows:
        pdf.section(sections["recommendations"][0], rule=_BLUE_LINE,
                    room=pdf.row_height(rows[0], pair) + _GAP)
        for row in rows:
            pdf.cards(row, pair)

    profitability_title, profitability = sections["profitability"]
    cost_title, costs = sections["costs"]
    finance_widths = [(pdf.epw - _GAP) * ratio for ratio in (0.61, 0.39)]
    financial = [_Card(profitability_title, [text for _, text in profitability[1:]], _GREEN_PALE, _GREEN_LINE,
                       metric=profitability[0][1]),
                 _Card(cost_title, [text for _, text in costs[1:3]], _SOFT, metric=costs[0][1])]
    pdf._ensure(pdf.row_height(financial, finance_widths) + _GAP)
    pdf.start_section(profitability_title)
    pdf.start_section(cost_title)
    pdf.cards(financial, finance_widths)
    if len(costs) > 3:
        pdf.band("Pozycje kosztorysu", _SOFT, _GREEN)
        pdf.blocks(costs[3:])

    funding_title, funding = sections["funding"]
    pdf.section(funding_title, fill=_YELLOW_PALE, rule=_YELLOW_LINE)
    pdf.blocks(funding, fill=_YELLOW_PALE, accent=(122, 95, 28))

    implementation_title, implementation = sections["implementation"]
    deadlines = [text for kind, text in implementation if kind == "deadline"]
    agenda = [_Card("Najbliższe terminy", deadlines, _BLUE_PALE, _BLUE_LINE, _BLUE)] if deadlines else []
    pdf.section(implementation_title, fill=_GREEN_PALE, rule=_GREEN_LINE,
                room=pdf.row_height(agenda, full) if agenda else 0)
    if agenda:
        pdf.cards(agenda, full)
    pdf.blocks([(kind, text) for kind, text in implementation if kind != "deadline"], fill=_GREEN_PALE)

    sources_title, sources = sections["sources"]
    limits_title, limits = sections["limitations"]
    evidence = [_Card(sources_title, [text for _, text in sources], _SOFT, compact=True),
                _Card(limits_title, [text for _, text in limits], _YELLOW_PALE, _YELLOW_LINE, compact=True)]
    evidence_widths = [(pdf.epw - _GAP) * ratio for ratio in (0.46, 0.54)]
    pdf.section(sections["appendix"][0], room=pdf.row_height(evidence, evidence_widths) + _GAP)
    pdf.cards(evidence, evidence_widths)
    pdf.blocks(sections["notes"][1])
    return bytes(pdf.output())
