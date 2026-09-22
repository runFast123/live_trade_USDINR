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

    def broker(self, verified=True, **kw):
        log = Log()
        broker = Broker(config(**kw), log)
        broker.build_client()
        # Whether the broker HONOURS the session is a separate question with
        # its own tests below; these are about which account it belongs to,
        # and they must not reach the network to find out.
        if verified is not None:
            broker.verify_session = lambda: ((True, "stubbed") if verified
                                             else (False, "rejected"))
        return broker, log

    def raw_broker(self, **kw):
        """Unstubbed, for the tests that exercise verify_session itself."""
        return self.broker(verified=None, **kw)

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


class TestTheBrokerHasToHonourIt(SessionCase):
    """logged_in used to be bool(session_id) -- a string being present.

    A session that every endpoint refused with "Unauthorized, VendorId
    doesn't exists" still reported itself logged in, so the session gate --
    one of the checks standing between the app and a live order -- passed
    while nothing worked at all. Seen on a real account.
    """

    def test_a_session_the_broker_refuses_is_not_resumed(self):
        broker, log = self.broker(verified=False)
        self.save_for(broker)
        self.assertFalse(broker.resume(self.path))
        self.assertIn("not accepted by the broker", log.text())

    def test_and_it_no_longer_claims_to_be_logged_in(self):
        """load_session put the dead id on the client; leaving it there
        meant logged_in kept saying yes about a session the broker had just
        called worthless."""
        broker, _ = self.broker(verified=False)
        self.save_for(broker)
        broker.resume(self.path)
        self.assertFalse(broker.logged_in)

    def test_and_the_dead_session_file_is_removed(self):
        broker, _ = self.broker(verified=False)
        self.save_for(broker)
        broker.resume(self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_a_session_it_honours_is_resumed(self):
        broker, _ = self.broker(verified=True)
        self.save_for(broker)
        self.assertTrue(broker.resume(self.path))

    def test_an_unreachable_broker_is_not_a_rejection(self):
        """A network blip must not force a fresh login every time."""
        broker, log = self.broker()
        broker.verify_session = lambda: (None, "connection reset")
        self.save_for(broker)
        self.assertTrue(broker.resume(self.path))
        self.assertIn("could not be reached", log.text())

    def test_logged_in_is_false_once_the_broker_has_refused(self):
        broker, _ = self.broker()
        broker.client.session_id = "SID"
        broker._session_ok = False
        self.assertFalse(broker.logged_in)

    def test_logged_in_is_true_when_it_has_been_honoured(self):
        broker, _ = self.broker()
        broker.client.session_id = "SID"
        broker._session_ok = True
        self.assertTrue(broker.logged_in)

    def test_an_unchecked_session_still_counts_as_logged_in(self):
        """The gates that follow fail on their own if nothing can be read."""
        broker, _ = self.broker()
        broker.client.session_id = "SID"
        broker._session_ok = None
        self.assertTrue(broker.logged_in)

    def test_no_session_id_is_never_logged_in(self):
        broker, _ = self.broker()
        broker.client.session_id = None
        self.assertFalse(broker.logged_in)

    def answering(self, broker, funds=None, positions=None, orders=None):
        """Point every endpoint verify_session tries at a given behaviour."""
        def endpoint(behaviour, method):
            class Stub:
                pass
            stub = Stub()
            setattr(stub, method,
                    behaviour if behaviour is not None
                    else (lambda *a, **kw: (_ for _ in ()).throw(
                        OSError("connection reset by peer"))))
            return stub
        broker.client.funds = endpoint(funds, "get_funds_view")
        broker.client.portfolio = endpoint(positions, "get_net_position")
        broker.client.orders = endpoint(orders, "get_order_book")

    def refuse(self):
        def call(*a, **kw):
            raise Exception("HTTP Request failed: 401 Client Error. "
                            "Response: Unauthorized, VendorId doesn't exists")
        return call

    def unreachable(self):
        def call(*a, **kw):
            raise OSError("connection reset by peer")
        return call

    def test_an_unauthorised_reply_from_every_endpoint_is_a_rejection(self):
        broker, _ = self.raw_broker()
        broker.client.session_id = "SID"
        r = self.refuse()
        self.answering(broker, funds=r, positions=r, orders=r)
        ok, why = broker.verify_session()
        self.assertFalse(ok)
        self.assertIn("Unauthorized", why)

    def test_one_endpoint_refusing_is_not_a_dead_session(self):
        """Declaring a live session dead locks the operator out of a working
        account, so any endpoint honouring it is enough."""
        broker, _ = self.raw_broker()
        broker.client.session_id = "SID"
        self.answering(broker, funds=self.refuse(),
                       positions=lambda *a, **kw: {"Status": "Success"},
                       orders=self.refuse())
        ok, why = broker.verify_session()
        self.assertTrue(ok)
        self.assertIn("positions", why)

    def test_a_connection_failure_is_not_a_rejection(self):
        broker, _ = self.raw_broker()
        broker.client.session_id = "SID"
        u = self.unreachable()
        self.answering(broker, funds=u, positions=u, orders=u)
        ok, _ = broker.verify_session()
        self.assertIsNone(ok)

    def test_a_good_reply_is_acceptance(self):
        broker, _ = self.raw_broker()
        broker.client.session_id = "SID"
        self.answering(
            broker,
            funds=lambda *a, **kw: {"Status": "Success",
                                    "Response": {"FundsView": {}}},
            positions=self.refuse(), orders=self.refuse())
        ok, _ = broker.verify_session()
        self.assertTrue(ok)


class TestAFreshLoginIsNeverBlocked(SessionCase):
    """A check whose failure has no remedy must not block.

    Refusing a RESUMED session is fair -- the remedy is to log in again, and
    the operator can do that. Refusing a FRESH login leaves no remedy at all:
    it locks them out of their own screen over a check that is not itself the
    safety. The safety is the session gate, which reads logged_in. So they
    are let in to a screen that says plainly it cannot trade, rather than a
    dialog they cannot get past. Reported as exactly that: a login dialog
    refusing to proceed.
    """

    def after_login(self, verdict):
        broker, log = self.broker(verified=None)
        broker.verify_session = lambda: verdict
        saved = {}
        broker.client.save_session = lambda p: saved.setdefault("path", p)
        broker._after_login(self.path)
        return broker, log, saved

    def test_a_refused_session_does_not_raise(self):
        _, log, saved = self.after_login((False, "status=401"))
        self.assertIn("path", saved)          # the session was still saved
        self.assertIn("will not accept the session", log.text())

    def test_and_it_says_nothing_can_trade(self):
        _, log, _ = self.after_login((False, "status=401"))
        self.assertIn("Nothing can trade", log.text())

    def test_an_unchecked_session_is_only_a_warning(self):
        _, log, saved = self.after_login((None, "connection reset"))
        self.assertIn("path", saved)
        self.assertIn("could not be checked", log.text())

    def test_a_good_session_says_nothing_alarming(self):
        _, log, saved = self.after_login((True, "accepted"))
        self.assertIn("path", saved)
        self.assertNotIn("will not accept", log.text())


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
