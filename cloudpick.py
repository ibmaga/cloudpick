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
import requests

# ── Настройки по умолчанию ───────────────────────────────────────────────────

DEFAULT_TARGET  = "46.243.142.0/23"
DEFAULT_DELAY_MIN = 15
DEFAULT_DELAY_MAX = 25
DEFAULT_ACTIVE_WAIT = 60

AUTH_URL  = "https://iam.api.cloud.ru/api/v1/auth/token"
API_BASE  = "https://console.cloud.ru/u-api/svp/svc/v1"

KNOWN_AZ = {
    "ru.AZ-1": None,  # заполняется при вводе
    "ru.AZ-2": "479a4ab3-3ff3-4972-95c5-7610bac5c0bb",
    "ru.AZ-3": None,
}

# ─────────────────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    val = input(prompt).strip()
    return val if val else default

def ask_az_ids() -> list[tuple[str, str]]:
    """Интерактивный ввод AZ ID. Возвращает список (az_name, az_id)."""
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
    """Интерактивная настройка при первом запуске."""
    print("╔══════════════════════════════════════════╗")
    print("║           cloudpick — cloud.ru           ║")
    print("╚══════════════════════════════════════════╝\n")

    # Credentials
    key_id = (
        os.environ.get("CLOUDRU_KEY_ID") or
        ask("  Key ID (CLOUDRU_KEY_ID): ")
    )
    key_secret = (
        os.environ.get("CLOUDRU_KEY_SECRET") or
        ask("  Key Secret (CLOUDRU_KEY_SECRET): ")
    )
    if not key_id or not key_secret:
        print("❌ Key ID и Key Secret обязательны.")
        sys.exit(1)

    project_id = (
        os.environ.get("CLOUDRU_PROJECT_ID") or
        ask("  Project ID (CLOUDRU_PROJECT_ID): ")
    )
    if not project_id:
        print("❌ Project ID обязателен.")
        sys.exit(1)

    # Целевая подсеть
    target_raw = ask(f"\n  Целевая подсеть [{DEFAULT_TARGET}]: ", DEFAULT_TARGET)
    try:
        target = ipaddress.ip_network(target_raw, strict=False)
    except ValueError:
        print(f"❌ Неверная подсеть: {target_raw}")
        sys.exit(1)

    # Зоны
    az_list = ask_az_ids()

    # Параметры
    print("\n── Параметры ─────────────────────────────────────────────")
    delay_min  = int(ask(f"  Мин. пауза между попытками (сек) [{DEFAULT_DELAY_MIN}]: ", str(DEFAULT_DELAY_MIN)))
    delay_max  = int(ask(f"  Макс. пауза между попытками (сек) [{DEFAULT_DELAY_MAX}]: ", str(DEFAULT_DELAY_MAX)))
    max_att    = int(ask("  Макс. попыток [1000]: ", "1000"))

    print()
    return {
        "key_id":      key_id,
        "key_secret":  key_secret,
        "project_id":  project_id,
        "target":      target,
        "az_list":     az_list,
        "delay_min":   delay_min,
        "delay_max":   delay_max,
        "max_att":     max_att,
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

def allocate_ip(token: str, cfg: dict, az_id: str, attempt: int) -> tuple[str, str]:
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

def wait_active(token: str, cfg: dict, fip_id: str, wait: int) -> bool:
    url    = f"{API_BASE}/floating-ips/{fip_id}"
    params = {"project_id": cfg["project_id"]}
    for _ in range(wait // 3):
        try:
            resp = requests.get(url, headers=hdrs(token), params=params, timeout=15)
            if resp.ok:
                state = resp.json().get("state", "")
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
                print(f"   ↻ retry удаления {attempt+1}/5, жду {wait}с: {e}")
                time.sleep(wait)
            else:
                print(f"   ⚠️  Не удалось удалить {fip_id[:8]}…: {e}")

# ── Основной цикл ─────────────────────────────────────────────────────────────

def main():
    cfg   = setup()
    token = get_token(cfg)
    print("✅ Токен получен")
    print(f"🎯 Цель: {cfg['target']}")
    print(f"🌐 Зоны: {', '.join(n for n, _ in cfg['az_list'])}\n")

    az_cycle = cfg["az_list"]
    az_idx   = 0
    found    = []

    for attempt in range(1, cfg["max_att"] + 1):
        if attempt > 1 and attempt % 10 == 0:
            token = get_token(cfg)
            print("🔄 Токен обновлён")

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
                status = "❌ Мимо    "

            print(f"[{attempt:04d}] {az_name} │ {ip:<18} {fip_id[:8]}… — {status}")

            if in_tgt:
                found.append((ip, fip_id, az_name))
                print(f"\n✅ Нужный IP: {ip}")
                print(f"   ID: {fip_id}")
                print(f"   Зона: {az_name}")
                print(f"   Назначь его на ВМ в консоли cloud.ru.")
                again = ask("\nИскать ещё? [y/N]: ", "n").lower()
                if again != "y":
                    break
                continue

            if keep:
                print(f"   💾 Сохранён (46.x), не удаляем")
                time.sleep(random.uniform(cfg["delay_min"], cfg["delay_max"]))
                continue

            print(f"         ожидаем active…", end=" ", flush=True)
            ok = wait_active(token, cfg, fip_id, cfg["active_wait"] if "active_wait" in cfg else DEFAULT_ACTIVE_WAIT)
            print(" ok" if ok else " таймаут")

            release_ip(token, cfg, fip_id)

        except requests.HTTPError as e:
            code = e.response.status_code
            print(f"[{attempt:04d}] HTTP {code}: {e.response.text[:150]}")
            wait = 75 if code == 422 else (45 if code == 429 else random.uniform(cfg["delay_min"], cfg["delay_max"]))
            print(f"         пауза {wait:.0f}с…")
            time.sleep(wait)
            continue

        except Exception as e:
            print(f"[{attempt:04d}] Ошибка: {e}")
            time.sleep(random.uniform(cfg["delay_min"], cfg["delay_max"]))
            continue

        delay = random.uniform(cfg["delay_min"], cfg["delay_max"])
        print(f"         пауза {delay:.1f}с…")
        time.sleep(delay)

    if found:
        print(f"\n📋 Найдено IP: {len(found)}")
        for ip, fid, az in found:
            print(f"   {ip} ({fid}) — {az}")
    else:
        print(f"\n⚠️  Не нашли за {cfg['max_att']} попыток.")

if __name__ == "__main__":
    main()
