"""Generates a visual, screenshot-driven 2-page PDF guide explaining the logic,
calculations, and operational workflow of the USD/INR Rollover System.
Uses actual system screenshots with numbered callout pins.
"""

import os
from PIL import Image, ImageDraw, ImageFont

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, HRFlowable,
    Image as RLImage
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas


def create_annotated_screenshots(raw_screenshot_path: str, out_dir: str):
    """Generates the annotated full screenshot and zoomed snippet images."""
    im = Image.open(raw_screenshot_path).convert('RGBA')

    # 1. Full annotated image
    annotated = im.copy()
    draw = ImageDraw.Draw(annotated)
    font_badge = ImageFont.truetype('arialbd.ttf', 13)

    # Numbered pins matching key UI sections: (num, box, badge_pos, col)
    pins = [
        ('1', (18, 75, 120, 142), (18, 52), (239, 68, 68)),    # Near Bid (Red - SELL)
        ('2', (725, 75, 830, 142), (725, 52), (34, 197, 94)),  # Far Ask (Green - BUY)
        ('3', (18, 165, 148, 220), (18, 142), (59, 130, 246)), # Roll Cost (Blue)
        ('4', (18, 222, 205, 252), (18, 200), (168, 85, 247)),# bps & Rs/lot (Purple)
        ('5', (320, 175, 520, 268), (320, 152), (245, 158, 11)),# Roll Limit (Amber)
        ('6', (875, 185, 995, 255), (875, 160), (236, 72, 153)),# WAITING status (Pink)
        ('7', (22, 330, 255, 475), (22, 305), (14, 165, 233)), # Ladder Card (Cyan)
        ('8', (775, 290, 995, 322), (775, 265), (99, 102, 241)) # Sizing & Progress (Indigo)
    ]

    for num, (x0, y0, x1, y1), (bx, by), col in pins:
        draw.rectangle([x0, y0, x1, y1], outline=col, width=2)
        bw, bh = 20, 20
        draw.ellipse([bx, by, bx + bw, by + bh], fill=col)
        draw.text((bx + 6, by + 2), num, fill=(255, 255, 255), font=font_badge)

    annotated_path = os.path.join(out_dir, "assets_annotated_gui.png")
    annotated.convert('RGB').save(annotated_path)

    # 2. Cropped snippets for page 2
    crop_quotes = im.crop((10, 10, 1014, 148)).convert('RGB')
    crop_quotes_path = os.path.join(out_dir, "assets_crop_quotes.png")
    crop_quotes.save(crop_quotes_path)

    crop_decision = im.crop((10, 150, 1014, 278)).convert('RGB')
    crop_decision_path = os.path.join(out_dir, "assets_crop_decision.png")
    crop_decision.save(crop_decision_path)

    crop_ladder = im.crop((10, 280, 1014, 485)).convert('RGB')
    crop_ladder_path = os.path.join(out_dir, "assets_crop_ladder.png")
    crop_ladder.save(crop_ladder_path)

    return annotated_path, crop_quotes_path, crop_decision_path, crop_ladder_path


