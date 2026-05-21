#!/usr/bin/env python3
"""
cloudpick — перебор публичных IP на cloud.ru и Selectel
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

# ── Проверка дублирующего процесса ───────────────────────────────────────────

def check_already_running():
    current_pid = os.getpid()
    try:
        import subprocess
        result = subprocess.run(["pgrep", "-a", "-f", "cloudpick"], capture_output=True, text=True)
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

# ── Константы ─────────────────────────────────────────────────────────────────

DEFAULT_DELAY_MIN   = 15
DEFAULT_DELAY_MAX   = 25
DEFAULT_ACTIVE_WAIT = 60
LOG_FILE            = "/var/log/cloudpick.log"

# cloud.ru
CLOUDRU_AUTH_URL = "https://iam.api.cloud.ru/api/v1/auth/token"
CLOUDRU_API_BASE = "https://console.cloud.ru/u-api/svp/svc/v1"
CLOUDRU_KNOWN_AZ = {
    "ru.AZ-1": None,
    "ru.AZ-2": "479a4ab3-3ff3-4972-95c5-7610bac5c0bb",
    "ru.AZ-3": None,
}

# Selectel
SEL_AUTH_URL   = "https://cloud.api.selcloud.ru/identity/v3/auth/tokens"
SEL_RESELL_URL = "https://api.selectel.ru/vpc/resell/v2"
SEL_REGIONS    = ["ru-1", "ru-2", "ru-3", "ru-7", "ru-8", "ru-9"]

# Целевые подсети — всё что в белых списках РКН
TARGET_NETWORKS = {
    "46.243.142.0/23":  "Sber Cloud (cloud.ru)",
    "31.129.42.0/24":   "Selectel",
    "5.188.0.0/16":     "Selectel",
    "185.91.52.0/24":   "Selectel",
    "89.208.0.0/16":    "VK Cloud",
    "217.0.0.0/8":      "VK Cloud",
    "109.0.0.0/8":      "VK Cloud",
    "212.233.0.0/16":   "VK Cloud",
    "213.219.0.0/16":   "VK Cloud",
    "51.0.0.0/8":       "Yandex Cloud",
    "84.204.0.0/16":    "Yandex Cloud",
    "158.160.0.0/16":   "Yandex Cloud",
    "79.0.0.0/8":       "Reg Cloud",
    "178.248.0.0/16":   "Curator Pro",
    "46.8.0.0/16":      "Contell",
}
TARGET_NETS = {ipaddress.ip_network(n, strict=False): label for n, label in TARGET_NETWORKS.items()}

# ── Логирование ───────────────────────────────────────────────────────────────

log = None

def setup_logging():
    logger = logging.getLogger("cloudpick")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    try:
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except PermissionError:
        print(f"⚠️  Нет прав на запись в {LOG_FILE}, логи только в stdout.")
    return logger

def out(msg: str):
    log.info(msg)

# ── Helpers ───────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    val = input(prompt).strip()
    return val if val else default



def check_ip(ip: str) -> tuple:
    """Возвращает (matched_label или None)."""
    addr = ipaddress.ip_address(ip)
    for net, label in TARGET_NETS.items():
        if addr in net:
            return label
    return None

# ── cloud.ru ──────────────────────────────────────────────────────────────────

def cloudru_setup() -> dict:
    print("\n── cloud.ru credentials ──────────────────────────────────")
    key_id     = os.environ.get("CLOUDRU_KEY_ID")     or ask("  Key ID: ")
    key_secret = os.environ.get("CLOUDRU_KEY_SECRET") or ask("  Key Secret: ")
    project_id = os.environ.get("CLOUDRU_PROJECT_ID") or ask("  Project ID: ")
    if not key_id or not key_secret or not project_id:
        print("❌ Все поля обязательны.")
        sys.exit(1)

    print("\n── cloud.ru зоны доступности ─────────────────────────────")
    print("AZ ID перехватить из DevTools при аренде IP. Enter = пропустить.\n")
    az_list = []
    for az_name, default_id in CLOUDRU_KNOWN_AZ.items():
        hint = f" [{default_id}]" if default_id else ""
        val  = ask(f"  {az_name} AZ ID{hint}: ", default_id or "")
        if val:
            az_list.append((az_name, val))
        else:
            print(f"  ⚠️  {az_name} пропущена")

    if not az_list:
        print("❌ Не задана ни одна зона.")
        sys.exit(1)

    return {"key_id": key_id, "key_secret": key_secret, "project_id": project_id, "az_list": az_list}

def cloudru_get_token(cfg: dict) -> str:
    for attempt in range(5):
        try:
            resp = requests.post(CLOUDRU_AUTH_URL, json={
                "keyId": cfg["key_id"], "secret": cfg["key_secret"]
            }, timeout=30)
            resp.raise_for_status()
            token = resp.json().get("access_token")
            if not token:
                raise RuntimeError(f"Нет токена: {resp.text}")
            return token
        except Exception as e:
            if attempt < 4:
                w = 10 * (attempt + 1)
                print(f"   ↻ retry токена {attempt+1}/5, жду {w}с: {e}")
                time.sleep(w)
            else:
                raise

def cloudru_hdrs(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def cloudru_allocate(token: str, cfg: dict, az_id: str, attempt: int) -> tuple:
    resp = requests.post(f"{CLOUDRU_API_BASE}/floating-ips", headers=cloudru_hdrs(token), json={
        "name": f"pick-{attempt:04d}", "description": "",
        "availability_zone_id": az_id, "tag_ids": [], "project_id": cfg["project_id"],
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["ip_address"], data["id"]

def cloudru_wait_active(token: str, cfg: dict, fip_id: str) -> bool:
    url    = f"{CLOUDRU_API_BASE}/floating-ips/{fip_id}"
    params = {"project_id": cfg["project_id"]}
    states = []
    for _ in range(DEFAULT_ACTIVE_WAIT // 3):
        try:
            resp = requests.get(url, headers=cloudru_hdrs(token), params=params, timeout=15)
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

def cloudru_release(token: str, cfg: dict, fip_id: str):
    url    = f"{CLOUDRU_API_BASE}/floating-ips/{fip_id}"
    params = {"project_id": cfg["project_id"]}
    for attempt in range(5):
        try:
            resp = requests.delete(url, headers=cloudru_hdrs(token), params=params, timeout=30)
            if resp.status_code in (200, 202, 204, 404):
                return
            raise RuntimeError(f"{resp.status_code} {resp.text[:200]}")
        except Exception as e:
            if attempt < 4:
                w = 10 * (attempt + 1)
                out(f"   ↻ retry удаления {attempt+1}/5, жду {w}с: {e}")
                time.sleep(w)
            else:
                out(f"   ⚠️  Не удалось удалить {fip_id[:8]}…: {e}")

# ── Selectel ──────────────────────────────────────────────────────────────────

def sel_setup() -> dict:
    print("\n── Selectel credentials ──────────────────────────────────")
    token      = os.environ.get("SEL_TOKEN")      or ask("  API Token (Профиль → API ключи): ")
    project_id = os.environ.get("SEL_PROJECT_ID") or ask("  Project ID: ")
    if not token or not project_id:
        print("❌ Все поля обязательны.")
        sys.exit(1)

    print("\n── Selectel регионы ──────────────────────────────────────")
    print("Доступные: ru-1, ru-2, ru-3, ru-7, ru-8, ru-9")
    regions_raw = ask(f"  Регионы через запятую [все]: ", ",".join(SEL_REGIONS))
    regions = [r.strip() for r in regions_raw.split(",") if r.strip()]

    return {
        "token":      token,
        "project_id": project_id,
        "regions":    regions,
    }

def sel_get_token(cfg: dict) -> str:
    """Статический токен Selectel — возвращаем как есть."""
    return cfg["token"]

def sel_hdrs(token: str) -> dict:
    return {"X-Token": token, "Content-Type": "application/json"}

def sel_allocate(token: str, cfg: dict, region: str, attempt: int) -> tuple:
    """Создать floating IP через Selectel Resell API v2."""
    url  = f"{SEL_RESELL_URL}/floatingips/projects/{cfg['project_id']}"
    body = {"floatingips": [{"region": region, "quantity": 1}]}
    resp = requests.post(url, headers=sel_hdrs(token), json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    fips = data.get("floatingips", [])
    if not fips:
        raise RuntimeError(f"Нет IP в ответе: {data}")
    fip = fips[0]
    return fip["floating_ip_address"], fip["id"]

def sel_release(token: str, fip_id: str):
    """Удалить floating IP Selectel."""
    url = f"{SEL_RESELL_URL}/floatingips/{fip_id}"
    for attempt in range(5):
        try:
            resp = requests.delete(url, headers=sel_hdrs(token), timeout=30)
            if resp.status_code in (200, 202, 204, 404):
                return
            raise RuntimeError(f"{resp.status_code} {resp.text[:200]}")
        except Exception as e:
            if attempt < 4:
                w = 10 * (attempt + 1)
                out(f"   ↻ retry удаления Selectel {attempt+1}/5, жду {w}с: {e}")
                time.sleep(w)
            else:
                out(f"   ⚠️  Не удалось удалить {fip_id[:8]}…: {e}")

# ── Основной цикл ─────────────────────────────────────────────────────────────

def setup_providers() -> list:
    """Спрашиваем какие провайдеры использовать и их credentials."""
    print("╔══════════════════════════════════════════╗")
    print("║     cloudpick — cloud.ru + Selectel      ║")
    print("╚══════════════════════════════════════════╝\n")

    providers = []

    use_cloudru = ask("Использовать cloud.ru? [Y/n]: ", "y").lower()
    if use_cloudru != "n":
        cfg = cloudru_setup()
        cfg["provider"] = "cloudru"
        providers.append(cfg)

    use_sel = ask("\nИспользовать Selectel? [Y/n]: ", "y").lower()
    if use_sel != "n":
        cfg = sel_setup()
        cfg["provider"] = "selectel"
        providers.append(cfg)

    if not providers:
        print("❌ Выбран хотя бы один провайдер.")
        sys.exit(1)

    return providers

def main():
    check_already_running()
    global log
    log = setup_logging()

    providers = setup_providers()

    print("\n── Параметры ─────────────────────────────────────────────")
    print(f"  Целей: {len(TARGET_NETWORKS)} подсетей из белых списков РКН")
    delay_min = int(ask(f"  Мин. пауза (сек) [{DEFAULT_DELAY_MIN}]: ", str(DEFAULT_DELAY_MIN)))
    delay_max = int(ask(f"  Макс. пауза (сек) [{DEFAULT_DELAY_MAX}]: ", str(DEFAULT_DELAY_MAX)))
    max_att   = int(ask("  Макс. попыток [1000]: ", "1000"))
    print()

    # Инициализируем токены
    tokens = {}
    for p in providers:
        if p["provider"] == "cloudru":
            tokens["cloudru"] = cloudru_get_token(p)
        elif p["provider"] == "selectel":
            tokens["selectel"] = sel_get_token(p)

    # Строим очередь задач: (provider_cfg, zone/region)
    tasks = []
    for p in providers:
        if p["provider"] == "cloudru":
            for az_name, az_id in p["az_list"]:
                tasks.append((p, az_name, az_id))
        elif p["provider"] == "selectel":
            for region in p["regions"]:
                tasks.append((p, region, region))

    out(f"{'='*55}")
    out(f"cloudpick запущен")
    out(f"Целей: {len(TARGET_NETWORKS)} подсетей РКН белого списка")
    out(f"Провайдеры: {', '.join(p['provider'] for p in providers)}")
    out(f"Задач в очереди: {len(tasks)}")
    out(f"Макс. попыток: {max_att}")
    out(f"{'='*55}")

    found = []
    task_idx = 0

    for attempt in range(1, max_att + 1):
        # Обновляем токены каждые 10 попыток
        if attempt > 1 and attempt % 10 == 0:
            for p in providers:
                if p["provider"] == "cloudru":
                    tokens["cloudru"] = cloudru_get_token(p)
                elif p["provider"] == "selectel":
                    tokens["selectel"] = sel_get_token(p)
            out("🔄 Токены обновлены")

        p_cfg, zone_name, zone_id = tasks[task_idx % len(tasks)]
        task_idx += 1
        provider = p_cfg["provider"]
        token    = tokens[provider]
        label    = f"{provider}:{zone_name}"

        try:
            if provider == "cloudru":
                ip, fip_id = cloudru_allocate(token, p_cfg, zone_id, attempt)
            else:
                ip, fip_id = sel_allocate(token, p_cfg, zone_name, attempt)

            matched = check_ip(ip)

            if matched:
                status = f"🎉 ПОПАЛИ! ({matched})"
            else:
                status = "❌ Мимо"

            out(f"[{attempt:04d}] {label:<20} │ {ip:<18} {fip_id[:8]}… — {status}")

            if matched:
                found.append((ip, fip_id, label, matched))
                out(f"✅ IP найден: {ip}  Провайдер: {matched}  ID: {fip_id}  Зона: {label}")
                again = ask("\nИскать ещё? [y/N]: ", "n").lower()
                if again != "y":
                    break
                continue

            # Не тот — удаляем
            if provider == "cloudru":
                print("         ожидаем active…", end=" ", flush=True)
                ok = cloudru_wait_active(token, p_cfg, fip_id)
                print()
                log.info("active ok" if ok else "active таймаут")
                cloudru_release(token, p_cfg, fip_id)
            else:
                # Selectel — сразу удаляем, статус не нужен
                time.sleep(3)
                sel_release(token, fip_id)

        except requests.HTTPError as e:
            code = e.response.status_code
            out(f"[{attempt:04d}] HTTP {code}: {e.response.text[:150]}")
            wait = 75 if code == 422 else (45 if code == 429 else random.uniform(delay_min, delay_max))
            out(f"         пауза {wait:.0f}с…")
            time.sleep(wait)
            continue

        except Exception as e:
            out(f"[{attempt:04d}] Ошибка: {e}")
            time.sleep(random.uniform(delay_min, delay_max))
            continue

        delay = random.uniform(delay_min, delay_max)
        out(f"         пауза {delay:.1f}с…")
        time.sleep(delay)

    out(f"{'='*55}")
    if found:
        out(f"📋 Найдено IP: {len(found)}")
        for ip, fid, zone, prov in found:
            out(f"   {ip} [{prov}] ({fid}) — {zone}")
    else:
        out(f"⚠️  Не нашли за {max_att} попыток.")
    out("cloudpick завершён")
    out(f"{'='*55}")

if __name__ == "__main__":
    main()
