"""验证处理回归测试：浏览器与时间是外部边界，不访问真实站点。"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("lune_app", Path(__file__).resolve().parents[1] / "app.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TurnstileTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.clock = Clock()
        self.enterContext(patch.object(app.time, "time", self.clock.time))
        self.enterContext(patch.object(app.time, "sleep", self.clock.sleep))

    def require(self, name):
        self.assertTrue(callable(getattr(app, name, None)), f"缺少验证处理接口 {name}")
        return getattr(app, name)

    def test_js_keeps_false_instead_of_default(self):
        run_js = self.require("js")
        sb = SimpleNamespace(execute_script=lambda script: False)
        self.assertIs(run_js(sb, "return false;", True), False)

    def test_js_uses_default_for_none(self):
        run_js = self.require("js")
        sb = SimpleNamespace(execute_script=lambda script: None)
        self.assertEqual(run_js(sb, "return null;", "fallback"), "fallback")

    def test_js_falls_back_to_cdp_expression(self):
        run_js = self.require("js")
        def unavailable(script):
            raise RuntimeError("WebDriver unavailable")
        def evaluate(expression):
            if expression == "(function(){ return 7; })()":
                return 7
            raise ValueError("需要完整表达式")
        sb = SimpleNamespace(execute_script=unavailable, cdp=SimpleNamespace(evaluate=evaluate))
        errors = []
        self.assertEqual(run_js(sb, "return 7;", errors=errors), 7)
        self.assertEqual(len(errors), 1)

    def test_js_both_paths_fail_without_raising(self):
        run_js = self.require("js")
        def unavailable(script):
            raise RuntimeError("unavailable")
        sb = SimpleNamespace(execute_script=unavailable, cdp=SimpleNamespace(evaluate=unavailable))
        errors = []
        self.assertEqual(run_js(sb, "return 7;", "fallback", errors), "fallback")
        self.assertEqual(len(errors), 2)

    def test_component_can_appear_after_initial_check(self):
        wait = self.require("wait_turnstile_present")
        sb = SimpleNamespace(execute_script=lambda script: self.clock.now >= 3)
        self.assertTrue(wait(sb, timeout=5))
        self.assertGreaterEqual(self.clock.now, 3)
        self.assertLessEqual(self.clock.now, 5)

    def test_absent_component_times_out(self):
        wait = self.require("wait_turnstile_present")
        sb = SimpleNamespace(execute_script=lambda script: False)
        self.assertFalse(wait(sb, timeout=3))
        self.assertLessEqual(self.clock.now, 3)

    def test_unreadable_component_state_does_not_mean_absent(self):
        wait = self.require("wait_turnstile_present")
        def unavailable(script):
            raise RuntimeError("unavailable")
        sb = SimpleNamespace(execute_script=unavailable, cdp=SimpleNamespace(evaluate=unavailable))
        with self.assertRaises(RuntimeError):
            wait(sb, timeout=3)

    def test_failed_os_click_is_not_reported_as_success(self):
        def execute(script):
            if script == app._COORDS_JS:
                return {"cx": 30, "cy": 40}
            return {"sx": 0, "sy": 0, "oh": 800, "ih": 700}
        sb = SimpleNamespace(execute_script=execute)
        with patch.object(app, "_xdotool_click", return_value=False):
            self.assertIs(app._click_turnstile(sb), False)

    def test_login_does_not_submit_when_detector_cannot_execute(self):
        self.require("wait_turnstile_present")
        sb = SimpleNamespace(
            uc_open_with_reconnect=lambda *args, **kwargs: None,
            get_page_source=lambda: '<input name="email">',
            wait_for_element=lambda *args, **kwargs: None,
            find_elements=lambda selector: [],
            save_screenshot=lambda path: None,
            click=lambda selector: self.fail("无法读取验证状态时不能提交登录"),
        )
        with patch.object(app, "js_fill_input"), patch.object(app, "wait_turnstile_present", side_effect=RuntimeError("不可读取")):
            self.assertFalse(app.login(sb))

    def test_token_requires_nonempty_string(self):
        read_token = self.require("turnstile_token")
        for value, expected in ((None, ""), (False, ""), (42, ""), ({"token": "x"}, ""), ("", ""), ("valid-token", "valid-token")):
            with self.subTest(value=value):
                sb = SimpleNamespace(execute_script=lambda script: value)
                self.assertEqual(read_token(sb), expected)

    def test_already_verified_does_not_click(self):
        solve = self.require("solve_turnstile")
        sb = SimpleNamespace(execute_script=lambda script: "valid-token")
        self.assertTrue(solve(sb, timeout=20))
        self.assertEqual(self.clock.now, 0)

    def test_timeout_does_not_count_click_as_verification(self):
        solve = self.require("solve_turnstile")
        sb = SimpleNamespace(execute_script=lambda script: "")
        self.assertFalse(solve(sb, timeout=3, first_wait=8))
        self.assertLessEqual(self.clock.now, 4)

    def test_token_appearing_during_wait_succeeds(self):
        solve = self.require("solve_turnstile")
        sb = SimpleNamespace(execute_script=lambda script: "valid-token" if self.clock.now >= 4 else "")
        self.assertTrue(solve(sb, timeout=10, first_wait=8))
        self.assertLessEqual(self.clock.now, 6)

    def test_native_click_reports_success_without_using_cdp(self):
        click = self.require("click_turnstile")
        sb = SimpleNamespace(execute_script=lambda script: {"cx": 30, "cy": 40})
        with patch.object(app, "_click_turnstile", return_value=True):
            self.assertTrue(click(sb))

    def test_failed_native_click_can_use_cdp_screen_coordinates(self):
        point = {"cx": 30, "cy": 40}
        def execute(script):
            if script == app._COORDS_JS:
                return point
            return {"sx": 100, "sy": 200, "oh": 800, "ih": 720}
        def coordinate_click(x, y):
            if (x, y) != (130, 320):
                raise ValueError("必须使用屏幕坐标")
        sb = SimpleNamespace(execute_script=execute, cdp=SimpleNamespace(gui_click_x_y=coordinate_click))
        with patch.object(app, "_click_turnstile", return_value=False):
            self.assertTrue(app.click_turnstile(sb))

    def test_uc_mode_falls_back_without_cdp(self):
        def execute(script):
            if script == app._COORDS_JS:
                return {"cx": 30, "cy": 40}
            return {"sx": 100, "sy": 200, "oh": 800, "ih": 720}
        def coordinate_click(x, y):
            if (x, y) != (130, 320):
                raise ValueError("必须使用屏幕坐标")
        sb = SimpleNamespace(execute_script=execute, uc_gui_click_x_y=coordinate_click)
        with patch.object(app, "_click_turnstile", return_value=False):
            self.assertTrue(app.click_turnstile(sb))

    def test_uc_mode_can_fall_back_when_coordinates_are_unavailable(self):
        sb = SimpleNamespace(execute_script=lambda script: None, uc_gui_click_captcha=lambda: None)
        with patch.object(app, "_click_turnstile", return_value=False):
            self.assertTrue(app.click_turnstile(sb))

    def test_all_click_paths_fail_returns_false(self):
        click = self.require("click_turnstile")
        def unavailable(*args):
            raise RuntimeError("click unavailable")
        sb = SimpleNamespace(execute_script=lambda script: {"cx": 30, "cy": 40}, cdp=SimpleNamespace(gui_click_x_y=unavailable, click_captcha=unavailable))
        with patch.object(app, "_click_turnstile", return_value=False):
            self.assertFalse(click(sb))

    def test_coordinate_lookup_timeout_does_not_raise(self):
        sb = SimpleNamespace(execute_script=lambda script: None)
        self.assertIs(app._click_turnstile(sb), False)
        self.assertLessEqual(self.clock.now, 10)

    def test_login_uses_delayed_detector_before_submit(self):
        wait = self.require("wait_turnstile_present")
        sb = SimpleNamespace(
            uc_open_with_reconnect=lambda *args, **kwargs: None,
            get_page_source=lambda: '<input name="email">',
            wait_for_element=lambda *args, **kwargs: None,
            find_elements=lambda selector: [],
            execute_script=lambda script: False,
            save_screenshot=lambda path: None,
            click=lambda selector: self.fail("验证失败时不能提交登录"),
        )
        with patch.object(app, "js_fill_input"), patch.object(app, "wait_turnstile_present", return_value=True), patch.object(app, "handle_turnstile", return_value=False):
            self.assertFalse(app.login(sb))

    def test_login_still_submits_when_no_component_exists(self):
        self.require("wait_turnstile_present")
        submitted = []
        sb = SimpleNamespace(
            uc_open_with_reconnect=lambda *args, **kwargs: None,
            get_page_source=lambda: '<input name="email">',
            wait_for_element=lambda *args, **kwargs: None,
            find_elements=lambda selector: [],
            execute_script=lambda script: False,
            save_screenshot=lambda path: None,
            click=lambda selector: submitted.append(selector),
            get_current_url=lambda: "https://betadash.lunes.host/",
            get_title=lambda: "Lunes Host | Account Page",
        )
        with patch.object(app, "js_fill_input"), patch.object(app, "wait_turnstile_present", return_value=False):
            self.assertTrue(app.login(sb))
        self.assertEqual(submitted, ['button[type="submit"]'])


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "设置 RUN_BROWSER_TESTS=1 运行离线 Chrome 测试")
class BrowserScriptTests(unittest.TestCase):
    def test_visible_iframe_is_preferred_over_padded_container(self):
        from urllib.parse import quote
        fixture = '''<!doctype html><body style="margin:0">
            <div class="cf-turnstile" style="position:absolute;left:100px;top:100px;padding-left:80px">
                <iframe src="data:text/html,challenges.cloudflare.com" style="display:block;border:0;width:300px;height:66px"></iframe>
            </div></body>'''
        # iframe 为离线 data URL，测试不请求验证服务。
        with app.SB(uc=True, headless=True) as sb:
            sb.open("data:text/html," + quote(fixture))
            self.assertEqual(app.js(sb, app._COORDS_JS), {"cx": 210, "cy": 133})


if __name__ == "__main__":
    unittest.main()