class NumberedCanvas(canvas.Canvas):
    """Adds running headers and footers with page numbers (Page X of Y)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, total_pages):
        self.saveState()
        self.setFont("Helvetica-Bold", 8)
        self.setFillColor(colors.HexColor("#475569"))

        # Header on Page 2
        if self._pageNumber > 1:
            self.drawString(36, 810, "USD/INR Rollover Watcher & Execution Engine — Visual Screen & Calculation Guide")
            self.setStrokeColor(colors.HexColor("#CBD5E1"))
            self.setLineWidth(0.5)
            self.line(36, 804, 559, 804)

        # Footer on all pages
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.5)
        self.line(36, 30, 559, 30)
        self.setFont("Helvetica", 7.5)
        self.drawString(36, 20, "CONFIDENTIAL & PROPRIETARY — FOR AUTHORIZED TRADING OPERATORS ONLY")
        page_str = f"Page {self._pageNumber} of {total_pages}"
        self.drawRightString(559, 20, page_str)
        self.restoreState()


def build_pdf(filename: str, img_dir: str):
    doc = SimpleDocTemplate(
        filename,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=32,
        bottomMargin=32
    )

    styles = getSampleStyleSheet()

    primary_color = colors.HexColor("#0F172A")    # Slate 900
    brand_blue = colors.HexColor("#1D4ED8")       # Blue 700
    subtext_color = colors.HexColor("#334155")    # Slate 700

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=16,
        leading=20,
        textColor=primary_color,
        spaceAfter=2
    )

    subtitle_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#475569"),
        spaceAfter=4
    )

    h1_style = ParagraphStyle(
        'SecHeading',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=13,
        textColor=brand_blue,
        spaceBefore=5,
        spaceAfter=3,
        keepWithNext=True
    )

    body_style = ParagraphStyle(
        'BodyDark',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7.5,
        leading=10.5,
        textColor=subtext_color,
        spaceAfter=3
    )

    badge_pin_style = ParagraphStyle(
        'BadgePin',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=11,
        textColor=colors.white
    )

    card_title_style = ParagraphStyle(
        'CardTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=11,
        textColor=primary_color
    )

    card_text_style = ParagraphStyle(
        'CardText',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7.2,
        leading=10,
        textColor=subtext_color
    )

    formula_style = ParagraphStyle(
        'FormulaText',
        parent=styles['Normal'],
        fontName='Courier-Bold',
        fontSize=7.2,
        leading=10,
        textColor=colors.HexColor("#1E3A8A")
    )

    story = []

    annotated_img_path = os.path.join(img_dir, "assets_annotated_gui.png")
    crop_quotes_path = os.path.join(img_dir, "assets_crop_quotes.png")
    crop_decision_path = os.path.join(img_dir, "assets_crop_decision.png")
    crop_ladder_path = os.path.join(img_dir, "assets_crop_ladder.png")

    # ========================== PAGE 1 ==========================
    # Title Banner
    story.append(Paragraph("USD/INR Rollover Watcher & Execution Engine", title_style))
    story.append(Paragraph("Visual Screen Map & Mathematical Calculation Reference (Step-by-Step)", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=1, color=brand_blue, spaceBefore=0, spaceAfter=4))

    # Big Annotated Screenshot
    # A4 width = 595, margins = 36*2=72 => printable = 523 pt.
    # Image aspect ratio: 1024x489 => height = 523 * (489/1024) = 249.7 pt.
    story.append(RLImage(annotated_img_path, width=523, height=249.7))
    story.append(Spacer(1, 4))

    story.append(Paragraph("1. Live Screen Breakdown (Matching the Numbered Callout Pins Above)", h1_style))

    # Pin Reference Table
    pin_table_data = [
        [
            # Col 1: Pin 1
            [Paragraph("<font color='#EF4444'><b>[ 1 ] Near Leg Bid (94.9500)</b></font>", card_title_style),
             Paragraph("<b>Action: SELL October Futures</b><br/>"
                       "To exit your expiring Oct contract, the order hits the live Bid. You receive <b>94.9500 Rs</b> per USD.<br/>"
                       "<i>Resting depth: 10,000 qty.</i>", card_text_style)],
            # Col 2: Pin 2
            [Paragraph("<font color='#22C55E'><b>[ 2 ] Far Leg Ask (95.4225)</b></font>", card_title_style),
             Paragraph("<b>Action: BUY November Futures</b><br/>"
                       "To enter the forward Nov contract, the order lifts the live Ask. You pay <b>95.4225 Rs</b> per USD.<br/>"
                       "<i>Resting depth: 100,000 qty.</i>", card_text_style)],
        ],
        [
            # Col 1: Pin 3
            [Paragraph("<font color='#3B82F6'><b>[ 3 ] Roll Cost Spread (0.4725 Rs)</b></font>", card_title_style),
             Paragraph("<b>Formula: Far Ask - Near Bid</b><br/>"
                       "<code>95.4225 - 94.9500 = 0.4725 Rs</code><br/>"
                       "The exact difference in rupees per dollar paid to carry the hedge 1 month forward.", formula_style)],
            # Col 2: Pin 4
            [Paragraph("<font color='#A855F7'><b>[ 4 ] Basis Points & Cost / Lot</b></font>", card_title_style),
             Paragraph("<b>Formula: (Spread / Mid) x 10,000</b><br/>"
                       "Mid = (94.9500 + 94.9550)/2 = 94.9525<br/>"
                       "<code>(0.4725 / 94.9525) x 10,000 = 49.8 bps</code><br/>"
                       "<b>Per Lot:</b> 0.4725 x 1,000 = <b>Rs 472.50 per lot</b>", formula_style)],
        ],
        [
            # Col 1: Pin 5
            [Paragraph("<font color='#F59E0B'><b>[ 5 ] Roll Limit Ceiling (47 bps = 0.4462)</b></font>", card_title_style),
             Paragraph("<b>Your Configured Ceiling</b><br/>"
                       "You configured 47 bps for 1-month rolls.<br/>"
                       "<code>(47 / 10,000) x 94.9525 = 0.4462 Rs</code><br/>"
                       "The maximum allowable spread. The bot will reject anything higher.", formula_style)],
            # Col 2: Pin 6
            [Paragraph("<font color='#EC4899'><b>[ 6 ] Live Status: WAITING (2.8 bps too dear)</b></font>", card_title_style),
             Paragraph("<b>Decision Rule: Cost &lt;= Limit</b><br/>"
                       "Market Cost = 49.8 bps | Your Limit = 47.0 bps<br/>"
                       "<code>49.8 - 47.0 = 2.8 bps too dear</code> (too expensive).<br/>"
                       "The system holds safely without executing until market price qualifies.", card_text_style)],
        ],
        [
            # Col 1: Pin 7
            [Paragraph("<font color='#0EA5E9'><b>[ 7 ] Target Trigger Hint (95.3962)</b></font>", card_title_style),
             Paragraph("<b>When will it execute?</b><br/>"
                       "<code>Near Bid + Limit = 94.9500 + 0.4462 = 95.3962</code><br/>"
                       "If Nov Ask falls to &lt;= <b>95.3962</b> (or Oct Bid rises by 2.8 bps), the trade fires automatically.", card_text_style)],
            # Col 2: Pin 8
            [Paragraph("<font color='#6366F1'><b>[ 8 ] Sizing & Progress (1,000 left, clip 1,000)</b></font>", card_title_style),
             Paragraph("<b>Trade Execution Sizing</b><br/>"
                       "• <b>1 Lot</b> = <b>1,000 Quantity</b>.<br/>"
                       "• <b>Each Clip:</b> Sends <b>1,000 qty in one go</b> (SELL 1,000 Oct / BUY 1,000 Nov).<br/>"
                       "• Capped at 1 clip per day for safety.", card_text_style)],
        ]
    ]

    t_pins = Table(pin_table_data, colWidths=[258, 258])
    t_pins.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(t_pins)

    # ========================== PAGE 2 ==========================
    story.append(PageBreak())

    story.append(Paragraph("2. Deep Dive: Section-by-Section Screen Walkthrough", h1_style))

    # PART A: Quotes snippet
    story.append(Paragraph("<b>A. Market Quotes & The Leg Pricing (Near Exit vs. Far Entry)</b>", card_title_style))
    story.append(RLImage(crop_quotes_path, width=523, height=71.2))
    story.append(Paragraph(
        "• <b>Near Leg (Oct FUT):</b> You are closing a long position, so the system places a SELL limit at the <b>Near Bid (94.9500)</b>.<br/>"
        "• <b>Far Leg (Nov FUT):</b> You are opening the new forward contract, so the system places a BUY limit at the <b>Far Ask (95.4225)</b>.<br/>"
        "• <b>Execution Safeguard:</b> Both limit orders are sent at current touchline prices with <code>allowance_ticks = 0</code> to guarantee zero negative price slippage.",
        body_style
    ))
    story.append(Spacer(1, 3))

    # PART B: Decision snippet
    story.append(Paragraph("<b>B. Roll Cost & Trigger Calculation Panel</b>", card_title_style))
    story.append(RLImage(crop_decision_path, width=523, height=65.5))
    story.append(Paragraph(
        "• <b>Observed Spread:</b> <code>95.4225 - 94.9500 = 0.4725 Rs</code> (= <b>49.8 bps</b> / <b>Rs 472.50 per lot</b>).<br/>"
        "• <b>Roll Limit:</b> Set to <b>47 bps</b> (= <b>0.4462 Rs</b>). The mathematical rule is: <b>Trade ONLY if Spread &lt;= Limit</b>.<br/>"
        "• <b>Waiting Indicator:</b> Because 49.8 bps is <b>2.8 bps higher</b> than 47.0 bps, the system displays <code>WAITING: 2.8 bps too dear</code> and holds safely.<br/>"
        "• <i>Historical Tracker:</i> Shows today's best roll cost was <b>45.3 bps</b> (at 12:36 PM), and the market has been at/below your limit for 4.3 minutes today.",
        body_style
    ))
    story.append(Spacer(1, 3))

    # PART C: Lot Sizing & Safety Box
    story.append(Paragraph("3. Sizing Rules: From 4,000 Lots Total to 1 Lot (1,000 Qty) Per Trade", h1_style))
    sizing_box = [
        [Paragraph("<b>HOW THE BOT SIZES EACH CLIP</b>", card_title_style)],
        [Paragraph(
            "• <b>Total Account Position:</b> <b>4,000 Lots</b> = <b>4,000,000 Quantity</b> (40 Lakh USD) across October futures.<br/>"
            "• <b>Single Trade Size:</b> Configured to <code>lots: 1</code>. When the trade triggers, it executes <b>exactly 1,000 quantity in one go</b>.<br/>"
            "• <b>Why 1,000 Quantity?</b> Choice Broking exchange API strictly requires orders in base units (1 lot = 1,000 units). Sending '1' would be rejected.<br/>"
            "• <b>Automatic Daily Lock:</b> <code>max_clips_per_day: 1</code> guarantees that after this 1 lot rolls, the engine immediately locks and prevents further trades today.",
            body_style
        )]
    ]
    t_sizing = Table(sizing_box, colWidths=[523])
    t_sizing.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#EFF6FF")),
        ('BOX', (0, 0), (-1, -1), 0.8, colors.HexColor("#93C5FD")),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(t_sizing)
    story.append(Spacer(1, 4))

    # PART D: 4-Step Operator Quick-Start
    story.append(Paragraph("4. Operator 4-Step Checklist (How to Execute via GUI)", h1_style))
    flow_table_data = [
        [Paragraph("Step", badge_pin_style),
         Paragraph("Action in Application Window (dist/roll_app.exe)", badge_pin_style),
         Paragraph("Expected Screen Confirmation", badge_pin_style)],

        [Paragraph("<b>Step 1</b>", card_title_style),
         Paragraph("<b>Open <code>roll_app.exe</code></b>: Inspect the live Near Bid (94.9500) and Far Ask (95.4225).", card_text_style),
         Paragraph("Live prices and current roll cost (0.4725 / 49.8 bps) tick in real time.", card_text_style)],

        [Paragraph("<b>Step 2</b>", card_title_style),
         Paragraph("<b>Set Target Limit</b>: Enter target bps (e.g. <code>47 bps</code> to wait, or <code>50 bps</code> to trigger immediately) and click <b>Set</b>.", card_text_style),
         Paragraph("The limit card updates immediately with the new rupee threshold (e.g. 0.4462 or 0.4748).", card_text_style)],

        [Paragraph("<b>Step 3</b>", card_title_style),
         Paragraph("<b>Switch from DRY RUN to LIVE</b>: Click the <b>DRY RUN</b> button at top right. A safety dialog pops up. Type <code>LIVE</code> and confirm.", card_text_style),
         Paragraph("Top status bar turns into a bright red <b>LIVE</b> badge. Real exchange orders are now authorised.", card_text_style)],

        [Paragraph("<b>Step 4</b>", card_title_style),
         Paragraph("<b>Click ARM</b>: System enters armed state for up to 120 seconds.", card_text_style),
         Paragraph("When Cost &lt;= Limit, bot sends 1,000 Oct Sell and 1,000 Nov Buy in one go, fills cleanly, and halts.", card_text_style)],
    ]
    t_flow = Table(flow_table_data, colWidths=[45, 235, 243])
    t_flow.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(t_flow)

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Generated visual PDF: {filename}")


if __name__ == "__main__":
    out_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets_dir = os.path.join(out_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    user_uploaded_img = r"C:\Users\kaival.trapasia\.gemini\antigravity-ide\brain\164cd879-dc50-43a6-9ac7-4750ca6fae0e\.user_uploaded\media_1792747436311.png"

    # Create crops & annotated images in assets/
    create_annotated_screenshots(user_uploaded_img, assets_dir)

    target_pdf = os.path.join(out_dir, "USDINR_Rollover_System_Guide.pdf")
    build_pdf(target_pdf, assets_dir)

    # Also build to dist/
    dist_dir = os.path.join(out_dir, "dist")
    if os.path.isdir(dist_dir):
        dist_pdf = os.path.join(dist_dir, "USDINR_Rollover_System_Guide.pdf")
        build_pdf(dist_pdf, assets_dir)

