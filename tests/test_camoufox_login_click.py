"""session-ended 页面点击 Log in 的回归测试。

线上 283 次 "未找到可点击的 Log in" 的真实原因不是元素找不到：Playwright 的
click 在点下去之后还要等 <a href> 触发的跨域跳转链跑完，走代理时经常超过 10s
的超时值。旧代码把这个超时当成定位器失败，换下一个定位器对着正在跳转的页面
再点一次，四个轮完才报错——而按钮其实已经被点了最多 3 次。

这里的用例锁死修复后的判据：成败看「页面有没有离开 session-ended」，不看
click 本身是正常返回还是超时。
"""
import unittest
from unittest.mock import Mock, patch

import auth_flow


# Playwright 在导航没跑完时抛出的超时消息，关键是结尾那两行：点击其实完成了。
NAV_TIMEOUT_MESSAGE = """Locator.click: Timeout 10000ms exceeded.
Call log:
  - waiting for get_by_role("link", name="Log in", exact=True).first
    - locator resolved to <a href="https://chatgpt.com/auth/login_with">Log in</a>
  - attempting click action
    - waiting for element to be visible, enabled and stable
    - element is visible, enabled and stable
    - scrolling into view if needed
    - done scrolling
    - performing click action
    - click action done
    - waiting for scheduled navigations to finish
"""

SESSION_ENDED_URL = "https://auth.openai.com/create-account"
AFTER_LOGIN_URL = "https://chatgpt.com/auth/login_with?callback_path=/"


def _page(url=SESSION_ENDED_URL, *, click_side_effect=None, count=1, visible=True):
    """造一个只实现本 helper 会用到的那几个方法的假 page。

    注意第 4 个定位器是 `page.locator(...).first`，helper 随后还会再取一次
    `.first`。裸 Mock 上多取一层会拿到一个全新的自动 Mock（它的 count() 返回
    Mock 对象，恒为真），桩就失效了。这里让 loc.first 指回 loc 自身，四个
    定位器才都落在同一套桩上。
    """
    page = Mock()
    page.url = url

    def _make():
        loc = Mock()
        loc.count.return_value = count
        loc.is_visible.return_value = visible
        loc.first = loc          # .first 指向自己，避免多取一层变成自动 Mock
        if click_side_effect is not None:
            loc.click.side_effect = click_side_effect
        return loc

    locator = _make()
    page.get_by_role.return_value = locator
    page.get_by_text.return_value = locator
    page.locator.return_value = locator
    return page, locator


