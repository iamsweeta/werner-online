from __future__ import annotations

import math
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_BROWSER_LOCK = threading.Semaphore(2)


@dataclass(frozen=True)
class PublicCalculator:
    company: str
    url: str
    source_label: str
    timeout: int = 32


CALCULATORS: dict[str, PublicCalculator] = {
    "СДЭК": PublicCalculator("СДЭК", "https://www.cdek.ru/ru/calculate/", "Открытый калькулятор СДЭК"),
    "DPD": PublicCalculator("DPD", "https://dpd.ru/calc", "Открытый калькулятор DPD"),
    "Байкал Сервис": PublicCalculator("Байкал Сервис", "https://www.baikalsr.ru/services/types/avtomobilnye-perevozki-gruzov/", "Официальный публичный калькулятор Байкал Сервис"),
    "Возовоз": PublicCalculator("Возовоз", "https://vozovoz.ru/order/create/", "Официальный публичный калькулятор Возовоза", timeout=38),
    "CTSgroup": PublicCalculator("CTSgroup", "https://www.cts-group.ru/prices", "Официальная тарифная таблица CTS Group"),
    "Главтрасса": PublicCalculator("Главтрасса", "https://glavtrassa.ru/clients/calc/", "Открытый калькулятор Главтрассы"),
    "ДЛ": PublicCalculator("ДЛ", "https://widgets.dellin.ru/calculator/", "Официальный виджет-калькулятор Деловых Линий", timeout=38),
    "Новая Линия": PublicCalculator("Новая Линия", "https://tknl.ru/price/", "Официальная тарифная таблица Новая Линия", timeout=38),
    "Pony Express": PublicCalculator("Pony Express", "https://www.ponyexpress.ru/support/servisy-samoobsluzhivaniya/tariff/", "Открытый калькулятор PONY EXPRESS"),
}

CITY_FROM_WORDS = ("откуда", "город отправ", "пункт отправ", "населенный пункт отправ", "origin", "from")
CITY_TO_WORDS = ("куда", "город получ", "город назнач", "пункт назнач", "населенный пункт назнач", "destination", "to")
WEIGHT_WORDS = ("вес", "масса", "weight")
VOLUME_WORDS = ("объем", "объём", "volume")
LENGTH_WORDS = ("длина", "length")
WIDTH_WORDS = ("ширина", "width")
HEIGHT_WORDS = ("высота", "height")
CALCULATE_WORDS = ("рассчитать", "посчитать", "расчет", "расчёт", "узнать стоимость", "calculate")
COOKIE_WORDS = ("принять", "согласен", "разрешить все", "accept", "ok")


