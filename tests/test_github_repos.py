import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import github_repos as g  # noqa: E402


def resp(status=200, headers=None, text="", body=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, text=text,
                           json=lambda: body)


def row(name, stars=0, views=None, **kw):
    base = {"name": name, "stars": stars, "views": views, "stars_delta": None,
            "views_all": None, "uniques": None, "clones": None, "clones_all": None,
            "forks": 0, "issues": 0, "updated": "", "language": "",
            "fork": False, "private": False, "archived": False}
    base.update(kw)
    return base


class SortTests(unittest.TestCase):
    def test_stars_then_views_then_name(self):
        rows = [row("b", 5, 1), row("a", 5, 1), row("c", 5, 9), row("d", 10, 0)]
        self.assertEqual([r["name"] for r in g.sort_rows(rows, "stars")], ["d", "c", "a", "b"])

    def test_missing_values_sort_last(self):
        rows = [row("x", views=None), row("y", views=0), row("z", views=3)]
        self.assertEqual([r["name"] for r in g.sort_rows(rows, "views")], ["z", "y", "x"])

    def test_name_sort_is_case_insensitive_ascending(self):
        rows = [row("beta"), row("Alpha"), row("gamma")]
        self.assertEqual([r["name"] for r in g.sort_rows(rows, "name")], ["Alpha", "beta", "gamma"])


class RateLimitTests(unittest.TestCase):
    def test_retry_after(self):
        self.assertEqual(g.rate_limit_wait(resp(429, {"Retry-After": "10"})), 11)

    def test_primary_limit_uses_reset_header(self):
        r = resp(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"})
        self.assertEqual(g.rate_limit_wait(r, now=1000), 101)

    def test_wait_is_capped(self):
        r = resp(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "999999"})
        self.assertEqual(g.rate_limit_wait(r, now=0), g.MAX_WAIT)

    def test_secondary_limit_from_body(self):
        self.assertEqual(g.rate_limit_wait(resp(403, text="You have exceeded a secondary rate limit")), 30)

    def test_plain_403_is_not_a_rate_limit(self):
        self.assertIsNone(g.rate_limit_wait(resp(403, text="Must have push access to view traffic")))

    def test_success_is_not_a_rate_limit(self):
        self.assertIsNone(g.rate_limit_wait(resp(200)))


class FakeGH:
    def __init__(self, responses):
        self.responses = responses

    def get(self, url, params=None, accept=None):
        return self.responses[url.rsplit("/", 1)[1]]


class TrafficFetchTests(unittest.TestCase):
    def test_forbidden_is_none_not_zero(self):
        gh = FakeGH({"views": resp(403)})
        self.assertEqual(g.fetch_traffic(gh, "u/r"), (None, "403"))

    def test_success(self):
        gh = FakeGH({
            "views": resp(body={"count": 7, "uniques": 3, "views": [{"timestamp": "2026-10-01T00:00:00Z", "count": 7, "uniques": 3}]}),
            "clones": resp(body={"count": 2, "uniques": 1, "clones": []}),
        })
        data, err = g.fetch_traffic(gh, "u/r")
        self.assertIsNone(err)
        self.assertEqual(data["views"]["count"], 7)
        self.assertEqual(data["clones"]["uniques"], 1)


def window(*days):
    return {"views": {"count": 0, "uniques": 0, "days": [
                {"timestamp": f"{d}T00:00:00Z", "count": c, "uniques": c} for d, c in days]},
            "clones": {"count": 0, "uniques": 0, "days": []}}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = g.open_db(":memory:")

    def test_windows_merge_and_latest_day_is_replaced(self):
        g.store_traffic(self.conn, "u/r", window(("2026-10-01", 5), ("2026-10-02", 2)))
        # Later run: day 1 dropped out of the API window, day 2 is now complete.
        g.store_traffic(self.conn, "u/r", window(("2026-10-02", 6), ("2026-10-03", 1)))
        self.assertEqual(g.traffic_total(self.conn, "u/r", "views"), 5 + 6 + 1)

    def test_total_is_none_without_data(self):
        self.assertIsNone(g.traffic_total(self.conn, "u/r", "views"))

    def test_star_delta(self):
        g.store_stars(self.conn, "u/r", 10, date(2026, 9, 1))
        g.store_stars(self.conn, "u/r", 14, date(2026, 9, 25))
        today = date(2026, 10, 22)
        self.assertEqual(g.star_delta(self.conn, "u/r", 20, 30, today), 10)  # vs 2026-09-01
        self.assertEqual(g.star_delta(self.conn, "u/r", 20, 7, today), 6)    # vs 2026-09-25

    def test_star_delta_without_old_snapshot(self):
        g.store_stars(self.conn, "u/r", 10, date(2026, 10, 20))
        self.assertIsNone(g.star_delta(self.conn, "u/r", 12, 30, date(2026, 10, 22)))


class BuildRowsTests(unittest.TestCase):
    def repo(self, **kw):
        base = {"name": "r", "full_name": "u/r", "html_url": "https://github.com/u/r",
                "owner": {"login": "u"}, "stargazers_count": 4, "forks_count": 1,
                "open_issues_count": 2, "language": None, "pushed_at": "2026-10-01T10:00:00Z",
                "fork": False, "private": False, "archived": False,
                "license": {"spdx_id": "NOASSERTION"}, "topics": ["a"]}
        base.update(kw)
        return base

    def test_unavailable_traffic_is_none(self):
        rows = g.build_rows([self.repo()], {"u/r": (None, "403")}, None, 30, date(2026, 10, 22))
        self.assertIsNone(rows[0]["views"])
        self.assertEqual(rows[0]["license"], "")
        self.assertEqual(rows[0]["updated"], "2026-10-01")

    def test_history_is_written_and_read(self):
        conn = g.open_db(":memory:")
        data = window(("2026-10-21", 3))
        data["views"]["count"], data["views"]["uniques"] = 3, 2
        data["clones"]["count"] = 1
        rows = g.build_rows([self.repo()], {"u/r": (data, None)}, conn, 30, date(2026, 10, 22))
        self.assertEqual(rows[0]["views"], 3)
        self.assertEqual(rows[0]["views_all"], 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM stars").fetchone()[0], 1)


class MiscTests(unittest.TestCase):
    def test_monthly_cumulative(self):
        dates = ["2026-01-05", "2026-01-20", "2026-03-02"]
        self.assertEqual(g.monthly_cumulative(dates), [("2026-01", 2, 2), ("2026-03", 1, 3)])

    def test_token_env_precedence(self):
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "a", "GH_TOKEN": "b"}, clear=True):
            self.assertEqual(g.get_token(), "a")
        with mock.patch.dict("os.environ", {"GH_TOKEN": "b"}, clear=True):
            self.assertEqual(g.get_token(), "b")

    def test_token_none_when_nothing_available(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("github_repos.shutil.which", return_value=None):
            self.assertIsNone(g.get_token())

    def test_render_table_shows_dash_for_missing(self):
        out = g.render_table([row("repo", 3, None, fork=True)], 30)
        self.assertIn("-", out.splitlines()[2])
        self.assertTrue(out.splitlines()[2].rstrip().endswith("F"))

    def test_qualify(self):
        self.assertEqual(g.qualify("x", "me"), "me/x")
        self.assertEqual(g.qualify("o/x", "me"), "o/x")


if __name__ == "__main__":
    unittest.main()
