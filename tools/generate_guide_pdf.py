"""Generates a professional, user-friendly 2-page PDF guide explaining the logic,
calculations, and operational workflow of the USD/INR Rollover System.
"""

import os
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas


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
            self.drawString(36, 810, "USD/INR Rollover Watcher & Execution Engine — User & Calculation Guide")
            self.setStrokeColor(colors.HexColor("#CBD5E1"))
            self.setLineWidth(0.5)
            self.line(36, 804, 559, 804)

        # Footer on all pages
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.5)
        self.line(36, 32, 559, 32)
        self.setFont("Helvetica", 7.5)
        self.drawString(36, 21, "CONFIDENTIAL & PROPRIETARY — FOR AUTHORIZED TRADING OPERATORS ONLY")
        page_str = f"Page {self._pageNumber} of {total_pages}"
        self.drawRightString(559, 21, page_str)
        self.restoreState()


def build_pdf(filename: str):
    doc = SimpleDocTemplate(
        filename,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()

    # Colors
    primary_color = colors.HexColor("#0F172A")    # Slate 900
    brand_blue = colors.HexColor("#1D4ED8")       # Blue 700
    subtext_color = colors.HexColor("#334155")    # Slate 700

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=17,
        leading=21,
        textColor=primary_color,
        spaceAfter=2
    )

    subtitle_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#475569"),
        spaceAfter=6
    )

    h1_style = ParagraphStyle(
        'SecHeading',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=15,
        textColor=brand_blue,
        spaceBefore=7,
        spaceAfter=4,
        keepWithNext=True
    )

    body_style = ParagraphStyle(
        'BodyDark',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=11.5,
        textColor=subtext_color,
        spaceAfter=4
    )

    table_header_style = ParagraphStyle(
        'THStyle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=7.5,
        leading=10,
        textColor=colors.white
    )

    table_cell_style = ParagraphStyle(
        'TDStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7.5,
        leading=10.5,
        textColor=primary_color
    )

    table_cell_bold = ParagraphStyle(
        'TDBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=7.5,
        leading=10.5,
        textColor=primary_color
    )

    callout_style = ParagraphStyle(
        'CalloutText',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=11.5,
        textColor=colors.HexColor("#1E3A8A")
    )

    story = []

    # ========================== PAGE 1 ==========================
    # Title Banner
    story.append(Paragraph("USD/INR Rollover Watcher & Execution Engine", title_style))
    story.append(Paragraph("System Logic, Mathematical Calculations, & Operator Reference Guide", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=1.2, color=brand_blue, spaceBefore=0, spaceAfter=6))

    # SECTION 1: WHAT IS A ROLLOVER & LOT SIZING
    story.append(Paragraph("1. Core Concept: What is a Futures Rollover & How Do Orders Execute?", h1_style))
    story.append(Paragraph(
        "A currency futures contract has a fixed monthly expiry date. To maintain a foreign exchange hedge without holding cash delivery, "
        "an expiring position must be rolled forward into the next month. The automated engine executes both legs in <b>one unified clip</b>:<br/>"
        "&nbsp;&nbsp;• <b>Near Leg (Oct FUT):</b> <b>SELL</b> at the Bid to close the expiring contract.<br/>"
        "&nbsp;&nbsp;• <b>Far Leg (Nov FUT):</b> <b>BUY</b> at the Ask to establish the new forward contract.",
        body_style
    ))

    # Lot definition box
    lot_box = [
        [Paragraph("<b>CORE SIZING LOGIC: LOTS VS. QUANTITY</b>", table_cell_bold)],
        [Paragraph(
            "• <b>1 Lot</b> = <b>1,000 USD Units</b> (Exchange standard contract size for USDINR).<br/>"
            "• <b>Your Total Holding:</b> <b>4,000 Lots</b> = <b>4,000,000 Quantity</b> (40 Lakh USD).<br/>"
            "• <b>Order Execution (1 Clip):</b> The bot is set to <code>lots: 1</code>. It sends <b>1,000 quantity in one go</b> (SELL 1,000 Oct / BUY 1,000 Nov).<br/>"
            "• <b>Post-Trade Position:</b> Leaves 3,999 Lots in October. The app safely halts after 1 clip due to <code>max_clips_per_day: 1</code>.",
            callout_style
        )]
    ]
    t_lot = Table(lot_box, colWidths=[523])
    t_lot.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#EFF6FF")),
        ('BOX', (0, 0), (-1, -1), 0.8, colors.HexColor("#93C5FD")),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(t_lot)
    story.append(Spacer(1, 4))

    # SECTION 2: MATHEMATICAL CALCULATION BREAKDOWN
    story.append(Paragraph("2. Mathematical Calculation Breakdown (Every Metric on Your Screen)", h1_style))

    calc_table_data = [
        [Paragraph("Metric / Number", table_header_style),
         Paragraph("Formula & Derivation", table_header_style),
         Paragraph("Live Example", table_header_style),
         Paragraph("What It Means to the Trader", table_header_style)],

        [Paragraph("<b>Near Leg (Oct FUT)</b>", table_cell_bold),
         Paragraph("Marketable exit hits Bid:<br/><code>Near Bid = 94.9500</code> (Ask = 94.9550)", table_cell_style),
         Paragraph("<b>94.9500</b><br/>(10,000 resting)", table_cell_style),
         Paragraph("The rupee price you receive when selling 1 lot of October futures.", table_cell_style)],

        [Paragraph("<b>Far Leg (Nov FUT)</b>", table_cell_bold),
         Paragraph("Marketable entry lifts Ask:<br/><code>Far Ask = 95.4225</code> (Bid = 95.4025)", table_cell_style),
         Paragraph("<b>95.4225</b><br/>(100,000 resting)", table_cell_style),
         Paragraph("The rupee price you pay when buying 1 lot of November futures.", table_cell_style)],

        [Paragraph("<b>Roll Cost (Spread)</b>", table_cell_bold),
         Paragraph("<code>Far Ask - Near Bid</code><br/>= 95.4225 - 94.9500", table_cell_style),
         Paragraph("<b>0.4725 Rs</b>", table_cell_bold),
         Paragraph("The carry spread in rupees per dollar to switch contracts.", table_cell_style)],

        [Paragraph("<b>Reference Mid-Price</b>", table_cell_bold),
         Paragraph("Near contract mid:<br/><code>(94.9500 + 94.9550) / 2</code>", table_cell_style),
         Paragraph("<b>94.9525 Rs</b>", table_cell_style),
         Paragraph("The benchmark price used to convert rupee spread into basis points.", table_cell_style)],

        [Paragraph("<b>Cost in Basis Points</b><br/>(bps)", table_cell_bold),
         Paragraph("<code>(Roll Cost / Mid) x 10,000</code><br/>= (0.4725 / 94.9525) x 10,000", table_cell_style),
         Paragraph("<b>49.8 bps</b><br/>(0.498%)", table_cell_bold),
         Paragraph("Standard metric for roll expense (1 bps = 0.01% of underlying).", table_cell_style)],

        [Paragraph("<b>Cost in Rs per Lot</b>", table_cell_bold),
         Paragraph("<code>Roll Cost x Lot Size</code><br/>= 0.4725 x 1,000", table_cell_style),
         Paragraph("<b>Rs 472.50</b><br/>per lot", table_cell_bold),
         Paragraph("Total out-of-pocket carry cost to roll 1 lot (1,000 USD).", table_cell_style)],

        [Paragraph("<b>Configured Roll Limit</b><br/>(Target Ceiling)", table_cell_bold),
         Paragraph("<code>(Limit bps / 10,000) x Mid</code><br/>= (47 / 10,000) x 94.9525", table_cell_style),
         Paragraph("<b>47 bps</b><br/>(= 0.4462 Rs)", table_cell_bold),
         Paragraph("The maximum spread you permit. Bot will reject anything higher.", table_cell_style)],

        [Paragraph("<b>Market Status & Gap</b>", table_cell_bold),
         Paragraph("<code>Current Cost - Limit</code><br/>= 49.8 bps - 47.0 bps", table_cell_style),
         Paragraph("<b>WAITING</b><br/>2.8 bps too dear", table_cell_bold),
         Paragraph("Market is 2.8 bps more expensive than your limit. App holds safely.", table_cell_style)],

        [Paragraph("<b>Target Execution Price</b>", table_cell_bold),
         Paragraph("<code>Near Bid + Limit Spread</code><br/>= 94.9500 + 0.4462", table_cell_style),
         Paragraph("<b>95.3962</b><br/>(or below)", table_cell_style),
         Paragraph("If Far Ask drops to <= 95.3962 (or Oct Bid rises), the trade fires.", table_cell_style)]
    ]

    t_calc = Table(calc_table_data, colWidths=[95, 160, 110, 158])
    t_calc.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(t_calc)
    story.append(Spacer(1, 4))

    # SECTION 3: TRIGGER RULE
    story.append(Paragraph("3. Execution Condition: When Does the System Trigger?", h1_style))
    story.append(Paragraph(
        "The system executes when: &nbsp;<b><code>Observed Roll Cost (bps) &lt;= Configured Roll Limit (bps)</code></b>.<br/>"
        "• If market is at 49.8 bps and limit is 47 bps: <b>Holds safely in WAITING state</b> (protects you from paying too much).<br/>"
        "• If limit is updated to 50 bps (or market spread narrows to &lt;= 47 bps): <b>Qualifies immediately</b> and fires the order.",
        body_style
    ))

    # ========================== PAGE 2 ==========================
    story.append(PageBreak())

    # SECTION 4: SAFETY GATES & CAPITAL PROTECTION
    story.append(Paragraph("4. Institutional Safety Gates & Built-in Capital Protections", h1_style))
    story.append(Paragraph(
        "Before sending any live order to the exchange, the engine automatically checks every safety gate:",
        body_style
    ))

    gates_data = [
        [Paragraph("Protection Gate", table_header_style),
         Paragraph("Configured Setting", table_header_style),
         Paragraph("Safety Guardrail & Purpose", table_header_style)],

        [Paragraph("<b>Daily Clip Cap</b>", table_cell_bold),
         Paragraph("<code>max_clips_per_day: 1</code>", table_cell_style),
         Paragraph("Prevents runaway trading. After 1 lot completes, the engine stops automatically for the day.", table_cell_style)],

        [Paragraph("<b>Book Liquidity Gate</b>", table_cell_bold),
         Paragraph("<code>require_touch_size: true</code>", table_cell_style),
         Paragraph("Ensures resting book size at the touch exceeds clip size (1,000 units), preventing slippage.", table_cell_style)],

        [Paragraph("<b>Circuit Band Check</b>", table_cell_bold),
         Paragraph("Exchange Floor / Ceiling", table_cell_style),
         Paragraph("Validates that buy/sell limits are strictly within exchange upper & lower price limits.", table_cell_style)],

        [Paragraph("<b>Auto-Disarm Timer</b>", table_cell_bold),
         Paragraph("<code>arm_timeout_sec: 120</code>", table_cell_style),
         Paragraph("When armed in LIVE mode, it automatically disarms after 2 minutes if the spread does not qualify.", table_cell_style)],

        [Paragraph("<b>Two-Leg Fail-Safe</b>", table_cell_bold),
         Paragraph("<code>auto_unwind: false</code>", table_cell_style),
         Paragraph("If Leg 1 fills and Leg 2 experiences network disruption, the bot halts immediately without guessing.", table_cell_style)],

        [Paragraph("<b>Margin Gate</b>", table_cell_bold),
         Paragraph("Pre-trade check", table_cell_style),
         Paragraph("Verifies available free margin (Rs 25.70 Lakh) easily covers the required margin (Rs 4,572 per lot).", table_cell_style)],
    ]
    t_gates = Table(gates_data, colWidths=[120, 125, 278])
    t_gates.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(t_gates)
    story.append(Spacer(1, 6))

    # SECTION 5: OPERATOR WORKFLOW
    story.append(Paragraph("5. Operator Step-by-Step Workflow (How to Trade via GUI)", h1_style))

    flow_data = [
        [Paragraph("Step", table_header_style),
         Paragraph("Action in Application Window (dist/roll_app.exe)", table_header_style),
         Paragraph("Expected Screen Response", table_header_style)],

        [Paragraph("<b>Step 1</b>", table_cell_bold),
         Paragraph("<b>Launch <code>roll_app.exe</code></b><br/>Verify October & November contract tokens and live market prices.", table_cell_style),
         Paragraph("Near Bid, Far Ask, and current Roll Cost (e.g. 0.4725 / 49.8 bps) update in real time.", table_cell_style)],

        [Paragraph("<b>Step 2</b>", table_cell_bold),
         Paragraph("<b>Set / Verify Roll Limit</b><br/>Enter target bps (e.g. <code>47 bps</code> to wait, or <code>50 bps</code> for immediate fill) and click <b>Set</b>.", table_cell_style),
         Paragraph("The limit card updates with the new rupee threshold and recalculates the difference in bps.", table_cell_style)],

        [Paragraph("<b>Step 3</b>", table_cell_bold),
         Paragraph("<b>Switch from DRY RUN to LIVE</b><br/>Click the <b>DRY RUN</b> toggle at top. A safety preflight dialog opens.<br/>Type <code>LIVE</code> in the box and click Confirm.", table_cell_style),
         Paragraph("The mode indicator changes to a bold red <b>LIVE</b> badge. System is authorised to place orders.", table_cell_style)],

        [Paragraph("<b>Step 4</b>", table_cell_bold),
         Paragraph("<b>Click ARM</b><br/>System arms for 120 seconds.", table_cell_style),
         Paragraph("As soon as Cost &lt;= Limit, it sells 1,000 Oct / buys 1,000 Nov, logs fills, and halts safely.", table_cell_style)]
    ]
    t_flow = Table(flow_data, colWidths=[48, 235, 240])
    t_flow.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(t_flow)
    story.append(Spacer(1, 6))

    # SECTION 6: FAQ & SUMMARY
    story.append(Paragraph("6. Trader Quick Reference & FAQ", h1_style))
    faq_box = [
        [Paragraph(
            "• <b>Will the bot accidentally roll all 4,000 lots at once?</b><br/>"
            "&nbsp;&nbsp;<b>No.</b> Order size is strictly controlled by <code>lots: 1</code> (1,000 quantity = 1 lot). "
            "With <code>max_clips_per_day: 1</code>, it executes only 1 lot and stops.<br/>"
            "• <b>Why does the order payload say 1,000 quantity instead of 1 lot?</b><br/>"
            "&nbsp;&nbsp;Choice Broking exchange API requires order quantity in base units (1 lot = 1,000 units). "
            "The app lets you think in lots and translates to units automatically.<br/>"
            "• <b>What does '2.8 bps too dear' mean?</b><br/>"
            "&nbsp;&nbsp;'Too dear' means too expensive. The live spread (49.8 bps) is 2.8 bps higher than your limit (47.0 bps). The app holds until the market reaches your price.",
            body_style
        )]
    ]
    t_faq = Table(faq_box, colWidths=[523])
    t_faq.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F1F5F9")),
        ('BOX', (0, 0), (-1, -1), 0.8, colors.HexColor("#CBD5E1")),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(t_faq)

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Generated PDF: {filename}")


if __name__ == "__main__":
    out_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target_pdf = os.path.join(out_dir, "USDINR_Rollover_System_Guide.pdf")
    build_pdf(target_pdf)

    dist_dir = os.path.join(out_dir, "dist")
    if os.path.isdir(dist_dir):
        dist_pdf = os.path.join(dist_dir, "USDINR_Rollover_System_Guide.pdf")
        build_pdf(dist_pdf)
