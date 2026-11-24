#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import subprocess
import requests
import re
import random
from seleniumbase import SB

# 从环境变量获取账号密码和 TG 配置
EMAIL        = os.environ.get("LUNES_EMAIL") or ""     # 登录邮箱
PASSWORD     = os.environ.get("LUNES_PASSWORD") or ""  # 登录密码
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID") or ""      # chat id,可选
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""    # bot token,可选

LOGIN_URL = "https://betadash.lunes.host/login?next=/"

#  获取出口IP及其时区 (api.ip.sb/geoip, 失败时降级到 ip-api.com)
def get_egress_info(proxy_server: str = ""):
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"}
    for url, pick in (
        ("https://api.ip.sb/geoip", lambda d: (d.get("ip"), d.get("timezone"))),
        ("http://ip-api.com/json/?fields=query,timezone", lambda d: (d.get("query"), d.get("timezone"))),
    ):
        try:
            data = requests.get(url, proxies=proxies, headers=headers, timeout=15).json()
            ip, tz = pick(data)
            if ip:
                return ip, tz
        except Exception as e:
            print(f"⚠️ 获取出口信息失败 ({url}): {e}")
    return None, None

#  Telegram 推送
def send_tg_message(status_icon, status_text, extra_text="", egress=""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 推送。")
        return

    local_time = time.gmtime(time.time() + 8 * 3600)
    current_time_str = time.strftime("%Y-%m-%d %H:%M:%S", local_time)

    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****'

    text = (
        f"🇺🇸 Lunes 保活通知\n\n"
        f"{status_icon} {status_text}\n"
        f"👤 登录账户: {masked_email}\n"
    )
    if egress:
        text += f"🌍 出口IP: {egress}\n"
    text += f"⏱️ 登录时间: {current_time_str}"
    if extra_text:
        text += f"\n\n{extra_text}"

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text}
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print("📩 Telegram 通知发送成功！")
        else:
            print(f"  ⚠️ Telegram 通知发送失败: {r.text}")
    except Exception as e:
        print(f"  ⚠️ Telegram 通知发送异常: {e}")

#  WebDriver / CDP 兼容执行，失败时保留诊断信息
# script 使用函数体语法，由 CDP 回退路径包成 IIFE。
def js(sb, script, default=None, errors=None):
    try:
        result = sb.execute_script(script)
        return default if result is None else result
    except Exception as e:
        if errors is not None:
            errors.append(f"execute_script: {type(e).__name__}: {e}")
        try:
            result = sb.cdp.evaluate("(function(){ " + script + " })()")
            return default if result is None else result
        except Exception as e2:
            if errors is not None:
                errors.append(f"cdp.evaluate: {type(e2).__name__}: {e2}")
            return default

#  验证响应优先从官方接口读取，再回退到表单隐藏域
_TOKEN_JS = """
try {
    if (window.turnstile && window.turnstile.getResponse) {
        var token = window.turnstile.getResponse();
        if (token) return token;
    }
} catch (e) {}
var input = document.querySelector('input[name="cf-turnstile-response"]');
return input ? (input.value || '') : '';
"""

_EXISTS_JS = """
return document.querySelector(
    'input[name="cf-turnstile-response"], iframe[src*="challenges.cloudflare.com"], .cf-turnstile'
) !== null;
"""

_COORDS_JS = """
function findWidget(root, selector) {
    var els = root.querySelectorAll(selector);
    for (var i = 0; i < els.length; i++) {
        var r = els[i].getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return els[i];
    }
    var all = root.querySelectorAll('*');
    for (var j = 0; j < all.length; j++) {
        if (all[j].shadowRoot) {
            var hit = findWidget(all[j].shadowRoot, selector);
            if (hit) return hit;
        }
    }
    return null;
}
function boxFromTokenInput() {
    var input = document.querySelector('input[name="cf-turnstile-response"]');
    var parent = input ? input.parentElement : null;
    for (var k = 0; k < 6 && parent; k++) {
        var r = parent.getBoundingClientRect();
        if (r.width > 100 && r.width < 600 && r.height > 30 && r.height < 140) return parent;
        parent = parent.parentElement;
    }
    return null;
}
// 优先查找实际 iframe，包括开放的 Shadow DOM，避免点击外层容器的留白。
var hit = findWidget(document, 'iframe[src*="challenges.cloudflare.com"]') ||
    findWidget(document, '.cf-turnstile, [id*="cf-chl"]') || boxFromTokenInput();
if (!hit) return null;
var r = hit.getBoundingClientRect();
return {cx: Math.round(r.x + 30), cy: Math.round(r.y + r.height / 2)};
"""

_WININFO_JS = """
return {
    sx: window.screenX || 0,
    sy: window.screenY || 0,
    oh: window.outerHeight,
    ih: window.innerHeight
};
"""