class ClickSessionEndedLoginTests(unittest.TestCase):
    def setUp(self):
        # 真实宽限期是 8s / 25s，这里压到 0.1s，否则每个超时用例都要空等几十秒。
        for attr in ("_LOGIN_NAV_GRACE_S", "_LOGIN_NAV_FINAL_GRACE_S"):
            patcher = patch.object(auth_flow, attr, 0.1)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_plain_success(self):
        page, locator = _page()
        self.assertTrue(auth_flow.click_session_ended_login(page))
        locator.click.assert_called_once()

    def test_navigation_timeout_then_page_leaves_counts_as_success(self):
        """核心回归：click 超时，但宽限期内页面跳走了 -> 成功，且只点一次。

        旧代码在这里会继续试下一个定位器，对着正在跳转的页面重复点击。
        """
        page, locator = _page()
        calls = {"n": 0}

        def _slow_nav(*_args, **_kwargs):
            calls["n"] += 1
            raise TimeoutError(NAV_TIMEOUT_MESSAGE)

        locator.click.side_effect = _slow_nav
        # 宽限期第二次探测时 URL 才变 —— 模拟"导航在跑，只是慢"。
        urls = [SESSION_ENDED_URL, SESSION_ENDED_URL, AFTER_LOGIN_URL]
        type(page).url = property(lambda _self: urls.pop(0) if len(urls) > 1 else urls[0])

        self.assertTrue(auth_flow.click_session_ended_login(page))
        self.assertEqual(calls["n"], 1)

    def test_click_done_but_navigation_never_happens_retries_next_locator(self):
        """click action done 不等于跳转一定发生。

        复现 round 8：#1 点完页面纹丝不动（导航被中止），换 #3 再点就成了。
        所以宽限期结束还没走掉时必须换定位器，不能直接返回成功。
        """
        page, locator = _page()
        attempts = {"n": 0}

        def _click(*_args, **_kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                # 第一次：点击完成，但导航被中止，页面不动。
                raise TimeoutError(NAV_TIMEOUT_MESSAGE)
            page.url = AFTER_LOGIN_URL

        locator.click.side_effect = _click

        self.assertTrue(auth_flow.click_session_ended_login(page))
        self.assertEqual(attempts["n"], 2, "第一个定位器没跳走时应换下一个重试")

    def test_slow_navigation_still_wins_after_all_locators_tried(self):
        """四个定位器都试完，但导航其实还在飞 —— 最后一轮等待里跳走了就算成功。

        复现 round 3：#1 点完页面没动，#3/#4 卡在 "navigation to finish"，
        说明导航还在跑。这时直接判失败会白丢一个已经注册好的号。
        """
        page, locator = _page()
        state = {"clicks": 0, "reads_after": 0}

        def _click(*_args, **_kwargs):
            state["clicks"] += 1
            if state["clicks"] == 1:
                raise TimeoutError(NAV_TIMEOUT_MESSAGE)
            # 后续定位器卡在导航等待上 —— 证明点击确实生效了。
            raise TimeoutError(
                'Locator.click: Timeout 10000ms exceeded.\nCall log:\n'
                '  - waiting for" https://chatgpt.com/auth/login_with"'
                ' navigation to finish...\n'
            )

        locator.click.side_effect = _click
        # URL 必须在「四个定位器试完、且已进入最后那轮兜底等待」之后才变。
        # 循环末尾本来就有一次 _left_session_ended_page，若在那时就变会被它接住，
        # 测不到兜底分支 —— 所以多放过几次读取，再用日志断言锁死走的是兜底。
        def _url(_self):
            if state["clicks"] < 4:
                return SESSION_ENDED_URL
            state["reads_after"] += 1
            return SESSION_ENDED_URL if state["reads_after"] <= 3 else AFTER_LOGIN_URL
        type(page).url = property(_url)

        with self.assertLogs("auth_flow", level="WARNING") as caught:
            self.assertTrue(auth_flow.click_session_ended_login(page))
        self.assertEqual(state["clicks"], 4)
        self.assertTrue(
            any("再等" in line and "观察导航" in line for line in caught.output),
            "应当走最后那轮兜底等待，而不是被循环末尾的检查提前接住",
        )

    def test_click_done_but_never_navigates_at_all_fails(self):
        """四个定位器点完页面都没动 -> 必须报失败，不能谎报成功。"""
        page, locator = _page(click_side_effect=TimeoutError(NAV_TIMEOUT_MESSAGE))

        self.assertFalse(auth_flow.click_session_ended_login(page))
        self.assertEqual(locator.click.call_count, 4)

    def test_timeout_is_success_when_page_already_navigated(self):
        """异常文本认不出来时，退回用 URL 判断：页面走掉了就是成功。"""
        page, locator = _page(click_side_effect=TimeoutError("Timeout 10000ms exceeded."))
        # 模拟点击触发了跳转：click 抛错之后 URL 已经变了。
        def _navigate(*_args, **_kwargs):
            page.url = AFTER_LOGIN_URL
            raise TimeoutError("Timeout 10000ms exceeded.")
        locator.click.side_effect = _navigate

        self.assertTrue(auth_flow.click_session_ended_login(page))
        self.assertEqual(locator.click.call_count, 1)

    def test_real_click_failure_still_fails(self):
        """点击是真的没打出去（元素被遮挡），页面也没动 -> 必须报失败。"""
        page, locator = _page(click_side_effect=TimeoutError(
            "Locator.click: Timeout 10000ms exceeded.\n"
            "Call log:\n  - attempting click action\n"
            "    - element intercepts pointer events\n"
        ))

        self.assertFalse(auth_flow.click_session_ended_login(page))
        # 四个定位器都试过了才放弃。
        self.assertEqual(locator.click.call_count, 4)

    def test_returns_true_without_clicking_if_already_left_page(self):
        """进来时页面已经不在 session-ended 了，不该再点。"""
        page, locator = _page(url=AFTER_LOGIN_URL)

        self.assertTrue(auth_flow.click_session_ended_login(page))
        locator.click.assert_not_called()

    def test_missing_element_is_not_clicked(self):
        page, locator = _page(count=0, visible=False)

        self.assertFalse(auth_flow.click_session_ended_login(page))
        locator.click.assert_not_called()

    def test_deadline_is_checked(self):
        page, _ = _page()
        seen = []
        auth_flow.click_session_ended_login(
            page, check_deadline=lambda label: seen.append(label)
        )
        self.assertEqual(seen, ["login_click"])

    def test_deadline_exception_propagates(self):
        """全局超时必须能中断这里，不能被 helper 吞掉。"""
        page, _ = _page()

        def _boom(_label):
            raise RuntimeError("Camoufox 全局超时")

        with self.assertRaises(RuntimeError):
            auth_flow.click_session_ended_login(page, check_deadline=_boom)


class LeftSessionEndedPageTests(unittest.TestCase):
    def test_create_account_url_means_still_on_page(self):
        self.assertFalse(auth_flow._left_session_ended_page(Mock(url=SESSION_ENDED_URL)))

    def test_other_url_means_left(self):
        self.assertTrue(auth_flow._left_session_ended_page(Mock(url=AFTER_LOGIN_URL)))

    def test_blank_url_is_not_treated_as_left(self):
        self.assertFalse(auth_flow._left_session_ended_page(Mock(url="")))

    def test_localized_title_does_not_matter(self):
        """标题会按出口 IP 本地化（实测拿到过泰语），判据只看 URL。"""
        page = Mock(url=AFTER_LOGIN_URL)
        page.title.return_value = "เซสชันของคุณสิ้นสุดแล้ว - OpenAI"
        self.assertTrue(auth_flow._left_session_ended_page(page))


if __name__ == "__main__":
    unittest.main()
