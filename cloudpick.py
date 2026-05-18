#!/usr/bin/env python3
"""
cloudpick — перебор публичных IP на cloud.ru Evolution
до попадания в нужную подсеть.

Установка:
  bash <(curl -fsSL https://raw.githubusercontent.com/ibmaga/cloudpick/main/install.sh)

Использование:
  cloudpick
"""

import os
import sys
import time
import random
import ipaddress
import logging
import requests
from datetime import datetime

# ── Проверка дублирующего процесса ───────────────────────────────────────────

def check_already_running():
    current_pid = os.getpid()
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-a", "-f", "cloudpick"],
            capture_output=True, text=True
        )
        pids = [
            line.split()[0]
            for line in result.stdout.strip().splitlines()
            if line and int(line.split()[0]) != current_pid
        ]
        if pids:
            print(f"⚠️  cloudpick уже запущен (PID: {', '.join(pids)})")
            ans = input("   Запустить ещё один? [y/N]: ").strip().lower()
            if ans != "y":
                sys.exit(0)
    except Exception:
        pass

# ── Настройки по умолчанию ───────────────────────────────────────────────────

DEFAULT_TARGET      = "46.243.142.0/23"
DEFAULT_DELAY_MIN   = 15
DEFAULT_DELAY_MAX   = 25
DEFAULT_ACTIVE_WAIT = 60
LOG_FILE            = "/var/log/cloudpick.log"

AUTH_URL  = "https://iam.api.cloud.ru/api/v1/auth/token"
API_BASE  = "https://console.cloud.ru/u-api/svp/svc/v1"

KNOWN_AZ = {
    "ru.AZ-1": "7c99a597-8516-494f-a2c7-d7377048681e",
    "ru.AZ-2": "479a4ab3-3ff3-4972-95c5-7610bac5c0bb",
    "ru.AZ-3": "2c63c482-2532-4bba-8c9b-70ea330507bf",
}

# ── Логирование ───────────────────────────────────────────────────────────────

def setup_logging():
    logger = logging.getLogger("cloudpick")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # stdout
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # файл
    try:
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except PermissionError:
        print(f"⚠️  Нет прав на запись в {LOG_FILE}, логи только в stdout.")

    return logger

log: logging.Logger = None  # инициализируется в main()

def out(msg: str):
    """Вывод в stdout и лог-файл с датой."""
    log.info(msg)

# ── Ввод ─────────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    val = input(prompt).strip()
    return val if val else default

def ask_az_ids() -> list:
    print("\n── Зоны доступности ──────────────────────────────────────")
    print("AZ ID можно найти перехватив POST-запрос при аренде IP в консоли.")
    print("Нажми Enter чтобы пропустить зону.\n")

    result = []
    for az_name, default_id in KNOWN_AZ.items():
        hint = f" [{default_id}]" if default_id else ""
        val  = ask(f"  {az_name} AZ ID{hint}: ", default_id or "")
        if val:
            result.append((az_name, val))
        else:
            print(f"  ⚠️  {az_name} пропущена")

    if not result:
        print("❌ Не задана ни одна зона. Выход.")
        sys.exit(1)

    return result

def setup() -> dict:
    print("╔══════════════════════════════════════════╗")
    print("║           cloudpick — cloud.ru           ║")
    print("╚══════════════════════════════════════════╝\n")

    key_id = os.environ.get("CLOUDRU_KEY_ID") or ask("  Key ID: ")
    key_secret = os.environ.get("CLOUDRU_KEY_SECRET") or ask("  Key Secret: ")
    if not key_id or not key_secret:
        print("❌ Key ID и Key Secret обязательны.")
        sys.exit(1)

    project_id = os.environ.get("CLOUDRU_PROJECT_ID") or ask("  Project ID: ")
    if not project_id:
        print("❌ Project ID обязателен.")
        sys.exit(1)

    target_raw = ask(f"\n  Целевая подсеть [{DEFAULT_TARGET}]: ", DEFAULT_TARGET)
    try:
        target = ipaddress.ip_network(target_raw, strict=False)
    except ValueError:
        print(f"❌ Неверная подсеть: {target_raw}")
        sys.exit(1)

    az_list = ask_az_ids()

    print("\n── Параметры ─────────────────────────────────────────────")
    delay_min = int(ask(f"  Мин. пауза (сек) [{DEFAULT_DELAY_MIN}]: ", str(DEFAULT_DELAY_MIN)))
    delay_max = int(ask(f"  Макс. пауза (сек) [{DEFAULT_DELAY_MAX}]: ", str(DEFAULT_DELAY_MAX)))
    max_att   = int(ask("  Макс. попыток [1000]: ", "1000"))

    print()
    return {
        "key_id":     key_id,
        "key_secret": key_secret,
        "project_id": project_id,
        "target":     target,
        "az_list":    az_list,
        "delay_min":  delay_min,
        "delay_max":  delay_max,
        "max_att":    max_att,
    }

# ── API ───────────────────────────────────────────────────────────────────────

def get_token(cfg: dict) -> str:
    resp = requests.post(AUTH_URL, json={
        "keyId":  cfg["key_id"],
        "secret": cfg["key_secret"],
    }, timeout=15)
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"Нет токена: {resp.text}")
    return token