# 验证组件异步渲染，不能将首次未找到视为无需验证。
def wait_turnstile_present(sb, timeout=12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        errors = []
        present = js(sb, _EXISTS_JS, None, errors)
        if present is None:
            diag_turnstile_dom(sb, errors)
            raise RuntimeError("无法读取 Turnstile 状态，不能确定是否需要验证")
        if present:
            return True
        time.sleep(1)
    return False


def turnstile_token(sb) -> str:
    token = js(sb, _TOKEN_JS, "")
    return token if isinstance(token, str) else ""


def diag_turnstile_dom(sb, errors=None):
    snap = js(sb, """
        return {
            readyState: document.readyState,
            turnstileApi: typeof window.turnstile,
            tokenInputPresent: !!document.querySelector('input[name="cf-turnstile-response"]'),
            frames: Array.from(document.querySelectorAll('iframe')).map(function(frame) {
                var r = frame.getBoundingClientRect();
                return {width: Math.round(r.width), height: Math.round(r.height)};
            })
        };
    """, "未能读取 DOM")
    print(f"   [diag] Turnstile 现场: {snap}")
    for error in errors or []:
        print(f"   [diag][exec-error] {str(error)[:200]}")

def js_fill_input(sb, selector: str, text: str):
    safe_text = text.replace('\\', '\\\\').replace('"', '\\"')
    sb.execute_script(f"""
    (function(){{
        var el = document.querySelector('{selector}');
        if (!el) return;
        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
        if (nativeInputValueSetter) {{
            nativeInputValueSetter.call(el, "{safe_text}");
        }} else {{
            el.value = "{safe_text}";
        }}
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }})()
    """)

def _activate_window():
    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls], capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]], timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"], timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _xdotool_click(x: int, y: int):
    _activate_window()
    try:
        subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)], timeout=3, stderr=subprocess.DEVNULL, check=True)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null") == 0

def _click_turnstile(sb, locate_timeout=10) -> bool:
    coords = None
    errors = []
    deadline = time.time() + locate_timeout
    while time.time() < deadline:
        coords = js(sb, _COORDS_JS, None, errors)
        if coords:
            break
        time.sleep(1)
    if not coords:
        print("⚠️ 无法定位 Turnstile 坐标，打印现场诊断:")
        diag_turnstile_dom(sb, errors)
        return False
    try:
        wi = js(sb, _WININFO_JS)
        if not wi:
            print("⚠️ 无法读取浏览器窗口坐标，放弃底层点击")
            return False
        bar = wi["oh"] - wi["ih"]
        ax = int(coords["cx"] + wi["sx"])
        ay = int(coords["cy"] + wi["sy"] + bar)
        print(f"🖱️ 底层点击 Turnstile ({ax}, {ay})")
        if not _xdotool_click(ax, ay):
            print("⚠️ 底层点击未成功，尝试其他点击方式")
            return False
        time.sleep(3)
        return True
    except Exception as e:
        print(f"⚠️ 底层点击失败: {e}")
        return False


def click_turnstile(sb) -> bool:
    if _click_turnstile(sb):
        return True
    cdp = getattr(sb, "cdp", None)
    coords = js(sb, _COORDS_JS)
    if coords:
        wi = js(sb, _WININFO_JS)
        if wi:
            try:
                # CDP 和 UC 的 GUI 点击接口均使用屏幕坐标，而非视口坐标。
                x = int(coords["cx"] + wi["sx"])
                y = int(coords["cy"] + wi["sy"] + wi["oh"] - wi["ih"])
                for mode, click in (
                    ("CDP", getattr(cdp, "gui_click_x_y", None)),
                    ("UC", getattr(sb, "uc_gui_click_x_y", None)),
                ):
                    if not callable(click):
                        continue
                    try:
                        print(f"🖱️ {mode} 坐标点击 Turnstile ({x}, {y})")
                        click(x, y)
                        return True
                    except Exception as e:
                        print(f"⚠️ {mode} 坐标点击失败: {e}")
            except Exception as e:
                print(f"⚠️ 无法转换浏览器坐标: {e}")
    for mode, click in (
        ("CDP", getattr(cdp, "click_captcha", None)),
        ("UC", getattr(sb, "uc_gui_click_captcha", None)),
    ):
        if not callable(click):
            continue
        try:
            print(f"🖱️ 兜底调用 {mode} 点击...")
            click()
            return True
        except Exception as e:
            print(f"⚠️ {mode} 验证点击失败: {e}")
    print("⚠️ 当前浏览器没有可用的验证点击方式")
    return False


def solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8) -> bool:
    if turnstile_token(sb):
        return True
    deadline = time.time() + timeout
    next_click = time.time() + first_wait
    clicks = 0
    while time.time() < deadline:
        if turnstile_token(sb):
            print(f"✅ {tag} 已通过（已获取响应）")
            return True
        if time.time() >= next_click:
            clicks += 1
            print(f"🖱️ 第 {clicks} 次点击 Turnstile 验证框...")
            click_turnstile(sb)
            next_click = time.time() + 10 + random.uniform(0, 3)
        time.sleep(2)
    ok = bool(turnstile_token(sb))
    print(f"{'✅' if ok else '❌'} {tag} {'已通过' if ok else '未通过'}")
    if not ok:
        diag_turnstile_dom(sb)
    return ok


def handle_turnstile(sb) -> bool:
    print("🔍 处理 Cloudflare Turnstile 验证...")
    return solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8)

def _login_succeeded(cur_url: str, page_title: str, page_src: str) -> bool:
    """判断当前页面是否已经是登录后的账户页。

    登录成功后会跳转到 https://betadash.lunes.host/ ，标题为
    "Lunes Host | Account Page"。跳转过程中标题可能还是空的，
    因此再用页面里的 server-card 作为兜底特征。
    """
    url = (cur_url or "").split('?')[0].lower()
    if "/login" in url:
        return False
    title = (page_title or "").lower()
    src = (page_src or "").lower()
    return "account page" in title or "server-card" in src

def login(sb) -> bool:
    print(f"🌐 打开登录页面: {LOGIN_URL}")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5)
    time.sleep(6)

    print("⏳ 等待 Cloudflare 验证通过...")
    cf_passed = False
    for i in range(30):
        page_src = sb.get_page_source() or ""
        if 'input[name="email"]' in page_src.lower() or 'name="email"' in page_src.lower():
            cf_passed = True
            print(f"✅ Cloudflare 验证已通过（{i+1}s）")
            break
        time.sleep(1)
    if not cf_passed:
        print("⚠️ Cloudflare 验证可能未通过，继续尝试...")

    try:
        sb.wait_for_element('input[name="email"]', timeout=15)
    except Exception:
        try:
            sb.wait_for_element('input[name="Email"]', timeout=5)
        except Exception:
            print("❌ 页面未加载出登录表单")
            cur_url = sb.get_current_url()
            page_title = sb.get_title() or ""
            print(f"  当前 URL: {cur_url}")
            print(f"  当前标题: {page_title}")
            sb.save_screenshot("login_load_fail.png")
            return False

    print("🍪 关闭可能的 Cookie 弹窗...")
    try:
        for btn in sb.find_elements("button"):
            if "Accept" in (btn.text or ""):
                btn.click()
                time.sleep(0.5)
                break
    except Exception:
        pass

    print(f"📧 填写邮箱...")
    js_fill_input(sb, 'input[name="email"]', EMAIL)
    time.sleep(0.3)
    
    print("🔑 填写密码...")
    js_fill_input(sb, 'input[name="password"]', PASSWORD)
    time.sleep(1)

    try:
        turnstile_present = wait_turnstile_present(sb)
    except RuntimeError as e:
        print(f"❌ 验证组件状态读取失败: {e}")
        sb.save_screenshot("login_turnstile_fail.png")
        return False
    if turnstile_present:
        if not handle_turnstile(sb):
            print("❌ 登录界面的 Turnstile 验证失败")
            sb.save_screenshot("login_turnstile_fail.png")
            return False
    else:
        print("ℹ️ 未检测到 Turnstile")

    print("🖱️ 点击登录按钮提交登录...")
    sb.click('button[type="submit"]')

    print("⏳ 等待登录跳转...")
    for _ in range(20):
        time.sleep(1)
        if _login_succeeded(sb.get_current_url(), sb.get_title(), sb.get_page_source()):
            break

    cur_url = sb.get_current_url()
    page_title = sb.get_title() or ""
    if _login_succeeded(cur_url, page_title, sb.get_page_source()):
        print(f"✅ 登录成功！(URL: {cur_url}, Title: {page_title})")
        return True

    print(f"❌ 登录失败，页面未跳转到账户页。(URL: {cur_url}, Title: {page_title})")
    sb.save_screenshot("login_failed.png")
    return False

