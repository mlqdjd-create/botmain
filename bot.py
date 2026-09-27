"""
بوت تيليجرام — Google Cloud → Cloud Run Service
نظام طابور لمعالجة الروابط بالتتابع
"""

import asyncio
import os
import re
import shutil
import urllib.parse
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from itertools import count

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from playwright.async_api import async_playwright

# ============================================================
# تشغيل محلي على جهاز المستخدم: يبقى التوكن داخل الملف كما طلبت.
BOT_TOKEN = "8949437133:AAGLhrLaZ3oPNrsCgYgOlWUM8b3yqzQn0rc"
TARGET_CHAT_ID = -2742181993

USER_DATA_DIR = Path("data/chrome_profile")
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

VIEWPORT = {"width": 1280, "height": 720}

CR_IMAGE = "docker.io/mohammedaljbori/v2ray:v1"
CR_SERVICE_NAME = "v2ray"
CR_PORT = "8080"
CR_MIN = "1"
CR_MAX = "8"

XRAY_UUID = "D2CB8181-233C-4D18-9972-8A1B04DB0044"
XRAY_SNI = "youtube.com"

# ✅ المسار الجديد الذي سيُرسل في تكوين VLESS
XRAY_PATH = "/Telegram_@oy_u4"

url_sessions = {}

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
"""

# ============================================================
def is_valid_google_sso_url(url: str) -> bool:
    pattern = (
        r"^https://www\.skills\.google/google_sso"
        r".*\b(fallback|AddSession|console\.cloud\.google\.com)\b"
    )
    return bool(re.search(pattern, url))


class GoogleNavigationError(RuntimeError):
    """فشل الوصول إلى Google بعد إعادة المحاولات ضمن الجلسة نفسها."""


async def goto_google_with_retry(page, url: str, label: str, attempts: int = 3):
    """يفتح صفحة Google دون اعتبار تأخر تحميل DOM فشلاً في التنقل.

    Google SSO يمر بعدة تحويلات بين skills.google وaccounts.google، وقد يظل
    DOM غير مكتمل مع أن التحويل وصل فعلاً. لذلك ننتظر بداية التحميل أولاً، ثم
    نعيد المحاولة عند أخطاء الشبكة، بدلاً من إنهاء جلسة المستخدم فوراً.
    """
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="commit", timeout=75_000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception:
                # بعض صفحات Google تتابع التحويل بعد commit؛ تتابع مرحلة الكشف
                # لاحقاً بدلاً من فشل الجلسة بسبب انتظار DOM فقط.
                pass
            print(f"[NAVIGATION] {label} نجح في المحاولة {attempt}")
            return
        except Exception as exc:
            last_error = exc
            print(
                f"[NAVIGATION] {label} فشل في المحاولة {attempt}/{attempts}: "
                f"{type(exc).__name__}: {exc}"
            )
            if attempt == attempts:
                break

            # إلغاء أي تحويل عالق قبل إعادة فتح نفس الرابط.
            try:
                await page.goto("about:blank", wait_until="commit", timeout=5_000)
            except Exception:
                pass
            await asyncio.sleep(attempt * 3)

    raise GoogleNavigationError(
        f"تعذر فتح {label} بعد {attempts} محاولات: {last_error}"
    ) from last_error


def extract_project_id(url: str) -> str:
    match = re.search(r'(qwiklabs-gcp-[\w\-]+)', url)
    return match.group(1) if match else ""


async def read_page_text(page):
    txt = ""
    try:
        for frame in page.frames:
            try:
                t = await asyncio.wait_for(
                    frame.evaluate("() => document.body.innerText || ''"),
                    timeout=8
                )
                txt += "\n" + t
            except Exception:
                continue
    except Exception:
        pass
    return txt


def extract_run_url(text: str) -> str:
    patterns = [
        r'https://[\w\-]+-\d+\.(?:[a-z]+-)?[a-z]+\d?\.run\.app[\w\-/]*',
        r'https://[\w\-]+\.(?:[a-z]+-)?[a-z]+\d?\.run\.app[\w\-/]*',
        r'https://[\w\-]+\.run\.app[\w\-/]*',
        r'[\w\-]+-\d+\.(?:[a-z]+-)?[a-z]+\d?\.run\.app',
        r'[\w\-]+\.(?:[a-z]+-)?[a-z]+\d?\.run\.app',
        r'[\w\-]+\.run\.app',
    ]
    for pat in patterns:
        matches = re.findall(pat, text)
        for m in matches:
            if "service-name" in m.lower():
                continue
            if not m.startswith("http"):
                m = "https://" + m
            return m
    return ""


def build_vless(domain: str) -> str:
    domain = domain.replace("https://", "").replace("http://", "").rstrip("/")
    # ✅ ترميز المسار ليصبح صالحاً داخل رابط VLESS
    encoded_path = urllib.parse.quote(XRAY_PATH, safe="")
    return (
        f"vless://{XRAY_UUID}@{domain}:443"
        f"?encryption=none&security=tls&sni={XRAY_SNI}"
        f"&fp=chrome&type=ws&host={domain}"
        f"&path={encoded_path}#GCP-Xray"
    )


async def publish_result(final_url: str, vless: str):
    if not TARGET_CHAT_ID:
        return
    try:
        await bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=(
                f"🔗 <code>{final_url}</code>\n\n"
                f"📋 <b>VLESS:</b>\n<code>{vless}</code>"
            ),
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[PUBLISH-ERR] {e}")


async def take_screenshot_and_send(page, bot_instance, user_id, caption: str):
    path = f"screen_{user_id}.png"
    try:
        await asyncio.wait_for(
            page.screenshot(path=path, full_page=False),
            timeout=15
        )
        await bot_instance.send_photo(
            chat_id=user_id,
            photo=FSInputFile(path),
            caption=caption
        )
    except Exception as e:
        print(f"[SCREENSHOT-ERR] {e}")
    finally:
        if os.path.exists(path):
            os.remove(path)


# ============================================================
# انتظار رابط run.app — 5 محاولات تشمل Shadow DOM
# ============================================================
async def wait_for_run_url(page, timeout=120) -> str:
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(2)

        # محاولة 1: locator مباشر
        try:
            link_locator = page.locator('a[href*="run.app"]')
            count = await link_locator.count()
            if count > 0:
                for i in range(count):
                    href = await link_locator.nth(i).get_attribute("href")
                    if href and "run.app" in href and "service-name" not in href.lower():
                        return href
        except Exception:
            pass

        # محاولة 2: من URL الصفحة
        try:
            url_now = page.url
            if "run.app" in url_now:
                m = re.search(
                    r'(https://[\w\-]+\.(?:[a-z]+-)?[a-z]+\d?\.run\.app[\w\-/]*)',
                    url_now
                )
                if m and "service-name" not in m.group(1).lower():
                    return m.group(1)
        except Exception:
            pass

        # محاولة 3: JavaScript يخترق Shadow DOM
        try:
            result = await asyncio.wait_for(
                page.evaluate("""() => {
                    function findRunAppLinks(root) {
                        const links = [];
                        root.querySelectorAll('a[href*="run.app"]').forEach(a => {
                            const h = a.href || a.getAttribute('href') || '';
                            if (h.includes('run.app') && !h.includes('service-name'))
                                links.push(h);
                        });
                        root.querySelectorAll('*').forEach(el => {
                            if (el.shadowRoot) {
                                const inner = findRunAppLinks(el.shadowRoot);
                                links.push(...inner);
                            }
                        });
                        return links;
                    }
                    return findRunAppLinks(document);
                }"""),
                timeout=10
            )
            if result and len(result) > 0:
                return result[0]
        except Exception:
            pass

        # محاولة 4: كل frame
        try:
            for frame in page.frames:
                try:
                    links = await asyncio.wait_for(
                        frame.evaluate("""() => {
                            const out = [];
                            document.querySelectorAll('a').forEach(a => {
                                const h = a.href || '';
                                if (h.includes('run.app') && !h.includes('service-name'))
                                    out.push(h);
                            });
                            return out;
                        }"""),
                        timeout=5
                    )
                    if links:
                        return links[0]
                except Exception:
                    continue
        except Exception:
            pass

        # محاولة 5: من نص الصفحة
        try:
            txt = await asyncio.wait_for(read_page_text(page), timeout=8)
            found = extract_run_url(txt)
            if found:
                return found
        except Exception:
            pass

    return ""


# ============================================================
# أدوات النقر والتعبئة
# ============================================================
async def find_next_button(page):
    texts = ["Next", "التالي", "Sign in", "Verify", "تحقق", "تسجيل الدخول"]
    for frame in page.frames:
        for t in texts:
            try:
                btn = await frame.query_selector(f'button:has-text("{t}")')
                if btn and await btn.is_visible():
                    return frame, btn
            except Exception:
                continue
        for sel in ['#identifierNext', '#passwordNext']:
            try:
                btn = await frame.query_selector(sel)
                if btn and await btn.is_visible():
                    return frame, btn
            except Exception:
                continue
    return None, None


async def click_agree(page) -> bool:
    texts = [
        "Agree and continue", "Agree", "I understand", "Accept",
        "Continue", "موافق ومتابعة", "أوافق", "متابعة", "Close",
    ]
    for frame in page.frames:
        for t in texts:
            for sel in [
                f'button:has-text("{t}")',
                f'[role="button"]:has-text("{t}")',
            ]:
                try:
                    btn = await frame.query_selector(sel)
                    if btn and await btn.is_visible():
                        try:
                            await btn.scroll_into_view_if_needed(timeout=3000)
                            await btn.click(timeout=3000)
                            return True
                        except Exception:
                            try:
                                await frame.evaluate("(b) => b.click()", btn)
                                return True
                            except Exception:
                                continue
                except Exception:
                    continue
    return False


async def click_checkbox(page) -> bool:
    for frame in page.frames:
        try:
            cbs = await frame.query_selector_all(
                'input[type="checkbox"], [role="checkbox"]'
            )
            for cb in cbs:
                try:
                    if await cb.is_visible():
                        try:
                            checked = await cb.is_checked()
                        except Exception:
                            checked = False
                        if not checked:
                            try:
                                await cb.click(timeout=3000)
                                return True
                            except Exception:
                                try:
                                    await frame.evaluate("(c) => c.click()", cb)
                                    return True
                                except Exception:
                                    continue
                except Exception:
                    continue
        except Exception:
            continue
    return False


async def click_text(page, texts) -> bool:
    for frame in page.frames:
        for t in texts:
            for sel in [
                f'button:has-text("{t}")',
                f'[role="button"]:has-text("{t}")',
                f'a:has-text("{t}")',
            ]:
                try:
                    el = await frame.query_selector(sel)
                    if el and await el.is_visible():
                        try:
                            await el.scroll_into_view_if_needed(timeout=3000)
                            await el.click(timeout=3000)
                            return True
                        except Exception:
                            try:
                                await frame.evaluate("(e) => e.click()", el)
                                return True
                            except Exception:
                                continue
                except Exception:
                    continue
    return False


async def click_create_service_safe(page) -> bool:
    try:
        btn = page.get_by_role("button", name="Create service")
        if await btn.is_visible(timeout=2000):
            await btn.click(force=True)
            return True
    except Exception:
        pass

    try:
        btn = page.get_by_role("button", name="Create")
        if await btn.is_visible(timeout=2000):
            await btn.click(force=True)
            return True
    except Exception:
        pass

    for frame in page.frames:
        try:
            btns = await frame.query_selector_all('button, [role="button"]')
            for btn in btns:
                try:
                    if not await btn.is_visible():
                        continue
                    txt = (await btn.inner_text() or "").strip().lower()
                    if txt not in ("create service", "create"):
                        continue
                    parent_txt = await frame.evaluate("""(el) => {
                        let p = el;
                        for (let i = 0; i < 5; i++) {
                            if (!p.parentElement) break;
                            p = p.parentElement;
                        }
                        return (p.innerText || '').toLowerCase();
                    }""", btn)
                    if "job" in parent_txt:
                        continue
                    if "service" in parent_txt or txt == "create service":
                        await btn.click(force=True, timeout=5000)
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    return await click_text(page, ["Create service", "Create Service"])


async def fill_field(page, selectors, value) -> bool:
    for frame in page.frames:
        for sel in selectors:
            try:
                el = await frame.wait_for_selector(sel, state="visible", timeout=2000)
                if el:
                    await el.click()
                    await asyncio.sleep(0.2)
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Delete")
                    await page.keyboard.type(value, delay=25)
                    return True
            except Exception:
                continue
    return False


async def fill_by_shadow_dom(page, label_text: str, value: str) -> bool:
    try:
        js = f"""() => {{
            const search = '{label_text}'.toLowerCase();
            function searchDeep(root) {{
                const labels = root.querySelectorAll('label, mat-label, [class*="label"]');
                for (const label of labels) {{
                    const txt = (label.innerText || '').toLowerCase().trim();
                    if (txt === search || txt.includes(search)) {{
                        let parent = label.parentElement;
                        for (let i = 0; i < 5; i++) {{
                            if (!parent) break;
                            const inp = parent.querySelector('input');
                            if (inp) return inp;
                            parent = parent.parentElement;
                        }}
                    }}
                }}
                const all = root.querySelectorAll('*');
                for (const el of all) {{
                    if (el.shadowRoot) {{
                        const found = searchDeep(el.shadowRoot);
                        if (found) return found;
                    }}
                }}
                return null;
            }}
            const inp = searchDeep(document);
            if (!inp) return false;
            inp.focus();
            inp.value = '';
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(inp, '{value}');
            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
            inp.dispatchEvent(new Event('blur', {{bubbles: true}}));
            return true;
        }}"""
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def fill_by_placeholder_js(page, placeholder_substring: str, value: str) -> bool:
    try:
        js = f"""() => {{
            const search = '{placeholder_substring}'.toLowerCase();
            function findInput(root) {{
                const inputs = root.querySelectorAll('input[type="text"], input:not([type])');
                for (const inp of inputs) {{
                    const ph = (inp.getAttribute('placeholder') || '').toLowerCase();
                    const aria = (inp.getAttribute('aria-label') || '').toLowerCase();
                    if ((ph + ' ' + aria).includes(search)) return inp;
                }}
                const all = root.querySelectorAll('*');
                for (const el of all) {{
                    if (el.shadowRoot) {{
                        const found = findInput(el.shadowRoot);
                        if (found) return found;
                    }}
                }}
                return null;
            }}
            const inp = findInput(document);
            if (!inp) return false;
            inp.focus();
            inp.value = '';
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(inp, '{value}');
            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }}"""
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def wait_for_input(page, selectors, timeout=15) -> bool:
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        for frame in page.frames:
            for sel in selectors:
                try:
                    el = await frame.query_selector(sel)
                    if el and await el.is_visible():
                        return True
                except Exception:
                    continue
        await asyncio.sleep(0.5)
    return False


async def select_radio(page, label) -> bool:
    try:
        return bool(await page.evaluate(f"""() => {{
            const radios = document.querySelectorAll('input[type="radio"], [role="radio"]');
            for (const r of radios) {{
                let p = r.parentElement;
                for (let i = 0; i < 6 && p; i++) {{
                    if ((p.innerText || '').toLowerCase().includes('{label.lower()}')) {{
                        r.click(); return true;
                    }}
                    p = p.parentElement;
                }}
            }}
            return false;
        }}"""))
    except Exception:
        return False


async def select_checkbox_label(page, label) -> bool:
    try:
        return bool(await page.evaluate(f"""() => {{
            const cbs = document.querySelectorAll('input[type="checkbox"], [role="checkbox"]');
            for (const cb of cbs) {{
                let p = cb.parentElement;
                for (let i = 0; i < 6 && p; i++) {{
                    if ((p.innerText || '').toLowerCase().includes('{label.lower()}')) {{
                        if (!cb.checked) cb.click();
                        return true;
                    }}
                    p = p.parentElement;
                }}
            }}
            return false;
        }}"""))
    except Exception:
        return False


async def find_input(page, selectors):
    for frame in page.frames:
        for sel in selectors:
            try:
                el = await frame.query_selector(sel)
                if el and await el.is_visible():
                    return frame, el
            except Exception:
                continue
    return None, None


# ============================================================
# نافذة الشروط
# ============================================================
async def has_cloud_consent(page) -> bool:
    try:
        for frame in page.frames:
            try:
                body = await asyncio.wait_for(
                    frame.evaluate("() => document.body.innerText || ''"),
                    timeout=5
                )
                low = body.lower()
                if ("welcome student" in low and
                        "i agree to the google cloud platform terms of service" in low):
                    return True
                if "welcome student" in low and "agree and continue" in low:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def handle_cloud_consent(page) -> bool:
    if not await has_cloud_consent(page):
        return False
    await asyncio.sleep(1)
    for _ in range(3):
        try:
            await page.mouse.wheel(0, 600)
        except Exception:
            pass
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.5)
    await click_checkbox(page)
    await asyncio.sleep(1)
    for _ in range(3):
        if await click_agree(page):
            await asyncio.sleep(3)
            return True
        await asyncio.sleep(1)
    return False


# ============================================================
# تفعيل Cloud Run API
# ============================================================
async def enable_cloud_run_api(page, project_id: str, authuser: str) -> bool:
    api_url = (
        f"https://console.cloud.google.com/apis/library/run.googleapis.com"
        f"?project={project_id}&authuser={authuser}"
    )
    try:
        await goto_google_with_retry(page, api_url, "صفحة Cloud Run API", attempts=2)
    except GoogleNavigationError as exc:
        print(f"[NAVIGATION] تعذر فتح صفحة API: {exc}")
        return False

    await asyncio.sleep(5)
    await handle_cloud_consent(page)

    for attempt in range(3):
        for frame in page.frames:
            try:
                manage = await frame.query_selector('button:has-text("Manage")')
                if manage and await manage.is_visible():
                    return True
                disable_lnk = await frame.query_selector('a:has-text("Disable API")')
                if disable_lnk and await disable_lnk.is_visible():
                    return True
                enable = await frame.query_selector('button:has-text("Enable")')
                if enable and await enable.is_visible():
                    await enable.click()
                    await asyncio.sleep(10)
                    return True
            except Exception:
                continue
        await asyncio.sleep(3)
    return False


# ============================================================
# كشف المرحلة
# ============================================================
async def detect_stage(page) -> str:
    try:
        for frame in page.frames:
            try:
                pwd = await frame.query_selector('input[type="password"]')
                if pwd and await pwd.is_visible():
                    return "password"
            except Exception:
                continue
    except Exception:
        pass

    try:
        if await asyncio.wait_for(has_cloud_consent(page), timeout=5):
            return "cr_consent"
    except Exception:
        pass

    txt = await read_page_text(page)
    low = txt.lower()

    if any(k in low for k in [
        "i agree to the google cloud platform terms of service",
        "agree and continue",
    ]):
        return "consent"

    try:
        url = page.url
        if ("console.cloud.google.com" in url and
                "accounts.google.com" not in url and
                "AddSession" not in url and
                "signin" not in url):
            if "/run" in url:
                return "cloudrun"
            return "dashboard"
    except Exception:
        pass

    if any(k in low for k in ["type the text you hear", "enter the characters you see"]):
        return "captcha"
    if any(k in low for k in ["2-step verification", "verification code"]):
        return "2fa"
    if "welcome to your new account" in low:
        return "welcome"

    try:
        for frame in page.frames:
            try:
                email = await frame.query_selector(
                    'input[type="email"], input[name="identifier"]'
                )
                if email and await email.is_visible():
                    try:
                        val = await email.input_value()
                    except Exception:
                        val = ""
                    if val and "@" in val:
                        return "email_filled"
                    return "email"
            except Exception:
                continue
    except Exception:
        pass

    return "unknown"


# ============================================================
# اختيار المشروع
# ============================================================
async def is_project_selected(page) -> bool:
    try:
        for frame in page.frames:
            try:
                sel_btn = await frame.query_selector('button:has-text("Select a project")')
                if sel_btn and await sel_btn.is_visible():
                    return False
            except Exception:
                continue
        txt = await read_page_text(page)
        if re.search(r'qwiklabs-gcp-[\w\-]+', txt):
            return True
    except Exception:
        pass
    return False


async def pick_project(page) -> str:
    clicked = False
    for frame in page.frames:
        try:
            btn = await frame.query_selector('button:has-text("Select a project")')
            if btn and await btn.is_visible():
                await btn.click()
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        return ""

    await asyncio.sleep(4)
    picked = False
    project_id = ""

    for frame in page.frames:
        try:
            links = await frame.query_selector_all('a')
            candidates = []
            for link in links:
                try:
                    txt = (await link.inner_text()).strip()
                    if txt.startswith("qwiklabs-gcp-"):
                        candidates.append((link, txt))
                except Exception:
                    continue
            if candidates:
                target, txt = candidates[-1]
                project_id = txt
                await target.click()
                picked = True
                break
        except Exception:
            continue

    if not picked:
        try:
            result = await page.evaluate("""() => {
                const links = document.querySelectorAll('a');
                let candidates = [];
                for (const a of links) {
                    const t = (a.innerText || '').trim();
                    if (t.startsWith('qwiklabs-gcp-')) candidates.push(a);
                }
                if (candidates.length === 0) return '';
                const target = candidates[candidates.length - 1];
                target.click();
                return target.innerText.trim();
            }""")
            if result:
                project_id = result
                picked = True
        except Exception:
            pass

    if not picked:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return ""

    await asyncio.sleep(6)
    txt = await read_page_text(page)
    m = re.search(r'qwiklabs-gcp-[\w\-]+', txt)
    if m:
        project_id = m.group(0)
    return project_id


# ============================================================
# Workflow الرئيسي
# ============================================================
async def full_workflow(page, user_id, send_msg, username, sso_url: str = ""):
    tag = f"@{username}"
    stage_stuck_since = {}
    STUCK_LIMIT = 180

    async def log(msg):
        try:
            await send_msg(msg)
        except Exception:
            pass

    async def check_stuck(stage: str):
        now = asyncio.get_event_loop().time()
        if stage not in stage_stuck_since:
            stage_stuck_since[stage] = now
            return False
        elapsed = now - stage_stuck_since[stage]
        if elapsed >= STUCK_LIMIT:
            await log(
                f"[{tag}] ⚠️ <b>تجمد في مرحلة <code>{stage}</code> "
                f"لأكثر من 3 دقائق</b>\nسيتم تخطي دورك."
            )
            await take_screenshot_and_send(
                page, bot, user_id,
                f"[{tag}] 📸 تجمد في: {stage}"
            )
            return True
        return False

    state = {
        "password": False, "captcha": False, "2fa": False,
        "consent": False, "welcome": False, "dashboard": False,
        "email_next_clicked": False, "cr_consent": False,
        "api_enabled": False,
    }
    authuser = "0"
    project_id_from_url = extract_project_id(sso_url)

    try:
        await log(
            f"☁️ <b>𝙂𝘾.𝙍𝙪𝙣</b>\n"
            f"✅ تم استلام الرابط. جاري التنفيذ الآن…\n\n"
            f"[{tag}] • 1) فتح رابط الطالب…"
        )

        start = asyncio.get_event_loop().time()
        max_wait = 60 * 35
        last_stage = ""
        step = 1

        while asyncio.get_event_loop().time() - start < max_wait:
            await asyncio.sleep(3)

            try:
                stage = await asyncio.wait_for(detect_stage(page), timeout=15)
            except Exception:
                await asyncio.sleep(5)
                continue

            if stage != last_stage:
                stage_stuck_since.pop(last_stage, None)
                last_stage = stage
                print(f"[WORKFLOW] → {stage}")
            else:
                if await check_stuck(stage):
                    return "SKIP"

            if stage == "cr_consent":
                try:
                    if await asyncio.wait_for(handle_cloud_consent(page), timeout=30):
                        state["cr_consent"] = True
                        stage_stuck_since.pop("cr_consent", None)
                        await asyncio.sleep(3)
                except Exception:
                    pass
                continue

            if stage == "email_filled" and not state["email_next_clicked"]:
                frame_ref, btn = await find_next_button(page)
                if btn:
                    try:
                        await btn.click(timeout=3000)
                        state["email_next_clicked"] = True
                        step += 1
                        await log(f"[{tag}] • • {step} ✅")
                        await asyncio.sleep(5)
                    except Exception:
                        pass

            if stage == "password":
                if not state["password"]:
                    state["password"] = True
                    await log(
                        f"[{tag}] • • 🔐 مطلوب كلمة السر\n\nأرسل كلمة السر هنا."
                    )
                continue

            if stage == "captcha":
                if not state["captcha"]:
                    state["captcha"] = True
                    await log(
                        f"[{tag}] • • 🤖 مطلوب CAPTCHA\n\nاقرأ الصورة وأرسل الكود."
                    )
                continue

            if stage == "2fa":
                if not state["2fa"]:
                    state["2fa"] = True
                    await log(
                        f"[{tag}] • • 📱 مطلوب كود 2FA\n\nأرسل الكود هنا."
                    )
                continue

            if stage == "consent" and not state["consent"]:
                for _ in range(3):
                    try:
                        await page.mouse.wheel(0, 800)
                    except Exception:
                        pass
                    await asyncio.sleep(0.3)
                await asyncio.sleep(1)
                await click_checkbox(page)
                await asyncio.sleep(1)
                if await click_agree(page):
                    state["consent"] = True
                    await asyncio.sleep(3)

            if stage == "welcome" and not state["welcome"]:
                for _ in range(4):
                    try:
                        await page.mouse.wheel(0, 1000)
                    except Exception:
                        pass
                    await asyncio.sleep(0.3)
                await asyncio.sleep(1)
                if await click_agree(page):
                    state["welcome"] = True
                    await asyncio.sleep(3)

            if stage == "dashboard" and not state["dashboard"]:
                state["dashboard"] = True
                step += 1
                await log(f"[{tag}] • • {step} ✅")

                try:
                    parsed = urllib.parse.urlparse(page.url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    authuser = qs.get("authuser", ["0"])[0]
                except Exception:
                    authuser = "0"

                project_id = project_id_from_url

                if not project_id:
                    project_picked = await is_project_selected(page)
                    if not project_picked:
                        step += 1
                        await log(f"[{tag}] • {step}) اختيار المشروع…")
                        try:
                            project_id = await asyncio.wait_for(
                                pick_project(page), timeout=60
                            )
                        except Exception:
                            project_id = ""
                        if not project_id:
                            await log(f"[{tag}] ❌ فشل اختيار المشروع")
                            await take_screenshot_and_send(
                                page, bot, user_id,
                                f"[{tag}] ❌ فشل اختيار المشروع"
                            )
                            return ""
                        step += 1
                        await log(f"[{tag}] • • {step} ✅")
                    else:
                        txt = await read_page_text(page)
                        m = re.search(r'qwiklabs-gcp-[\w\-]+', txt)
                        if m:
                            project_id = m.group(0)
                else:
                    step += 1
                    await log(f"[{tag}] • • {step} ✅ (Project: {project_id})")

                await asyncio.sleep(3)

                if project_id and not state["api_enabled"]:
                    step += 1
                    await log(f"[{tag}] • {step}) تفعيل Cloud Run API…")
                    try:
                        api_ok = await asyncio.wait_for(
                            enable_cloud_run_api(page, project_id, authuser),
                            timeout=90
                        )
                    except Exception:
                        api_ok = False
                    state["api_enabled"] = True
                    step += 1
                    await log(
                        f"[{tag}] • • {step} "
                        f"{'✅ API مفعّل' if api_ok else '⚠️ API غير مؤكد، متابعة…'}"
                    )

                step += 1
                await log(f"[{tag}] • {step}) فتح Cloud Run…")

                target_url = (
                    f"https://console.cloud.google.com/run/create"
                    f"?enableapi=true&deploymentType=container"
                    f"&project={project_id}&authuser={authuser}"
                ) if project_id else "https://console.cloud.google.com/run/create"

                try:
                    await goto_google_with_retry(
                        page, target_url, "صفحة إنشاء Cloud Run", attempts=2
                    )
                except GoogleNavigationError:
                    await log(
                        f"[{tag}] ⚠️ تعذر فتح Cloud Run بعد إعادة المحاولة. "
                        "سيتم تخطي الرابط للحفاظ على الطابور."
                    )
                    return "SKIP"

                await asyncio.sleep(8)

                try:
                    await asyncio.wait_for(handle_cloud_consent(page), timeout=20)
                except Exception:
                    pass

                step += 1
                await log(f"[{tag}] • • {step} ✅")

                await wait_for_input(
                    page,
                    ['input[aria-label*="Container image"]', 'input[type="text"]'],
                    timeout=20
                )
                await asyncio.sleep(2)

                step += 1
                await log(f"[{tag}] • {step}) تعبئة الحقول…")

                img_ok = await fill_by_shadow_dom(page, "Container image URL", CR_IMAGE)
                if not img_ok:
                    img_ok = await fill_by_placeholder_js(page, "container image", CR_IMAGE)
                if not img_ok:
                    await fill_field(page, [
                        'input[aria-label*="Container image"]',
                        'input[formcontrolname="imageUrl"]',
                    ], CR_IMAGE)

                await asyncio.sleep(2)

                srv_ok = await fill_by_shadow_dom(page, "Service name", CR_SERVICE_NAME)
                if not srv_ok:
                    srv_ok = await fill_by_placeholder_js(page, "service name", CR_SERVICE_NAME)
                if not srv_ok:
                    await fill_field(page, [
                        'input[aria-label*="Service name"]',
                        'input[formcontrolname="serviceName"]',
                    ], CR_SERVICE_NAME)

                await asyncio.sleep(2)

                step += 1
                await log(f"[{tag}] • • {step} ✅")

                await select_radio(page, "Allow public access")
                await asyncio.sleep(1)
                await select_radio(page, "Instance-based")
                await asyncio.sleep(1)

                for sel, val in [
                    ('input[aria-label*="Minimum number of instances"], '
                     'input[formcontrolname*="minInstance"]', CR_MIN),
                    ('input[aria-label*="Maximum number of instances"], '
                     'input[formcontrolname*="maxInstance"]', CR_MAX),
                ]:
                    try:
                        el = await page.query_selector(sel)
                        if el and await el.is_visible():
                            await el.click()
                            await page.keyboard.press("Control+A")
                            await page.keyboard.press("Delete")
                            await el.type(val, delay=40)
                    except Exception:
                        pass

                await click_text(page, [
                    "Containers, Networking, Security",
                    "Containers, Networking",
                ])
                await asyncio.sleep(2)

                try:
                    pi = await page.query_selector(
                        'input[aria-label*="Container port"], '
                        'input[formcontrolname*="containerPort"]'
                    )
                    if pi and await pi.is_visible():
                        cur = await pi.input_value()
                        if cur != CR_PORT:
                            await pi.click()
                            await page.keyboard.press("Control+A")
                            await page.keyboard.press("Delete")
                            await pi.type(CR_PORT, delay=40)
                except Exception:
                    pass

                await select_radio(page, "Second generation")
                await asyncio.sleep(1)
                await select_checkbox_label(page, "Startup CPU boost")
                await asyncio.sleep(1)

                step += 1
                await log(f"[{tag}] • {step}) Create الخدمة…")

                created = False
                for attempt in range(3):
                    if await click_create_service_safe(page):
                        created = True
                        break
                    await asyncio.sleep(2)

                if not created:
                    await take_screenshot_and_send(
                        page, bot, user_id,
                        f"[{tag}] ⚠️ فشل الضغط على Create"
                    )

                step += 1
                await log(f"[{tag}] • • {step} ✅ Create")

                step += 1
                await log(f"[{tag}] • {step}) انتظار رابط النشر…")

                try:
                    await asyncio.wait_for(
                        page.wait_for_load_state("networkidle"),
                        timeout=30
                    )
                except Exception:
                    pass

                final_url = ""
                try:
                    final_url = await asyncio.wait_for(
                        wait_for_run_url(page, timeout=120),
                        timeout=130
                    )
                except Exception:
                    final_url = ""

                if final_url:
                    domain = (
                        final_url
                        .replace("https://", "")
                        .replace("http://", "")
                        .rstrip("/")
                    )
                    vless = build_vless(domain)
                    await log(
                        f"[{tag}] • 🎉 <b>تم النشر!</b>\n\n"
                        f"🔗 <b>الرابط:</b>\n<code>{final_url}</code>\n\n"
                        f"📋 <b>VLESS:</b>\n<code>{vless}</code>"
                    )
                    await publish_result(final_url, vless)
                    return final_url
                else:
                    await log(f"[{tag}] ⏰ انتهى الوقت بدون رابط")
                    await take_screenshot_and_send(
                        page, bot, user_id,
                        f"[{tag}] ⏰ آخر حالة للصفحة"
                    )
                    return ""

        return ""

    except Exception as e:
        print(f"[WORKFLOW-ERR] {e}")
        try:
            await take_screenshot_and_send(
                page, bot, user_id,
                f"[{tag}] ⚠️ خطأ عام: {str(e)[:200]}"
            )
        except Exception:
            pass
        return ""


# ============================================================
# غلاف Timeout صارم 35 دقيقة
# ============================================================
async def full_workflow_safe(page, user_id, send_msg, username, sso_url: str = ""):
    tag = f"@{username}"
    try:
        result = await asyncio.wait_for(
            full_workflow(page, user_id, send_msg, username, sso_url),
            timeout=60 * 35
        )
        return result
    except asyncio.TimeoutError:
        try:
            await send_msg(
                f"[{tag}] ⏰ <b>انتهى الحد الأقصى (35 دق)</b>\n"
                f"تم إنهاء الجلسة تلقائياً."
            )
            await take_screenshot_and_send(
                page, bot, user_id,
                f"[{tag}] ⏰ Timeout 35 دقيقة"
            )
        except Exception:
            pass
        return "SKIP"
    except Exception as e:
        try:
            await send_msg(f"[{tag}] ⚠️ خطأ: {str(e)[:150]}")
        except Exception:
            pass
        return ""


# ============================================================
# ✅ جلسة متصفح محلية بدون بروكسي
# ============================================================
async def start_url_session(user_id, url):
    user_specific_dir = USER_DATA_DIR / f"user_{user_id}"
    p = None
    browser = None
    try:
        user_specific_dir.mkdir(parents=True, exist_ok=True)
        p = await async_playwright().start()
        launch_args = dict(
                user_data_dir=str(user_specific_dir),
                headless=True,
                viewport=VIEWPORT,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                    "--disable-background-networking",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-tools", "--disable-extensions", "--disable-plugins",
                    "--disable-sync", "--no-first-run", "--no-default-browser-check",
                    "--disable-default-apps", "--disable-component-update",
                    "--disable-domain-reliability",
                    "--disable-features=Translate,BackForwardCache,"
                    "AcceptCHFrame,MediaRouter,OptimizationHints",
                    "--disable-ipc-flooding-protection", "--disable-hang-monitor",
                    "--disable-prompt-on-repost",
                    "--disable-client-side-phishing-detection",
                    "--disable-popup-blocking", "--metrics-recording-only",
                    "--mute-audio", "--no-zygote",
                    "--js-flags=--max-old-space-size=256", "--single-process",
                ],
                ignore_default_args=["--enable-automation"],
            )
        browser = await p.chromium.launch_persistent_context(**launch_args)
        await browser.add_init_script(STEALTH_JS)
        page = browser.pages[0] if browser.pages else await browser.new_page()

        for pattern in ["**/*.{png,jpg,jpeg,gif,webp,svg,ico}"]:
            try:
                await page.route(pattern, lambda route: route.abort())
            except Exception:
                pass

        print(f"[URL] {url[:80]}")
        await goto_google_with_retry(page, url, "رابط Google SSO", attempts=2)

        try:
            await page.wait_for_selector(
                'input[type="password"], input[type="email"], input[name="identifier"]',
                timeout=15_000
            )
        except Exception:
            pass

        await asyncio.sleep(2)
        url_sessions[user_id] = {
            "playwright": p,
            "browser": browser,
            "page": page,
        }
        return True
    except Exception as exc:
        print(f"[SESSION] فشل بدء جلسة {user_id}: {type(exc).__name__}: {exc}")
        await safe_close_context(browser, p, user_id)
        shutil.rmtree(user_specific_dir, ignore_errors=True)
        raise


async def close_url_session(user_id):
    s = url_sessions.pop(user_id, None)
    if not s:
        return
    await safe_close_context(s.get("browser"), s.get("playwright"), user_id)
    user_dir = USER_DATA_DIR / f"user_{user_id}"
    shutil.rmtree(user_dir, ignore_errors=True)


async def safe_close_context(browser, playwright_instance, user_id):
    """إغلاق idempotent لا يرمي خطأ إذا أُغلقت الصفحة/السياق مسبقاً."""
    if browser:
        try:
            await browser.close(reason=f"finish user session {user_id}")
        except Exception as exc:
            print(f"[CLEANUP] browser already closed for {user_id}: {exc}")
    if playwright_instance:
        try:
            await playwright_instance.stop()
        except Exception as exc:
            print(f"[CLEANUP] playwright already stopped for {user_id}: {exc}")


async def submit_value(page, value, stage):
    sel_map = {
        "password": ['input[type="password"]'],
        "captcha": ['input[name="ca"]', 'input[id="ca"]', 'input[type="text"]'],
        "2fa": ['input[name="totpPin"]', 'input#totpPin', 'input[type="tel"]'],
    }
    frame, el = await find_input(page, sel_map.get(stage, []))
    if not el:
        return False
    try:
        await el.click()
        await el.fill(value)
        await asyncio.sleep(0.4)
        _, btn = await find_next_button(page)
        if btn:
            await btn.click()
        else:
            await el.press("Enter")
        return True
    except Exception as e:
        print(f"[SUBMIT] {e}")
        return False


# ============================================================
# إدارة الطابور
# ============================================================
@dataclass
class QueueItem:
    job_id: int
    user_id: int
    url: str
    send_msg: object
    username: str
    cancelled: bool = False


task_queue: asyncio.Queue[QueueItem] = asyncio.Queue()
active_users = set()
job_ids = count(1)
jobs_by_id: dict[int, QueueItem] = {}
queued_job_ids: deque[int] = deque()
active_job_id: int | None = None


def pending_jobs_for_user(user_id: int) -> list[QueueItem]:
    """يعيد كل روابط المستخدم المنتظرة بالترتيب الذي ستعمل به."""
    return [
        jobs_by_id[job_id]
        for job_id in queued_job_ids
        if job_id in jobs_by_id and jobs_by_id[job_id].user_id == user_id
    ]


def queue_position(job_id: int) -> int | None:
    """موضع المهمة بين الجلسة الجارية والمهام المنتظرة، إن كانت لم تبدأ."""
    try:
        waiting_index = list(queued_job_ids).index(job_id)
    except ValueError:
        return None
    return waiting_index + 1 + (1 if active_job_id is not None else 0)


def cancel_queued_jobs_for_user(user_id: int) -> int:
    """يلغي كل الروابط التي لم تبدأ بعد، من دون العبث بطابور asyncio الداخلي."""
    cancelled_count = 0
    for job_id in list(queued_job_ids):
        item = jobs_by_id.get(job_id)
        if item and item.user_id == user_id:
            item.cancelled = True
            jobs_by_id.pop(job_id, None)
            queued_job_ids.remove(job_id)
            cancelled_count += 1
    return cancelled_count


async def queue_worker():
    global active_job_id
    while True:
        item = await task_queue.get()
        if item.cancelled:
            task_queue.task_done()
            continue

        user_id = item.user_id
        url = item.url
        send_msg = item.send_msg
        username = item.username
        try:
            queued_job_ids.remove(item.job_id)
        except ValueError:
            # يمكن أن يحدث فقط إذا أُلغي الرابط قبل أن يصله العامل.
            task_queue.task_done()
            continue

        active_job_id = item.job_id
        active_users.add(user_id)
        print(f"[WORKER] ▶ بدأ جلسة {user_id} (job={item.job_id})")
        try:
            success = await start_url_session(user_id, url)
            if success:
                s = url_sessions.get(user_id)
                if s:
                    result = await full_workflow_safe(
                        s["page"], user_id, send_msg, username, sso_url=url
                    )
                    if result == "SKIP":
                        try:
                            await send_msg(
                                f"[@{username}] ⏭ <b>تم تخطي دورك</b> بسبب التجمد.\n"
                                f"يمكنك إرسال الرابط مجدداً."
                            )
                        except Exception:
                            pass
        except GoogleNavigationError as e:
            print(f"[WORKER-NAVIGATION-ERR] {e}")
            try:
                await send_msg(
                    "⚠️ تعذر الوصول إلى Google بعد إعادة المحاولة. "
                    "تم تخطي هذا الرابط حتى لا يتوقف الطابور؛ أرسله لاحقاً."
                )
            except Exception:
                pass
        except Exception as e:
            print(f"[WORKER-ERR] {e}")
            try:
                await send_msg(f"⚠️ خطأ: {str(e)[:150]}")
            except Exception:
                pass
        finally:
            await close_url_session(user_id)
            active_users.discard(user_id)
            jobs_by_id.pop(item.job_id, None)
            active_job_id = None
            task_queue.task_done()
            print(f"[WORKER] ✅ انتهت جلسة {user_id} — الطابور حر")


# ============================================================
# البوت
# ============================================================
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN غير مضبوط. أضفه كمتغير بيئة في إعدادات Cloud Run."
    )


bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Google Cloud → Cloud Run</b>\n\n"
        "📎 أرسل رابط <b>Google SSO</b> من:\n"
        "https://www.skills.google/focuses/33353?parent=catalog\n\n"
        "🔐 كلمة السر → يخبرك البوت\n"
        "🎯 كل شيء تلقائي\n\n"
        "/cancel — إلغاء\n/status — حالة"
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    uid = message.from_user.id
    pending = pending_jobs_for_user(uid)
    lines = []

    if uid in active_users:
        lines.append("🟢 لديك مشروع قيد التشغيل حالياً.")

    if pending:
        positions = [queue_position(item.job_id) for item in pending]
        positions = [str(pos) for pos in positions if pos is not None]
        lines.append(
            f"⏳ لديك <b>{len(pending)}</b> رابط/روابط في الطابور "
            f"(المواضع: <b>{'، '.join(positions)}</b>)."
        )

    if not lines:
        lines.append("❌ لا توجد لديك أي مشاريع تعمل حالياً أو في الطابور.")

    lines.extend([
        f"📋 إجمالي الروابط المنتظرة: <b>{len(queued_job_ids)}</b>",
    ])
    await message.answer("\n".join(lines))


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    uid = message.from_user.id
    cancelled_waiting = cancel_queued_jobs_for_user(uid)
    if uid in active_users:
        await close_url_session(uid)
        active_users.discard(uid)
        suffix = (
            f" وتم حذف <b>{cancelled_waiting}</b> رابط/روابط من الطابور."
            if cancelled_waiting else ""
        )
        await message.answer(f"✅ تم إلغاء الجلسة الحالية.{suffix}")
    elif cancelled_waiting:
        await message.answer(
            f"✅ تم حذف <b>{cancelled_waiting}</b> رابط/روابط من الطابور."
        )
    else:
        await message.answer("ليس لديك مشروع يعمل حالياً أو روابط في الطابور.")


@dp.message(F.text.startswith("http"))
async def handle_url(message: Message):
    uid = message.from_user.id
    username = message.from_user.username or "dzakt"
    url = message.text.strip()

    if not is_valid_google_sso_url(url):
        await message.answer(
            "⚠️ <b>رابط غير صالح!</b>\n\n"
            "يرجى إرسال الرابط الصحيح من:\n"
            "https://www.skills.google/focuses/33353?parent=catalog",
            disable_web_page_preview=True
        )
        try:
            await message.delete()
        except Exception:
            pass
        return

    async def send_msg(text):
        try:
            await message.answer(text)
        except Exception:
            pass

    # يقبل أكثر من رابط من المستخدم نفسه؛ كل رابط عنصر مستقل في الطابور.
    item = QueueItem(
        job_id=next(job_ids),
        user_id=uid,
        url=url,
        send_msg=send_msg,
        username=username,
    )
    jobs_by_id[item.job_id] = item
    queued_job_ids.append(item.job_id)
    position = queue_position(item.job_id)
    await task_queue.put(item)

    if position and position > 1:
        await message.answer(
            f"📥 تم استلام الرابط رقم <b>{item.job_id}</b>! "
            f"مكانه في الطابور: <b>{position}</b>\n"
            "يمكنك إرسال رابط آخر وسيُضاف بعده تلقائياً."
        )
    else:
        await message.answer(
            f"📥 تم استلام الرابط رقم <b>{item.job_id}</b> وسيبدأ الآن.\n"
            "يمكنك إرسال رابط آخر وسيُضاف إلى الطابور تلقائياً."
        )


@dp.message(F.text & ~F.text.startswith("/") & ~F.text.startswith("http"))
async def handle_input(message: Message):
    uid = message.from_user.id
    if uid not in url_sessions:
        return

    s = url_sessions[uid]
    page = s["page"]
    value = message.text.strip()

    try:
        stage = await asyncio.wait_for(detect_stage(page), timeout=10)
    except Exception:
        stage = "unknown"

    if stage in ("password", "captcha", "2fa"):
        if await submit_value(page, value, stage):
            await message.answer(f"✅ تم الإرسال ({stage})")
        else:
            await take_screenshot_and_send(
                page, bot, uid,
                f"⚠️ فشل الإرسال في مرحلة: {stage}"
            )
    else:
        await take_screenshot_and_send(
            page, bot, uid,
            f"⚠️ المرحلة الحالية: <code>{stage}</code>"
        )


async def main():
    print("🤖 البوت شغال...")
    asyncio.create_task(queue_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
