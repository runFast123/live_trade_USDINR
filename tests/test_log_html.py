"""A broker error page is logged as one line, not as a web page.

When one of Choice's gateways fails, the vendor client raises with the whole
response body in the message, and that body is nginx's HTML error page. On
28 Sep fifteen of them went into the log file and the log pane, stylesheet
and all, pushing the lines that mattered out of view. The call, the status
code and the URL are what anyone needs; the markup is noise.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest

from rollover.logbook import Logbook, without_html

# Verbatim from the log, 28 Sep 2026 16:48:23.
PAGE_502 = """HTTP Request failed: 502 Server Error: Bad Gateway for url: https://finxomne.choiceindia.com/api/OpenAPI/GetMargin
Response: <!DOCTYPE html>
<html>
<head>
<title>Error</title>
<style>
    body {
        width: 35em;
        margin: 0 auto;
        font-family: Tahoma, Verdana, Arial, sans-serif;
    }
</style>
</head>
<body>
<h1>An error occurred.</h1>
<p>Sorry, the page you are looking for is currently unavailable.<br/>
Please try again later.</p>
<p>If you are the system administrator of this resource then you should check
the error log for details.</p>
<p><em>Faithfully yours, nginx.</em></p>
</body>
</html>"""


class TestThePageIsReducedToItsWords(unittest.TestCase):
    def setUp(self):
        self.line = without_html("Margin call failed: " + PAGE_502)

    def test_it_is_one_line(self):
        self.assertNotIn("\n", self.line)

    def test_the_call_the_status_and_the_url_are_kept(self):
        self.assertIn("Margin call failed", self.line)
        self.assertIn("502 Server Error: Bad Gateway", self.line)
        self.assertIn("/api/OpenAPI/GetMargin", self.line)

    def test_the_page_says_what_it_said(self):
        self.assertIn("HTML error page", self.line)
        self.assertIn("Error", self.line)
        self.assertIn("An error occurred.", self.line)

    def test_no_markup_or_stylesheet_survives(self):
        for fragment in ("<", "margin: 0 auto", "font-family", "nginx",
                         "Response:"):
            self.assertNotIn(fragment, self.line, fragment)


class TestOrdinaryMessagesAreUntouched(unittest.TestCase):
    def test_plain_text(self):
        text = "Quotes now coming from the live feed."
        self.assertIs(without_html(text), text)

    def test_a_json_body_is_kept_whole(self):
        """A broker refusal in JSON carries the reason, and must stay."""
        text = ("HTTP Request failed: 401 Client Error\nResponse: "
                '{"Status": "Fail", "Reason": "Unauthorized, VendorId doesn\'t exists"}')
        self.assertEqual(without_html(text), text)

    def test_a_comparison_is_not_mistaken_for_markup(self):
        text = "cost 29.7 bps < limit 30 bps and depth > clip"
        self.assertEqual(without_html(text), text)

    def test_a_page_with_no_title_still_collapses(self):
        line = without_html("Order book call failed: <html><body></body></html>")
        self.assertEqual(line, "Order book call failed: "
                               "[the broker sent an HTML error page: "
                               "nothing readable on it]")


class TestTheLogbookUsesIt(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loghtml_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = Logbook(self.dir)

    def test_the_file_gets_one_line(self):
        self.log.warn("Margin call failed: " + PAGE_502)
        with open(self.log.path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("502 Server Error", lines[0])

    def test_the_pane_gets_one_line_too(self):
        self.log.warn("Margin call failed: " + PAGE_502)
        (_stamp, level, message), = self.log.recent()
        self.assertEqual(level, "WARN")
        self.assertNotIn("<", message)

    def test_an_alert_is_reduced_the_same_way(self):
        """A halt that quotes the exception must still be readable."""
        self.log.alert("HALTED: startup failed: " + PAGE_502)
        self.assertIn("502 Server Error", self.log.last_alert)
        self.assertNotIn("<style>", self.log.last_alert)


if __name__ == "__main__":
    unittest.main(verbosity=2)