# 点击被拦截时诊断: 元素中心点实际命中的是哪个节点
def _probe_click_target(sb, element):
    try:
        data = sb.execute_script("""
            var el = arguments[0];
            var r = el.getBoundingClientRect();
            var cx = Math.round(r.left + r.width / 2);
            var cy = Math.round(r.top + r.height / 2);
            var top = document.elementFromPoint(cx, cy);
            function desc(n) {
                if (!n) return null;
                var cls = (typeof n.className === 'string') ? n.className : '';
                return n.tagName + (n.id ? '#' + n.id : '') +
                    (cls ? '.' + cls.trim().split(/\\s+/).join('.') : '');
            }
            return JSON.stringify({
                rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                center: [cx, cy],
                scrollY: Math.round(window.scrollY),
                hit: desc(top),
                hitText: top ? (top.innerText || '').slice(0, 120) : null
            });
        """, element)
        print(f"   [probe] 卡片位置/命中节点: {data}")
    except Exception as e:
        print(f"   [probe] 诊断失败: {type(e).__name__}: {e}")

# 访问服务器页面
def visit_server(sb) -> (bool, dict):
    print("🔍 正在查找服务器卡片...")
    try:
        sb.wait_for_element('a.server-card', timeout=15)
    except Exception:
        print("❌ 未找到服务器卡片（可能没有服务器）")
        return False, {"error": "未找到服务器卡片，可能账户无服务器"}

    cards = sb.find_elements('a.server-card')
    if not cards:
        return False, {"error": "未找到服务器卡片"}

    card = cards[0]
    href = card.get_attribute('href')
    if not href:
        return False, {"error": "卡片缺少 href 属性"}

    match = re.search(r'/servers/(\d+)', href)
    if not match:
        return False, {"error": f"无法从 href 解析服务器 ID: {href}"}
    server_id = match.group(1)

    print(f"🖱️ 点击服务器卡片 (ID: {server_id})")
    try:
        card.click()
    except Exception as e:
        # 元素存在但中心点被遮挡时, 记录覆盖元素, 便于定位是浮层还是渲染中间态
        print(f"⚠️ 直接点击被拦截: {type(e).__name__}: {str(e)[:150]}")
        _probe_click_target(sb, card)
        try:
            sb.save_screenshot("server_card_click_fail.png")
            print("📄 已保存截图: server_card_click_fail.png")
        except Exception as se:
            print(f"⚠️ 保存截图失败: {se}")
        print("🖱️ 改用 JS 点击兜底...")
        try:
            sb.execute_script("arguments[0].click();", card)
        except Exception as e2:
            return False, {"server_id": server_id, "error": f"点击服务器卡片失败: {type(e2).__name__}: {e2}"}
    time.sleep(3)

    expected_url_prefix = f"https://betadash.lunes.host/servers/{server_id}"
    for _ in range(10):
        cur_url = sb.get_current_url().split('?')[0]
        if cur_url == expected_url_prefix:
            break
        time.sleep(1)
    else:
        return False, {"server_id": server_id, "error": f"跳转后 URL 不匹配，当前: {sb.get_current_url()}"}

    page_title = sb.get_title() or ""
    server_name = ""
    if "Server " in page_title:
        server_name = page_title.split("Server ", 1)[-1].strip()
    else:
        server_name = f"ID {server_id}"

    print(f"✅ 成功访问服务器: {server_name} (ID: {server_id})")
    return True, {"server_id": server_id, "server_name": server_name}

def main():
    print("#" * 25)
    print("   Lunes 自动登录续期")
    print("#" * 25)
    
    is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_server = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    sb_kwargs = {"uc": True, "headless": False}

    if is_proxy:
        print(f"🔗 挂载代理: {proxy_server}")
        sb_kwargs["proxy"] = proxy_server
    else:
        print("🌐 未使用代理，直连访问")

    # 出口IP对应时区设置给浏览器, 避免IP与时区不一致
    ip, tz = get_egress_info(proxy_server if is_proxy else "")
    egress_str = f"{ip} ({tz})" if (ip and tz) else (ip or "未知")
    if tz:
        print(f"📍 出口IP: {ip}, 时区: {tz}")
        # Chrome 进程读取 TZ 环境变量确定时区(CDP set_timezone_override 在此版本对页面不生效)
        os.environ["TZ"] = tz
    else:
        print(f"📍 出口IP: {egress_str}, 时区: 未获取到(浏览器使用系统默认)")

    with SB(**sb_kwargs) as sb:
        print("✅ 浏览器已启动")

        if login(sb):
            success, info = visit_server(sb)
            if success:
                extra = f"服务器: {info['server_name']}\nID: {info['server_id']}"
                send_tg_message("✅", "续期成功", extra, egress=egress_str)
            else:
                error_msg = info.get('error', '未知错误')
                print(f"❌ 访问服务器失败: {error_msg}")
                extra = f"错误: {error_msg}"
                if 'server_id' in info:
                    extra += f"\n服务器ID: {info['server_id']}"
                send_tg_message("❌", "续期失败", extra, egress=egress_str)
        else:
            print("\n❌ 登录失败，终止后续续期操作。")
            send_tg_message("❌", "登录失败", "", egress=egress_str)

if __name__ == "__main__":
    main()
