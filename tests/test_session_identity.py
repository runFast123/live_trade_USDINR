"""A saved session belongs to one account, and only that account may reuse it.

The vendor's loader checks the date and nothing else. So changing the
credentials and restarting on the same day silently reused the PREVIOUS
account's token: the app reported itself logged in, read that account's funds
and positions, and would have sent that account's orders, while the operator
believed they were on the new one.

Observed for real -- the credentials and the host were both changed one
afternoon, and a funds check came back describing the old account.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import unittest

from rollover.broker import Broker
from rollover.config import RollConfig


class Log:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(("info", m))
    def warn(self, m): self.lines.append(("warn", m))
    error = alert = warn

    def text(self):
        return " ".join(m for _, m in self.lines)


def config(**kw):
    base = dict(base_url="https://finx.choiceindia.com", vendor_id="M0984",
                api_key="KEY-A", mobile_no="9420000091",
                near_token="1769", far_token="1584")
    base.update(kw)
    return RollConfig(**base)


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="session_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "session.json")

    def broker(self, **kw):
        log = Log()
        broker = Broker(config(**kw), log)
        broker.build_client()
        return broker, log

    def save_for(self, broker, stamped=True, day=None):
        payload = {"date": (day or datetime.date.today()).isoformat(),
                   "session_id": "SID", "access_token": "TOK",
                   "bcast_ip": "1.2.3.4", "bcast_port": 1234}
        if stamped:
            payload["account"] = broker.account_fingerprint()
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)


class TestTheFingerprint(SessionCase):
    def test_the_same_credentials_give_the_same_one(self):
        a, _ = self.broker()
        b, _ = self.broker()
        self.assertEqual(a.account_fingerprint(), b.account_fingerprint())

    def test_a_different_key_gives_a_different_one(self):
        a, _ = self.broker()
        b, _ = self.broker(api_key="KEY-B")
        self.assertNotEqual(a.account_fingerprint(), b.account_fingerprint())

    def test_so_does_a_different_login(self):
        a, _ = self.broker()
        b, _ = self.broker(mobile_no="9998887777")
        self.assertNotEqual(a.account_fingerprint(), b.account_fingerprint())

    def test_and_a_different_host(self):
        """finx and finxomne are different endpoints."""
        a, _ = self.broker()
        b, _ = self.broker(base_url="https://finxomne.choiceindia.com")
        self.assertNotEqual(a.account_fingerprint(), b.account_fingerprint())

    def test_and_a_different_vendor(self):
        a, _ = self.broker()
        b, _ = self.broker(vendor_id="OTHER")
        self.assertNotEqual(a.account_fingerprint(), b.account_fingerprint())

    def test_it_writes_down_no_secret(self):
        broker, _ = self.broker()
        got = broker.account_fingerprint()
        self.assertEqual(len(got), 32)
        for secret in ("KEY-A", "9420000091", "M0984"):
            self.assertNotIn(secret, got)


class TestResumingIt(SessionCase):
    def test_the_account_that_saved_it_may_reuse_it(self):
        broker, _ = self.broker()
        self.save_for(broker)
        self.assertTrue(broker.resume(self.path))

    def test_another_account_may_not(self):
        """The whole point: this used to return True."""
        a, _ = self.broker()
        self.save_for(a)
        b, log = self.broker(api_key="KEY-B",
                             base_url="https://finxomne.choiceindia.com")
        self.assertFalse(b.resume(self.path))
        self.assertIn("does not belong to the account configured now",
                      log.text())

    def test_the_stale_file_is_removed(self):
        """Left in place it would be refused again every start, and it is a
        live token for an account this install is no longer using."""
        a, _ = self.broker()
        self.save_for(a)
        b, _ = self.broker(api_key="KEY-B")
        b.resume(self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_a_session_from_an_older_build_is_refused(self):
        """It carries no stamp, so it cannot be shown to be the right one.
        A fresh login costs one code; being wrong costs the wrong account."""
        broker, log = self.broker()
        self.save_for(broker, stamped=False)
        self.assertFalse(broker.resume(self.path))
        self.assertIn("predates this check", log.text())

    def test_no_session_at_all_is_simply_a_login(self):
        broker, _ = self.broker()
        self.assertFalse(broker.resume(self.path))

    def test_yesterdays_session_is_still_refused(self):
        """The date check has not gone away, it has been added to."""
        broker, _ = self.broker()
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        self.save_for(broker, day=yesterday)
        self.assertFalse(broker.resume(self.path))

    def test_an_unreadable_file_is_refused_rather_than_trusted(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        broker, _ = self.broker()
        self.assertFalse(broker.resume(self.path))


class TestStampingIt(SessionCase):
    def test_a_saved_session_carries_the_account(self):
        broker, _ = self.broker()
        self.save_for(broker, stamped=False)
        broker._stamp_session(self.path)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["account"],
                             broker.account_fingerprint())

    def test_stamping_keeps_everything_else(self):
        broker, _ = self.broker()
        self.save_for(broker, stamped=False)
        broker._stamp_session(self.path)
        with open(self.path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["session_id"], "SID")
        self.assertEqual(saved["access_token"], "TOK")

    def test_a_stamped_session_round_trips(self):
        broker, _ = self.broker()
        self.save_for(broker, stamped=False)
        broker._stamp_session(self.path)
        self.assertTrue(broker.resume(self.path))

    def test_a_failure_to_stamp_is_not_fatal(self):
        """It costs a login next start, which is the safe direction."""
        broker, log = self.broker()
        broker._stamp_session(os.path.join(self.dir, "nowhere", "s.json"))
        self.assertIn("Could not stamp", log.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
