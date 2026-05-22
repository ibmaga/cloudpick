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
from pathlib import Path

# ── .env поддержка ────────────────────────────────────────────────────────────

def load_env():
    """Загружает .env из текущей директории или ~/.cloudpick/.env"""
    paths = [
        Path(".env"),
        Path.home() / ".cloudpick" / ".env",
    ]
    for path in paths:
        if path.exists():
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
            print(f"📄 Загружен конфиг: {path}")
            return
    print("ℹ️  .env не найден — используется интерактивный ввод")
    print(f"   Создай ~/.cloudpick/.env для автозапуска без вопросов\n")

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
CLOUDRU_LB_BASE  = "https://console.cloud.ru/u-api/svp/v2/nlb"
CLOUDRU_KNOWN_AZ = {
    "ru.AZ-1": "7c99a597-8516-494f-a2c7-d7377048681e",
    "ru.AZ-2": "479a4ab3-3ff3-4972-95c5-7610bac5c0bb",
    "ru.AZ-3": "2c63c482-2532-4bba-8c9b-70ea330507bf",
}
LB_AZ_CONFIG = {
    "ru.AZ-2": {
        "subnet_id":        "8024d269-9d2f-4c9c-86bd-edf957a7ac59",
        "vpc_id":           "5feb2787-f372-448d-b893-da9393e85a55",
        "backend_group_id": "d551cb7d-c221-4e4c-9337-aaccdc3fcb05",
    },
}

# Selectel
SEL_RESELL_URL = "https://api.selectel.ru/vpc/resell/v2"
SEL_REGIONS    = ["ru-1", "ru-3", "ru-7", "ru-8", "ru-9", "gis-1", "gis-2"]

# Целевые подсети ──────────────────────────────────────────────────────────
TARGET_NETWORKS = {
    "46.243.142.0/23":  "cloud.ru",
    "37.44.196.0/23":   "cloud.ru LB",
    "31.129.42.0/24":   "Selectel",
    "5.188.0.0/16":     "Selectel",
    "185.91.52.0/24":   "Selectel",
    "89.208.0.0/16":    "VK Cloud",
    "217.0.0.0/8":      "VK Cloud",
    "109.120.0.0/16":   "VK Cloud",
    "212.233.0.0/16":   "VK Cloud",
    "213.219.0.0/16":   "VK Cloud",
    "51.0.0.0/8":       "Yandex Cloud",
    "84.204.0.0/16":    "Yandex Cloud",
    "158.160.0.0/16":   "Yandex Cloud",
    "79.174.0.0/16":    "Reg Cloud",
    "178.248.0.0/16":   "Curator Pro",
    "46.8.0.0/16":      "Contell",
}
TARGET_NETS = {ipaddress.ip_network(n, strict=False): label for n, label in TARGET_NETWORKS.items()}
EXCLUDE_NETS = [ipaddress.ip_network("46.16.36.0/24", strict=False)]

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
        print(f"⚠️  Нет прав на {LOG_FILE}, логи только в stdout.")
    return logger

def out(msg: str):
    log.info(msg)

# ── Helpers ───────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    val = input(prompt).strip()
    return val if val else default

def env(key: str, prompt: str, default: str = "") -> str:
    """Читает из env, если нет — спрашивает."""
    val = os.environ.get(key, "").strip()
    if val:
        return val
    return ask(prompt, default)

def check_ip(ip: str):
    addr = ipaddress.ip_address(ip)
    if any(addr in net for net in EXCLUDE_NETS):
        return None
    for net, label in TARGET_NETS.items():
        if addr in net:
            return label
    return None