def hdrs(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def allocate_ip(token: str, cfg: dict, az_id: str, attempt: int) -> tuple:
    url  = f"{API_BASE}/floating-ips"
    body = {
        "name":                 f"pick-{attempt:04d}",
        "description":          "",
        "availability_zone_id": az_id,
        "tag_ids":              [],
        "project_id":           cfg["project_id"],
    }
    resp = requests.post(url, headers=hdrs(token), json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["ip_address"], data["id"]

def wait_active(token: str, cfg: dict, fip_id: str) -> bool:
    url    = f"{API_BASE}/floating-ips/{fip_id}"
    params = {"project_id": cfg["project_id"]}
    states = []
    for _ in range(DEFAULT_ACTIVE_WAIT // 3):
        try:
            resp = requests.get(url, headers=hdrs(token), params=params, timeout=15)
            if resp.ok:
                state = resp.json().get("state", "")
                if not states or states[-1] != state:
                    states.append(state)
                    print(f" [{state}]", end="", flush=True)
                if state in ("active", "available"):
                    return True
                if state == "error":
                    return False
        except Exception:
            pass
        time.sleep(3)
    return False

def release_ip(token: str, cfg: dict, fip_id: str):
    url    = f"{API_BASE}/floating-ips/{fip_id}"
    params = {"project_id": cfg["project_id"]}
    for attempt in range(5):
        try:
            resp = requests.delete(url, headers=hdrs(token), params=params, timeout=30)
            if resp.status_code in (200, 202, 204, 404):
                return
            raise RuntimeError(f"{resp.status_code} {resp.text[:200]}")
        except Exception as e:
            if attempt < 4:
                wait = 10 * (attempt + 1)
                out(f"   ↻ retry удаления {attempt+1}/5, жду {wait}с: {e}")
                time.sleep(wait)
            else:
                out(f"   ⚠️  Не удалось удалить {fip_id[:8]}…: {e}")

# ── Основной цикл ─────────────────────────────────────────────────────────────

def main():
    check_already_running()
    global log
    log = setup_logging()

    cfg   = setup()
    token = get_token(cfg)

    out(f"{'='*55}")
    out(f"cloudpick запущен")
    out(f"Цель: {cfg['target']}")
    out(f"Зоны: {', '.join(n for n, _ in cfg['az_list'])}")
    out(f"Макс. попыток: {cfg['max_att']}")
    out(f"{'='*55}")

    az_cycle = cfg["az_list"]
    az_idx   = 0
    found    = []

    for attempt in range(1, cfg["max_att"] + 1):
        if attempt > 1 and attempt % 10 == 0:
            token = get_token(cfg)
            out("🔄 Токен обновлён")

        az_name, az_id = az_cycle[az_idx % len(az_cycle)]
        az_idx += 1

        try:
            ip, fip_id = allocate_ip(token, cfg, az_id, attempt)
            in_tgt = ipaddress.ip_address(ip) in cfg["target"]
            keep   = ip.startswith("46.") and not in_tgt

            if in_tgt:
                status = "🎉 ПОПАЛИ!"
            elif keep:
                status = "⭐ СОХРАНЯЕМ"
            else:
                status = "❌ Мимо"

            out(f"[{attempt:04d}] {az_name} │ {ip:<18} {fip_id[:8]}… — {status}")

            if in_tgt:
                found.append((ip, fip_id, az_name))
                out(f"✅ Нужный IP: {ip}  ID: {fip_id}  Зона: {az_name}")
                again = ask("\nИскать ещё? [y/N]: ", "n").lower()
                if again != "y":
                    break
                continue

            if keep:
                out(f"   💾 Сохранён (46.x), не удаляем")
                time.sleep(random.uniform(cfg["delay_min"], cfg["delay_max"]))
                continue

            print("         ожидаем active…", end=" ", flush=True)
            ok = wait_active(token, cfg, fip_id)
            print()
            log.info("active ok" if ok else "active таймаут")

            release_ip(token, cfg, fip_id)

        except requests.HTTPError as e:
            code = e.response.status_code
            out(f"[{attempt:04d}] HTTP {code}: {e.response.text[:150]}")
            wait = 75 if code == 422 else (45 if code == 429 else random.uniform(cfg["delay_min"], cfg["delay_max"]))
            out(f"         пауза {wait:.0f}с…")
            time.sleep(wait)
            continue

        except Exception as e:
            out(f"[{attempt:04d}] Ошибка: {e}")
            time.sleep(random.uniform(cfg["delay_min"], cfg["delay_max"]))
            continue

        delay = random.uniform(cfg["delay_min"], cfg["delay_max"])
        out(f"         пауза {delay:.1f}с…")
        time.sleep(delay)

    out(f"{'='*55}")
    if found:
        out(f"📋 Найдено IP: {len(found)}")
        for ip, fid, az in found:
            out(f"   {ip} ({fid}) — {az}")
    else:
        out(f"⚠️  Не нашли за {cfg['max_att']} попыток.")
    out(f"cloudpick завершён")
    out(f"{'='*55}")

if __name__ == "__main__":
    main()