def _browser_candidates() -> list[tuple[str, str | None]]:
    candidates: list[tuple[str, str | None]] = []
    edge_paths = [
        shutil.which("msedge"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ]
    chrome_paths = [
        shutil.which("chrome"), shutil.which("google-chrome"), shutil.which("chromium"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in edge_paths:
        if path and Path(path).exists():
            candidates.append(("edge", path))
    for path in chrome_paths:
        if path and Path(path).exists():
            candidates.append(("chrome", path))
    # Selenium Manager can still discover browsers not found above.
    if not any(kind == "edge" for kind, _ in candidates):
        candidates.append(("edge", None))
    if not any(kind == "chrome" for kind, _ in candidates):
        candidates.append(("chrome", None))
    return candidates


def browser_runtime_status() -> dict[str, Any]:
    try:
        import selenium  # type: ignore
        selenium_version = getattr(selenium, "__version__", "установлен")
    except Exception as exc:
        return {
            "available": False,
            "selenium": False,
            "message": f"Не установлен Selenium: {exc}",
            "browsers": [],
        }
    browsers = [f"{kind}: {path or 'автообнаружение'}" for kind, path in _browser_candidates()]
    return {
        "available": True,
        "selenium": True,
        "selenium_version": selenium_version,
        "message": "Фоновые адаптеры используют Edge/Chrome без открытия окон пользователю.",
        "browsers": browsers,
    }


def _make_driver(headless: bool):
    from selenium import webdriver  # type: ignore

    errors: list[str] = []
    for kind, binary in _browser_candidates():
        try:
            if kind == "edge":
                options = webdriver.EdgeOptions()
            else:
                options = webdriver.ChromeOptions()
            options.page_load_strategy = "eager"
            if binary:
                options.binary_location = binary
            if headless:
                options.add_argument("--headless=new")
            options.add_argument("--window-size=1440,1100")
            options.add_argument("--lang=ru-RU")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--disable-notifications")
            options.add_argument("--disable-popup-blocking")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            driver = webdriver.Edge(options=options) if kind == "edge" else webdriver.Chrome(options=options)
            driver.set_page_load_timeout(35)
            driver.set_script_timeout(20)
            return driver, kind
        except Exception as exc:
            errors.append(f"{kind}: {exc}")
    raise RuntimeError("Не удалось запустить Edge/Chrome. " + " | ".join(errors[-2:]))


def _visible_elements(driver, selector: str):
    from selenium.webdriver.common.by import By  # type: ignore

    result = []
    for element in driver.find_elements(By.CSS_SELECTOR, selector):
        try:
            if element.is_displayed() and element.is_enabled():
                result.append(element)
        except Exception:
            continue
    return result


def _element_descriptor(driver, element) -> str:
    try:
        return str(driver.execute_script(
            """
            const e = arguments[0];
            const parts = [e.getAttribute('placeholder'), e.getAttribute('aria-label'), e.getAttribute('name'), e.id, e.getAttribute('data-testid')];
            if (e.labels) for (const l of e.labels) parts.push(l.innerText);
            const parent = e.closest('label, .field, .form-group, .input, .control, div');
            if (parent) parts.push((parent.innerText || '').slice(0, 180));
            return parts.filter(Boolean).join(' ').toLowerCase();
            """,
            element,
        ) or "")
    except Exception:
        return ""


def _best_input(driver, words: tuple[str, ...], used: set[str], negatives: tuple[str, ...] = ()):
    candidates = _visible_elements(driver, "input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea, [contenteditable=true]")
    scored = []
    for index, element in enumerate(candidates):
        try:
            marker = element.id or element.get_attribute("name") or f"idx:{index}"
            if marker in used:
                continue
            text = _element_descriptor(driver, element)
            score = sum(8 for word in words if word in text) - sum(10 for word in negatives if word in text)
            input_type = (element.get_attribute("type") or "").lower()
            if input_type in {"number", "text", "search", ""}:
                score += 1
            if score > 0:
                scored.append((score, -index, marker, element, text))
        except Exception:
            continue
    if not scored:
        return None
    scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
    _, _, marker, element, _ = scored[0]
    used.add(marker)
    return element


def _set_value(element, value: str, autocomplete: bool = False) -> None:
    from selenium.webdriver.common.keys import Keys  # type: ignore

    element.click()
    try:
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.BACKSPACE)
    except Exception:
        pass
    element.send_keys(value)
    if autocomplete:
        time.sleep(1.0)
        # Prefer the exact visible suggestion over blind ArrowDown. CTS and a
        # few carrier widgets can render a stale/default first suggestion, so
        # keyboard-only selection may silently keep Moscow when SPB was typed.
        clicked=False
        try:
            driver=element.parent
            clicked=bool(driver.execute_script(r"""
              const wanted=arguments[0].trim().toLowerCase().replace(/ё/g,'е');
              const norm=x=>(x||'').trim().toLowerCase().replace(/ё/g,'е').replace(/\s+/g,' ');
              const visible=e=>!!(e && (e.offsetWidth||e.offsetHeight||e.getClientRects().length));
              const nodes=[...document.querySelectorAll('[role=option],li,.ui-menu-item,.autocomplete-item,.suggestion,.select2-results__option,[class*=suggest]')];
              const hit=nodes.find(n=>visible(n) && norm(n.innerText||n.textContent)===wanted);
              if(!hit) return false;
              hit.scrollIntoView({block:'nearest'}); hit.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
              hit.click(); return true;
            """,value))
        except Exception:
            clicked=False
        if not clicked:
            element.send_keys(Keys.ARROW_DOWN)
            element.send_keys(Keys.ENTER)
        time.sleep(0.5)


def _click_matching_button(driver, words: tuple[str, ...]) -> bool:
    from selenium.webdriver.common.by import By  # type: ignore

    selectors = "button, input[type=submit], input[type=button], [role=button], a, .btn, .button, [class*=\"btn\"], [class*=\"button\"]"
    candidates = []
    for index, element in enumerate(driver.find_elements(By.CSS_SELECTOR, selectors)):
        try:
            if not element.is_displayed() or not element.is_enabled():
                continue
            text = " ".join(filter(None, [element.text, element.get_attribute("value"), element.get_attribute("aria-label")])).strip().lower()
            score = sum(10 for word in words if word in text)
            if score:
                candidates.append((score, -index, element))
        except Exception:
            continue
    if not candidates:
        return False
    candidates.sort(reverse=True, key=lambda item: (item[0], item[1]))
    element = candidates[0][2]
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        time.sleep(0.2)
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)
    return True


def _accept_cookies(driver) -> None:
    try:
        _click_matching_button(driver, COOKIE_WORDS)
    except Exception:
        pass


def _body_text(driver) -> str:
    try:
        return str(driver.execute_script("return document.body ? document.body.innerText : '';" ) or "")
    except Exception:
        return ""


def _extract_prices(before: str, after: str) -> list[dict[str, Any]]:
    before_lines = {re.sub(r"\s+", " ", line).strip() for line in before.splitlines() if line.strip()}
    after_lines = [re.sub(r"\s+", " ", line).strip() for line in after.splitlines() if line.strip()]
    changed = [line for line in after_lines if line not in before_lines]
    lines = changed if changed else after_lines
    pattern = re.compile(r"(?<!\d)(\d{1,3}(?:[\s\u00a0]\d{3})*(?:[.,]\d{1,2})?|\d{3,7}(?:[.,]\d{1,2})?)\s*(?:₽|руб(?:\.|ля|лей)?)", re.I)
    found: list[dict[str, Any]] = []
    for line_index, line in enumerate(lines):
        previous = lines[line_index - 1] if line_index else ""
        for match in pattern.finditer(line):
            raw = match.group(1).replace("\u00a0", " ").replace(" ", "").replace(",", ".")
            try:
                value = float(raw)
            except ValueError:
                continue
            if value < 100 or value > 20_000_000:
                continue
            context = f"{previous} {line}".lower()
            score = 0
            for word in ("стоимость", "итого", "тариф", "доставка", "перевозка", "к оплате", "цена", "результат"):
                if word in context:
                    score += 3
            for word in ("страхов", "объявлен", "скидк", "эконом", "упаков", "налож", "телефон"):
                if word in context:
                    score -= 5
            is_minimum = bool(re.search(r"(?:^|\s)от\s+\d", context))
            if is_minimum:
                score -= 1
            found.append({"price": value, "score": score, "context": context, "price_is_minimum": is_minimum})
    found.sort(key=lambda row: (-row["score"], row["price"]))
    return found


def _fill_calculator(driver, origin: str, destination: str, weight: float, volume: float, *, route_preselected: bool = False) -> dict[str, Any]:
    used: set[str] = set()
    origin_el = _best_input(driver, CITY_FROM_WORDS, used, negatives=CITY_TO_WORDS)
    destination_el = _best_input(driver, CITY_TO_WORDS, used, negatives=CITY_FROM_WORDS)
    weight_el = _best_input(driver, WEIGHT_WORDS, used)
    volume_el = _best_input(driver, VOLUME_WORDS, used)
    length_el = _best_input(driver, LENGTH_WORDS, used)
    width_el = _best_input(driver, WIDTH_WORDS, used)
    height_el = _best_input(driver, HEIGHT_WORDS, used)

    missing = []
    if not route_preselected:
        if not origin_el:
            missing.append("город отправления")
        if not destination_el:
            missing.append("город назначения")
    if not weight_el:
        missing.append("вес")
    if missing:
        raise RuntimeError("Не найдены поля: " + ", ".join(missing))

    if origin_el:
        _set_value(origin_el, origin, autocomplete=True)
    if destination_el:
        _set_value(destination_el, destination, autocomplete=True)
    _set_value(weight_el, f"{float(weight):g}")

    side_cm = max(1.0, math.pow(max(float(volume), 0.000001), 1 / 3) * 100.0)
    if volume_el:
        _set_value(volume_el, f"{float(volume):g}")
    if length_el:
        _set_value(length_el, f"{side_cm:.1f}")
    if width_el:
        _set_value(width_el, f"{side_cm:.1f}")
    if height_el:
        _set_value(height_el, f"{side_cm:.1f}")

    return {
        "origin": bool(origin_el), "destination": bool(destination_el), "weight": bool(weight_el),
        "volume": bool(volume_el), "dimensions": bool(length_el and width_el and height_el),
    }


def _fill_calculator_in_page_or_frame(driver, origin: str, destination: str, weight: float, volume: float, *, route_preselected: bool = False) -> dict[str, Any]:
    """Fill a public calculator located either in the page or in one of its iframes.

    Baikal Service currently exposes the calculator through an iframe on some
    versions of the site.  The previous generic browser adapter only searched
    the top-level document, so it reported that city/weight fields were absent
    even though they were visible to the user.
    """
    from selenium.webdriver.common.by import By  # type: ignore

    driver.switch_to.default_content()
    first_error: Exception | None = None
    try:
        return _fill_calculator(driver, origin, destination, weight, volume, route_preselected=route_preselected)
    except Exception as exc:
        first_error = exc

    frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
    for frame in frames:
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
            return _fill_calculator(driver, origin, destination, weight, volume, route_preselected=route_preselected)
        except Exception:
            continue
    driver.switch_to.default_content()
    if first_error:
        raise first_error
    raise RuntimeError("Не найдены поля калькулятора ни на странице, ни во встроенном iframe")



def _show_assist_banner(driver, company: str) -> None:
    try:
        driver.execute_script(
            """
            const old = document.getElementById('__tariff_compare_banner');
            if (old) old.remove();
            const box = document.createElement('div');
            box.id = '__tariff_compare_banner';
            box.style.cssText = 'position:fixed;z-index:2147483647;left:16px;right:16px;top:16px;padding:14px 18px;background:#0f172a;color:white;border-radius:12px;font:15px/1.35 Arial;box-shadow:0 8px 30px rgba(0,0,0,.35)';
            box.textContent = 'Логистическая аналитика: проверьте заполнение калькулятора и нажмите «Рассчитать». После появления цены окно закроется автоматически.';
            document.body.appendChild(box);
            """
        )
    except Exception:
        pass

def calculate_from_public_site(
    company: str,
    origin: str,
    destination: str,
    weight: float,
    volume: float,
    *,
    allow_visible_fallback: bool = False,
    url_override: str | None = None,
    route_preselected: bool = False,
) -> dict[str, Any]:
    config = CALCULATORS[company]
    target_url = url_override or config.url
    with _BROWSER_LOCK:
        last_error: Exception | None = None
        # Never open interactive browser windows during automatic refresh.
        # A visible fallback could wait 90 seconds and still had no reliable way
        # to transfer a manually obtained result back into the application.
        attempts = [True]
        for headless in attempts:
            driver = None
            try:
                driver, browser_kind = _make_driver(headless=headless)
                driver.get(target_url)
                time.sleep(2.0)
                _accept_cookies(driver)
                before = _body_text(driver)
                try:
                    filled = _fill_calculator_in_page_or_frame(driver, origin, destination, weight, volume, route_preselected=route_preselected)
                    clicked = _click_matching_button(driver, CALCULATE_WORDS)
                    if not clicked and headless:
                        # Some calculators submit after the last autocomplete selection.
                        from selenium.webdriver.common.keys import Keys  # type: ignore
                        active = driver.switch_to.active_element
                        active.send_keys(Keys.ENTER)
                except Exception:
                    if headless:
                        raise
                    filled = {"manual_assist": True}
                if not headless:
                    _show_assist_banner(driver, company)
                deadline = time.time() + (config.timeout if headless else 90)
                candidates: list[dict[str, Any]] = []
                after = ""
                while time.time() < deadline:
                    time.sleep(1.0)
                    after = _body_text(driver)
                    candidates = _extract_prices(before, after)
                    if candidates and candidates[0]["score"] >= 2:
                        break
                if not candidates:
                    title = driver.title or ""
                    raise RuntimeError(f"Калькулятор не показал цену. Страница: {title}")
                best = candidates[0]
                return {
                    "ok": True,
                    "price": round(float(best["price"]), 2),
                    "price_is_minimum": bool(best.get("price_is_minimum")),
                    "browser": browser_kind,
                    "headless": headless,
                    "source_url": target_url,
                    "source_label": config.source_label,
                    "message": "Цена считана с результата официального публичного калькулятора.",
                    "details": filled,
                }
            except Exception as exc:
                last_error = exc
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
        return {
            "ok": False,
            "source_url": target_url,
            "source_label": config.source_label,
            "message": str(last_error or "Не удалось получить цену из открытого калькулятора"),
        }



def _money_value(text: Any) -> float | None:
    raw = str(text or "").replace("\u00a0", " ").replace(",", ".")
    m = re.search(r"-?\d+(?:[\s ]\d{3})*(?:\.\d+)?", raw)
    if not m:
        return None
    try:
        return float(m.group(0).replace(" ", ""))
    except Exception:
        return None


def _set_route_selects_by_text(driver, origin: str, destination: str) -> bool:
    """Set origin/destination controls even when a site hides native selects behind a JS widget."""
    script = r"""
    const origin = arguments[0].toLowerCase(), dest = arguments[1].toLowerCase();
    const sels = [...document.querySelectorAll('select')];
    function info(s){
      const opts=[...s.options].map(o=>(o.textContent||'').trim());
      const box=s.closest('label,.field,.form-group,.filter,.select,div');
      const desc=((box&&box.innerText)||'').toLowerCase().slice(0,300);
      return {s,opts,desc};
    }
    const all=sels.map(info).filter(x=>x.opts.some(t=>t.toLowerCase()===origin)&&x.opts.some(t=>t.toLowerCase()===dest));
    function choose(kind, text){
      let cand=all.find(x=> kind==='from' ? /отправ|откуда|from/.test(x.desc) : /назнач|получ|куда|to/.test(x.desc));
      if(!cand){ const idx=kind==='from'?0:1; cand=all[idx]; }
      if(!cand) return false;
      const opt=[...cand.s.options].find(o=>(o.textContent||'').trim().toLowerCase()===text.toLowerCase());
      if(!opt) return false;
      cand.s.value=opt.value; opt.selected=true;
      cand.s.dispatchEvent(new Event('input',{bubbles:true}));
      cand.s.dispatchEvent(new Event('change',{bubbles:true}));
      if(window.jQuery) try{ window.jQuery(cand.s).trigger('change'); }catch(e){}
      return true;
    }
    return {from:choose('from',arguments[0]), to:choose('to',arguments[1]), count:all.length};
    """
    try:
        result = driver.execute_script(script, origin, destination) or {}
        if result.get("from") and result.get("to"):
            time.sleep(0.7)
            return True
    except Exception:
        pass
    # Custom autocomplete fallback.
    used: set[str] = set()
    try:
        origin_el = _best_input(driver, CITY_FROM_WORDS, used, negatives=CITY_TO_WORDS)
        destination_el = _best_input(driver, CITY_TO_WORDS, used, negatives=CITY_FROM_WORDS)
        if origin_el and destination_el:
            _set_value(origin_el, origin, autocomplete=True)
            _set_value(destination_el, destination, autocomplete=True)
            return True
    except Exception:
        pass
    return False



def _set_origin_control_by_text(driver, origin: str) -> bool:
    """Set only the CTS origin control. Destination is read from the table row.

    The current CTS /prices page uses an autocomplete-style input for origin; it
    does not expose the two native selects assumed by earlier builds.
    """
    script=r"""
    const wanted=arguments[0].trim().toLowerCase().replace(/ё/g,'е');
    const norm=x=>(x||'').trim().toLowerCase().replace(/ё/g,'е');
    const sels=[...document.querySelectorAll('select')];
    for(const s of sels){
      const opt=[...s.options].find(o=>norm(o.textContent)===wanted);
      if(!opt) continue;
      const box=s.closest('label,.field,.form-group,.filter,.select,div');
      const desc=norm((box&&box.innerText)||'');
      if(desc && !/отправ|откуда|from/.test(desc)) continue;
      s.value=opt.value; opt.selected=true; s.dispatchEvent(new Event('input',{bubbles:true})); s.dispatchEvent(new Event('change',{bubbles:true}));
      if(window.jQuery) try{window.jQuery(s).val(opt.value).trigger('change').trigger('change.select2');}catch(e){}
      return true;
    }
    return false;
    """
    try:
        if driver.execute_script(script,origin):
            time.sleep(.5); return True
    except Exception:
        pass
    try:
        used:set[str]=set()
        el=_best_input(driver,CITY_FROM_WORDS,used,negatives=CITY_TO_WORDS)
        if not el:
            # Current CTS markup can render the two autocomplete inputs without
            # useful name/placeholder attributes. Find the small container that
            # also owns the "Показать тарифы" control and take its first
            # visible text/search input (origin precedes destination on /prices).
            el=driver.execute_script(r"""
            const norm=x=>(x||'').trim().toLowerCase().replace(/ё/g,'е');
            const visible=e=>!!(e && (e.offsetWidth||e.offsetHeight||e.getClientRects().length));
            const controls=[...document.querySelectorAll('button,a,input[type=submit],input[type=button],[role=button],.btn,.button,[class*=btn],[class*=button]')];
            const btn=controls.find(x=>/показать\s+тариф/.test(norm(x.innerText||x.value||x.textContent||'')));
            if(btn){
              let n=btn;
              for(let depth=0;n&&n!==document.body&&depth<7;depth++,n=n.parentElement){
                const ins=[...n.querySelectorAll('input:not([type=hidden])')].filter(visible);
                if(ins.length>=2) return ins[0];
              }
            }
            const ins=[...document.querySelectorAll('input:not([type=hidden])')].filter(visible);
            for(const input of ins){
              const label=input.id?document.querySelector(`label[for="${CSS.escape(input.id)}"]`):null;
              const own=norm([input.name,input.id,input.placeholder,input.getAttribute('aria-label'),label&&label.innerText].filter(Boolean).join(' '));
              if(/город отправ|откуда|пункт отправ|origin|from/.test(own)) return input;
            }
            return null;
            """)
        if not el:
            return False
        _set_value(el,origin,autocomplete=True)
        return True
    except Exception:
        return False


def _submit_route_form(driver, origin: str, destination: str, button_words: tuple[str, ...] = ("показать",)) -> dict[str, Any]:
    """Submit the actual tariff form after setting its two city selects.

    This does not depend on Select2's visual widgets. It edits the underlying
    native selects inside the same form as the tariff submit button and submits
    that form. This is substantially more reliable in headless Edge/Chrome.
    """
    words = [str(x).lower() for x in button_words]
    script = r"""
    const origin=arguments[0].trim().toLowerCase().replace(/ё/g,'е');
    const dest=arguments[1].trim().toLowerCase().replace(/ё/g,'е');
    const words=arguments[2]||[];
    const norm=x=>(x||'').trim().toLowerCase().replace(/ё/g,'е');
    const buttons=[...document.querySelectorAll('button,input[type=submit],input[type=button],a')];
    const btn=buttons.find(b=>words.some(w=>norm(b.innerText||b.value||'').includes(w)));
    if(!btn) return {ok:false,reason:'submit button not found'};
    let form=btn.closest('form');
    let scope=form || btn.parentElement;
    if(!form){
      let n=btn;
      while(n && n!==document.body){
        const sels=[...n.querySelectorAll('select')];
        const city=sels.filter(s=>{const opts=[...s.options].map(o=>norm(o.textContent));return opts.includes(origin)&&opts.includes(dest)});
        if(city.length>=2){scope=n;break;}
        n=n.parentElement;
      }
    }
    const selects=[...(scope||document).querySelectorAll('select')];
    const city=selects.filter(s=>{const opts=[...s.options].map(o=>norm(o.textContent));return opts.includes(origin)&&opts.includes(dest)});
    if(city.length<2) return {ok:false,reason:'two city selects not found',selectCount:selects.length,cityCount:city.length};
    function setText(s,text){
      const o=[...s.options].find(x=>norm(x.textContent)===text);
      if(!o) return false;
      s.value=o.value; o.selected=true;
      s.dispatchEvent(new Event('input',{bubbles:true}));
      s.dispatchEvent(new Event('change',{bubbles:true}));
      if(window.jQuery){try{window.jQuery(s).val(o.value).trigger('change').trigger('change.select2');}catch(e){}}
      return true;
    }
    const okFrom=setText(city[0],origin), okTo=setText(city[1],dest);
    if(!okFrom||!okTo) return {ok:false,reason:'city option not selected'};
    // Prefer the real form submission because many tariff pages build the
    // result server-side and synthetic click handlers are brittle in headless mode.
    form=city[0].closest('form') || form;
    if(form){
      if(form.requestSubmit){try{form.requestSubmit(btn.matches('button,input[type=submit]')?btn:undefined);}catch(e){form.requestSubmit();}}
      else form.submit();
      return {ok:true,mode:'form',from:city[0].value,to:city[1].value};
    }
    try{btn.click();}catch(e){btn.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));}
    return {ok:true,mode:'click',from:city[0].value,to:city[1].value};
    """
    try:
        result = driver.execute_script(script, origin, destination, words) or {}
        if isinstance(result, dict) and result.get("ok"):
            return result
        return result if isinstance(result, dict) else {"ok": False, "reason": str(result)}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def _dom_tables(driver) -> list[dict[str, Any]]:
    try:
        data = driver.execute_script(r"""
        return [...document.querySelectorAll('table')].map(t => ({
          headers:[...t.querySelectorAll('thead th')].map(x=>(x.innerText||x.textContent||'').trim()),
          rows:[...t.querySelectorAll('tbody tr')].map(r=>[...r.querySelectorAll('th,td')].map(x=>(x.innerText||x.textContent||'').trim())),
          text:(t.innerText||'').slice(0,20000)
        }));
        """)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _cts_table_point(cells: list[str], weight: float) -> dict[str, Any] | None:
    # Official CTS table order published on /prices:
    # city, days, minimum, >3000, 2000-3000, 1501-2000, 1201-1500,
    # 1001-1200, 801-1000, 501-800, 301-500, 201-300, 101-200, 1-100.
    if len(cells) < 14:
        return None
    minimum = _money_value(cells[2])
    w = float(weight)
    if w <= 100: idx = 13
    elif w <= 200: idx = 12
    elif w <= 300: idx = 11
    elif w <= 500: idx = 10
    elif w <= 800: idx = 9
    elif w <= 1000: idx = 8
    elif w <= 1200: idx = 7
    elif w <= 1500: idx = 6
    elif w <= 2000: idx = 5
    elif w <= 3000: idx = 4
    else: idx = 3
    rate = _money_value(cells[idx])
    if minimum is None or rate is None or rate <= 0:
        return None
    return {"minimum": minimum, "rate": rate, "price": max(minimum, w * rate), "days": cells[1]}


def _calculate_many_cts_table(origin: str, destination: str, points: list[tuple[float,float]], on_result=None) -> list[dict[str,Any]]:
    """Read CTS's current origin-specific weight table from the public site.

    v42.3 accepts the page only after the actual tariff heading confirms the
    requested origin.  If the default page is on Moscow, only the origin
    autocomplete is changed; destination is then validated by the table row.
    """
    config=CALCULATORS["CTSgroup"]
    results:list[dict[str,Any]]=[]
    errors=[]
    target_urls=[config.url,"https://cts-group.ru/prices","http://cts-group.ru/prices"]
    target_urls=list(dict.fromkeys(target_urls))

    def run_one(target_url:str):
        driver=None
        try:
            driver,browser_kind=_make_driver(headless=True)
            try:
                from selenium.common.exceptions import TimeoutException
                driver.set_page_load_timeout(18)
                try: driver.get(target_url)
                except TimeoutException:
                    try: driver.execute_script("window.stop();")
                    except Exception: pass
            except Exception:
                driver.get(target_url)
            time.sleep(.8); _accept_cookies(driver)

            def tariff_heading()->str:
                try:
                    return str(driver.execute_script(r"""
                    const nodes=[...document.querySelectorAll('h1,h2,h3,h4,.h1,.h2,.h3,.title')];
                    const hit=nodes.find(n=>/тарифы\s+на\s+перевозку\s+груза\s+из\s+города/i.test((n.innerText||'').trim()));
                    return hit ? (hit.innerText||'').trim() : '';
                    """) or "")
                except Exception:
                    return ""

            def heading_matches()->bool:
                h=tariff_heading().lower().replace('ё','е')
                wanted=origin.lower().replace('ё','е')
                return bool(h and wanted in h and 'из города' in h)

            # Moscow is currently the site's default.  If the heading already
            # matches, do not touch the controls at all.
            if not heading_matches():
                if not _set_origin_control_by_text(driver,origin):
                    # A native-form fallback remains for future site variants.
                    submitted=_submit_route_form(driver,origin,destination,("показать тарифы","показать"))
                    if not submitted.get("ok"):
                        raise RuntimeError("CTS: origin autocomplete/native form not set: "+str(submitted.get("reason") or submitted))
                else:
                    if not _click_matching_button(driver,("показать тарифы","показать")):
                        raise RuntimeError("CTS: кнопка «Показать тарифы» не найдена")

                deadline=time.time()+20
                while time.time()<deadline and not heading_matches():
                    time.sleep(.5)

            heading=tariff_heading()
            if not heading_matches():
                raise RuntimeError(f"CTS не переключил таблицу на город отправления {origin}; фактический заголовок: {heading or 'не найден'}")

            route_cells=None
            deadline=time.time()+10
            wanted_dest=destination.strip().lower().replace("ё","е")
            while time.time()<deadline and route_cells is None:
                for table in _dom_tables(driver):
                    for cells in table.get("rows") or []:
                        if cells and str(cells[0]).strip().lower().replace("ё","е")==wanted_dest and len(cells)>=14:
                            route_cells=cells; break
                    if route_cells: break
                if route_cells is None: time.sleep(.4)
            if not route_cells:
                raise RuntimeError(f"CTS подтвердил origin={origin}, но не показал строку назначения {destination}")

            local=[]
            for weight,volume in points:
                calc=_cts_table_point(route_cells,float(weight))
                if not calc:
                    row={"ok":False,"weight":float(weight),"volume":float(volume),"source_url":target_url,"source_label":config.source_label,"message":"Не распознана весовая ставка CTS"}
                else:
                    row={"ok":True,"weight":float(weight),"volume":float(volume),"price":round(calc["price"],2),"price_is_minimum":False,
                         "browser":browser_kind+("-http" if target_url.startswith("http://") else ""),"source_url":target_url,"source_label":config.source_label,"source_type":"Официальная тарифная таблица CTS Group",
                         "rate_per_kg":calc["rate"],"minimum_charge":calc["minimum"],"delivery_days":calc["days"],
                         "formula":f"max(минимум {calc['minimum']:g} ₽; {float(weight):g} кг × {calc['rate']:g} ₽/кг)",
                         "message":f"Ставка прочитана из текущей таблицы CTS с подтверждённым заголовком «{heading}»."}
                local.append(row)
            return local
        finally:
            if driver is not None:
                try: driver.quit()
                except Exception: pass

    with _BROWSER_LOCK:
        for target_url in target_urls:
            try:
                results=run_one(target_url)
                if any(r.get("ok") for r in results):
                    break
            except Exception as exc:
                errors.append(f"{target_url}: {type(exc).__name__}: {exc}")
                results=[]

    if not results:
        results=[{"ok":False,"weight":None,"volume":None,"source_url":config.url,"source_label":config.source_label,"message":" | ".join(errors[-3:]) or "CTS browser table unavailable"}]
    if on_result:
        for row in results:
            try: on_result(row)
            except Exception: pass
    return results


def calculate_many_from_public_site(
    company: str,
    origin: str,
    destination: str,
    points: list[tuple[float, float]],
    on_result=None,
) -> list[dict[str, Any]]:
    """Read several control points with one browser process.

    Route refresh needs the full customer grid. Launching Edge/Chrome separately
    for every weight made the refresh needlessly slow and could outlive the UI
    polling window. Reusing one driver keeps the operation bounded while each
    returned value still comes from a separate official calculator submission.
    """
    if company == "CTSgroup":
        return _calculate_many_cts_table(origin, destination, points, on_result=on_result)
    if company == "Новая Линия":
        return _calculate_many_newline_table(origin, destination, points, on_result=on_result)
    config = CALCULATORS[company]
    results: list[dict[str, Any]] = []
    with _BROWSER_LOCK:
        driver = None
        try:
            driver, browser_kind = _make_driver(headless=True)
            consecutive_failures = 0
            for weight, volume in points:
                try:
                    driver.switch_to.default_content()
                    driver.get(config.url)
                    time.sleep(1.4)
                    _accept_cookies(driver)
                    before = _body_text(driver)
                    filled = _fill_calculator_in_page_or_frame(driver, origin, destination, weight, volume)
                    clicked = _click_matching_button(driver, CALCULATE_WORDS)
                    if not clicked:
                        from selenium.webdriver.common.keys import Keys  # type: ignore
                        driver.switch_to.active_element.send_keys(Keys.ENTER)
                    deadline = time.time() + min(config.timeout, 24)
                    candidates: list[dict[str, Any]] = []
                    while time.time() < deadline:
                        time.sleep(0.8)
                        candidates = _extract_prices(before, _body_text(driver))
                        if candidates and candidates[0]["score"] >= 2:
                            break
                    if not candidates:
                        raise RuntimeError("Калькулятор не показал стоимость")
                    best = candidates[0]
                    row = {
                        "ok": True,
                        "weight": float(weight),
                        "volume": float(volume),
                        "price": round(float(best["price"]), 2),
                        "price_is_minimum": bool(best.get("price_is_minimum")),
                        "browser": browser_kind,
                        "headless": True,
                        "source_url": config.url,
                        "source_label": config.source_label,
                        "message": "Цена считана с результата официального публичного калькулятора.",
                        "details": filled,
                    }
                    results.append(row)
                    if on_result:
                        try:
                            on_result(row)
                        except Exception:
                            pass
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    row = {
                        "ok": False, "weight": float(weight), "volume": float(volume),
                        "source_url": config.url, "source_label": config.source_label,
                        "message": str(exc),
                    }
                    results.append(row)
                    if on_result:
                        try:
                            on_result(row)
                        except Exception:
                            pass
                    # If the page structure changed, do not spend several minutes
                    # repeating the same selector failure for every matrix row.
                    if consecutive_failures >= 2:
                        break
        except Exception as exc:
            if not results:
                results.append({
                    "ok": False, "weight": None, "volume": None,
                    "source_url": config.url, "source_label": config.source_label,
                    "message": str(exc),
                })
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
    return results