def cloudru_hdrs(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ── cloud.ru auth ─────────────────────────────────────────────────────────────

def cloudru_get_token(key_id: str, key_secret: str) -> str:
    for attempt in range(5):
        try:
            resp = requests.post(CLOUDRU_AUTH_URL, json={
                "keyId": key_id, "secret": key_secret
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

# ── cloud.ru floating IP ──────────────────────────────────────────────────────

def cloudru_setup() -> dict:
    print("\n── cloud.ru Floating IP ──────────────────────────────────")
    key_id     = env("CLOUDRU_KEY_ID",     "  Key ID: ")
    key_secret = env("CLOUDRU_KEY_SECRET", "  Key Secret: ")
    project_id = env("CLOUDRU_PROJECT_ID", "  Project ID: ")
    if not key_id or not key_secret or not project_id:
        print("❌ Все поля обязательны.")
        sys.exit(1)

    print("\n── cloud.ru AZ (Enter = пропустить) ─────────────────────")
    az_list = []
    for az_name, default_id in CLOUDRU_KNOWN_AZ.items():
        val = env(f"CLOUDRU_AZ_{az_name.replace('.','_')}",
                  f"  {az_name} AZ ID [{default_id}]: ", default_id)
        if val:
            az_list.append((az_name, val))

    if not az_list:
        print("❌ Не задана ни одна зона.")
        sys.exit(1)

    return {"key_id": key_id, "key_secret": key_secret, "project_id": project_id, "az_list": az_list}

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

# ── cloud.ru Load Balancer ────────────────────────────────────────────────────

def lbaas_setup() -> dict:
    print("\n── cloud.ru Load Balancer ────────────────────────────────")
    key_id     = env("CLOUDRU_LB_KEY_ID",     "  Key ID: ")
    key_secret = env("CLOUDRU_LB_KEY_SECRET", "  Key Secret: ")
    project_id = env("CLOUDRU_LB_PROJECT_ID", "  Project ID: ")
    if not key_id or not key_secret or not project_id:
        print("❌ Все поля обязательны.")
        sys.exit(1)

    available = list(LB_AZ_CONFIG.keys())
    az_raw = env("CLOUDRU_LB_AZ", f"  Зоны [{','.join(available)}]: ", ",".join(available))
    az_list = [a.strip() for a in az_raw.split(",") if a.strip() in LB_AZ_CONFIG]

    if not az_list:
        print("❌ Не выбрана ни одна зона.")
        sys.exit(1)

    return {"key_id": key_id, "key_secret": key_secret, "project_id": project_id, "az_list": az_list}

def lbaas_create(token: str, cfg: dict, az: str, attempt: int) -> str:
    az_cfg = LB_AZ_CONFIG[az]
    resp   = requests.post(f"{CLOUDRU_LB_BASE}/balancers", headers=cloudru_hdrs(token), json={
        "name":        f"pick-lb-{attempt:04d}",
        "description": "",
        "projectId":   cfg["project_id"],
        "vpcId":       az_cfg["vpc_id"],
        "availabilityZones": [{"name": az, "subnetIds": []}],
        "internalAddress": {"subnetId": az_cfg["subnet_id"], "allocate": True},
        "externalAddress": {"allocate": True},
        "minReplicasInAz": 1,
        "targetGroups": [{
            "name":           f"Rule-{attempt:04d}",
            "backendGroupId": az_cfg["backend_group_id"],
            "listeners":      [{"port": 8443, "targetPort": 443, "name": f"listener-{attempt:04d}"}],
            "algorithm":      "ALG_ROUND_ROBIN",
            "protocol":       "TCP",
        }],
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    lb_id = data.get("id") or data.get("balancerId")
    if not lb_id:
        raise RuntimeError(f"Нет ID балансировщика: {data}")
    return lb_id

def lbaas_wait_active(token: str, cfg: dict, lb_id: str, timeout: int = 180) -> str:
    url    = f"{CLOUDRU_LB_BASE}/balancers/{lb_id}"
    params = {"projectId": cfg["project_id"]}
    states = []
    for _ in range(timeout // 5):
        try:
            resp = requests.get(url, headers=cloudru_hdrs(token), params=params, timeout=15)
            out(f"   DEBUG GET {resp.url} → {resp.status_code} {resp.text[:300]}")
            if resp.ok:
                data  = resp.json()
                state = data.get("status") or data.get("state") or ""
                if not states or states[-1] != state:
                    states.append(state)
                    print(f" [{state}]", end="", flush=True)
                if state in ("NLB_STATUS_RUNNING", "NLB_STATUS_ACTIVE"):
                    ip = data.get("externalIpv4") or ""
                    if ip:
                        return ip
                if state in ("NLB_STATUS_ERROR", "NLB_STATUS_FAILED"):
                    return ""
        except Exception:
            pass
        time.sleep(5)
    return ""

def lbaas_delete(token: str, cfg: dict, lb_id: str):
    url    = f"{CLOUDRU_LB_BASE}/balancers/{lb_id}"
    params = {"projectId": cfg["project_id"]}
    for attempt in range(5):
        try:
            resp = requests.delete(url, headers=cloudru_hdrs(token), params=params, timeout=30)
            if resp.status_code in (200, 202, 204, 404):
                return
            raise RuntimeError(f"{resp.status_code} {resp.text[:200]}")
        except Exception as e:
            if attempt < 4:
                w = 15 * (attempt + 1)
                out(f"   ↻ retry удаления LB {attempt+1}/5, жду {w}с: {e}")
                time.sleep(w)
            else:
                out(f"   ⚠️  Не удалось удалить LB {lb_id[:8]}…: {e}")

# ── Selectel ──────────────────────────────────────────────────────────────────

def sel_setup() -> dict:
    print("\n── Selectel ──────────────────────────────────────────────")
    token      = env("SEL_TOKEN",      "  API Token: ")
    project_id = env("SEL_PROJECT_ID", "  Project ID: ")
    if not token or not project_id:
        print("❌ Все поля обязательны.")
        sys.exit(1)

    regions_raw = env("SEL_REGIONS", f"  Регионы [{','.join(SEL_REGIONS)}]: ", ",".join(SEL_REGIONS))
    regions = [r.strip() for r in regions_raw.split(",") if r.strip()]

    return {"token": token, "project_id": project_id, "regions": regions}

def sel_allocate(token: str, cfg: dict, region: str, attempt: int) -> tuple:
    url  = f"{SEL_RESELL_URL}/floatingips/projects/{cfg['project_id']}"
    resp = requests.post(url, headers={"X-Token": token, "Content-Type": "application/json"},
                         json={"floatingips": [{"region": region, "quantity": 1}]}, timeout=30)
    resp.raise_for_status()
    fips = resp.json().get("floatingips", [])
    if not fips:
        raise RuntimeError(f"Нет IP в ответе: {resp.text}")
    fip = fips[0]
    return fip["floating_ip_address"], fip["id"]

def sel_release(token: str, fip_id: str):
    url = f"{SEL_RESELL_URL}/floatingips/{fip_id}"
    for attempt in range(5):
        try:
            resp = requests.delete(url, headers={"X-Token": token}, timeout=30)
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

# ── Setup ─────────────────────────────────────────────────────────────────────

def setup_providers() -> list:
    print("╔══════════════════════════════════════════╗")
    print("║     cloudpick — cloud.ru + Selectel      ║")
    print("╚══════════════════════════════════════════╝\n")

    providers = []

    use_cloudru = env("USE_CLOUDRU", "Использовать cloud.ru Floating IP? [Y/n]: ", "y").lower()
    if use_cloudru != "n":
        cfg = cloudru_setup()
        cfg["provider"] = "cloudru"
        providers.append(cfg)

    use_lb = env("USE_CLOUDRU_LB", "\nИспользовать cloud.ru Load Balancer? [y/N]: ", "n").lower()
    if use_lb == "y":
        cfg = lbaas_setup()
        cfg["provider"] = "cloudru_lb"
        providers.append(cfg)

    use_sel = env("USE_SELECTEL", "\nИспользовать Selectel? [Y/n]: ", "y").lower()
    if use_sel != "n":
        cfg = sel_setup()
        cfg["provider"] = "selectel"
        providers.append(cfg)

    if not providers:
        print("❌ Выбери хотя бы одного провайдера.")
        sys.exit(1)

    return providers

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    load_env()
    check_already_running()
    global log
    log = setup_logging()

    providers = setup_providers()

    print("\n── Параметры ─────────────────────────────────────────────")
    print(f"  Целей: {len(TARGET_NETWORKS)} подсетей из целевых подсетей")
    delay_min = int(env("DELAY_MIN", f"  Мин. пауза (сек) [{DEFAULT_DELAY_MIN}]: ", str(DEFAULT_DELAY_MIN)))
    delay_max = int(env("DELAY_MAX", f"  Макс. пауза (сек) [{DEFAULT_DELAY_MAX}]: ", str(DEFAULT_DELAY_MAX)))
    max_att   = int(env("MAX_ATTEMPTS", "  Макс. попыток [1000]: ", "1000"))
    print()

    # Токены
    tokens = {}
    cloudru_cfg = next((p for p in providers if p["provider"] == "cloudru"), None)
    lb_cfg      = next((p for p in providers if p["provider"] == "cloudru_lb"), None)
    sel_cfg     = next((p for p in providers if p["provider"] == "selectel"), None)

    if cloudru_cfg:
        tokens["cloudru"] = cloudru_get_token(cloudru_cfg["key_id"], cloudru_cfg["key_secret"])
    if lb_cfg:
        tokens["cloudru_lb"] = cloudru_get_token(lb_cfg["key_id"], lb_cfg["key_secret"])
    if sel_cfg:
        tokens["selectel"] = sel_cfg["token"]

    # Очередь задач
    tasks = []
    for p in providers:
        if p["provider"] == "cloudru":
            for az_name, az_id in p["az_list"]:
                tasks.append((p, az_name, az_id))
        elif p["provider"] == "cloudru_lb":
            for az in p["az_list"]:
                tasks.append((p, az, az))
        elif p["provider"] == "selectel":
            for region in p["regions"]:
                tasks.append((p, region, region))

    out(f"{'='*55}")
    out(f"cloudpick запущен")
    out(f"Целей: {len(TARGET_NETWORKS)} целевых подсетей")
    out(f"Провайдеры: {', '.join(p['provider'] for p in providers)}")
    out(f"Задач: {len(tasks)} | Макс. попыток: {max_att}")
    out(f"{'='*55}")

    found            = []
    task_idx         = 0
    consecutive_429  = 0

    for attempt in range(1, max_att + 1):
        if attempt > 1 and attempt % 10 == 0:
            if cloudru_cfg:
                tokens["cloudru"] = cloudru_get_token(cloudru_cfg["key_id"], cloudru_cfg["key_secret"])
            if lb_cfg:
                tokens["cloudru_lb"] = cloudru_get_token(lb_cfg["key_id"], lb_cfg["key_secret"])
            out("🔄 Токены обновлены")

        p_cfg, zone_name, zone_id = tasks[task_idx % len(tasks)]
        task_idx += 1
        provider = p_cfg["provider"]
        token    = tokens[provider]
        label    = f"{provider}:{zone_name}"

        try:
            # ── Load Balancer ──
            if provider == "cloudru_lb":
                print(f"         [{attempt:04d}] LB в {zone_name}…", end=" ", flush=True)
                lb_id = lbaas_create(token, p_cfg, zone_name, attempt)
                print(f" ожидаем…", end=" ", flush=True)
                ip = lbaas_wait_active(token, p_cfg, lb_id)
                print()

                if not ip:
                    out(f"[{attempt:04d}] cloudru_lb:{zone_name} — не удалось получить IP, удаляем LB")
                    lbaas_delete(token, p_cfg, lb_id)
                    time.sleep(random.uniform(60, 90))
                    continue

                matched = check_ip(ip)
                status  = f"🎉 ПОПАЛИ! ({matched})" if matched else "❌ Мимо"
                out(f"[{attempt:04d}] {label:<20} │ {ip:<18} {lb_id[:8]}… — {status}")

                if matched:
                    found.append((ip, lb_id, label, matched))
                    out(f"✅ IP найден через LB: {ip}  Провайдер: {matched}  ID: {lb_id}")
                    again = ask("\nИскать ещё? [y/N]: ", "n").lower()
                    if again != "y":
                        break
                else:
                    lbaas_delete(token, p_cfg, lb_id)
                    delay = random.uniform(60, 90)
                    out(f"         пауза {delay:.0f}с…")
                    time.sleep(delay)
                continue

            # ── Floating IP ──
            if provider == "cloudru":
                ip, fip_id = cloudru_allocate(token, p_cfg, zone_id, attempt)
            else:
                ip, fip_id = sel_allocate(token, p_cfg, zone_name, attempt)

            matched = check_ip(ip)
            status  = f"🎉 ПОПАЛИ! ({matched})" if matched else "❌ Мимо"
            consecutive_429 = 0
            out(f"[{attempt:04d}] {label:<20} │ {ip:<18} {fip_id[:8]}… — {status}")

            if matched:
                found.append((ip, fip_id, label, matched))
                out(f"✅ IP найден: {ip}  Провайдер: {matched}  ID: {fip_id}")
                again = ask("\nИскать ещё? [y/N]: ", "n").lower()
                if again != "y":
                    break
                continue

            if provider == "cloudru":
                print("         ожидаем active…", end=" ", flush=True)
                ok = cloudru_wait_active(token, p_cfg, fip_id)
                print()
                cloudru_release(token, p_cfg, fip_id)
            else:
                time.sleep(3)
                sel_release(token, fip_id)

        except requests.HTTPError as e:
            code = e.response.status_code
            out(f"[{attempt:04d}] HTTP {code}: {e.response.text[:150]}")
            if code == 429:
                consecutive_429 += 1
                wait = min(45 * consecutive_429, 300)
            else:
                consecutive_429 = 0
                wait = random.uniform(delay_min, delay_max)
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
