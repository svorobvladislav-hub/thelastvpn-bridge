import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import urllib.parse
from curl_cffi import requests

XUI_URL = os.getenv("XUI_URL", "").strip().rstrip("/")
XUI_USER = os.getenv("XUI_USER", "").strip()
XUI_PASS = os.getenv("XUI_PASS", "").strip()

STATE_FILE = "active_slots_state.json"
OUTPUT_SUB_FILE = "sub.txt"

SWISS_HOST = "ch5.h1cloud.net"
BRIDGE_PORTS = [25384, 25385, 25386, 25397]

PRIORITY_SLOTS = [
    ("KZ", "🇰🇿 Казахстан • Almaty [Bridge]"),
    ("DE", "🇩🇪 Германия • Frankfurt [Bridge]"),
    ("NL", "🇳🇱 Нидерланды • Amsterdam [Bridge]"),
    ("US", "🇺🇸 США • Virginia [Bridge]"),
]

RU_PROXY_SOURCES = [
    ("socks5", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/RU/data.txt"),
    ("http", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"),
    ("socks5", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("http", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"),
]


def uri_to_outbound(uri: str, tag: str) -> dict:
    p = urllib.parse.urlparse(uri)
    qs = urllib.parse.parse_qs(p.query)
    net = qs.get("type", ["tcp"])[0]
    stream = {
        "network": net,
        "security": "reality",
        "realitySettings": {
            "serverName": qs.get("sni", [""])[0],
            "fingerprint": qs.get("fp", ["chrome"])[0],
            "publicKey": qs.get("pbk", [""])[0],
            "shortId": qs.get("sid", [""])[0],
            "spiderX": urllib.parse.unquote(qs.get("spx", ["/"])[0]),
        },
    }
    if net == "grpc":
        stream["grpcSettings"] = {"serviceName": qs.get("serviceName", [""])[0]}

    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": p.hostname,
                "port": p.port or 443,
                "users": [{
                    "id": p.username,
                    "encryption": "none",
                    "flow": qs.get("flow", [""])[0],
                }],
            }]
        },
        "streamSettings": stream,
    }


def fetch_ru_public_proxies() -> list[str]:
    direct = requests.Session(impersonate="chrome")
    ru_list = []
    other_list = []

    for proto, url in RU_PROXY_SOURCES:
        try:
            r = direct.get(url, timeout=8)
            if r.status_code != 200:
                continue
            is_ru_dedicated = "/countries/RU/" in url
            for line in r.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "://" in line:
                    p_url = line
                elif re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", line):
                    p_url = f"{proto}://{line}"
                else:
                    continue
                if is_ru_dedicated:
                    ru_list.append(p_url)
                else:
                    other_list.append(p_url)
        except Exception:
            pass

    seen = set()
    result = []
    for p in ru_list + other_list[:250]:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result[:350]


def try_login(url: str, proxy_url: str | None = None, silent: bool = False):
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    sess = requests.Session(impersonate="chrome", verify=False, proxies=proxies)

    r_get = sess.get(f"{url}/", timeout=8)
    final_base = str(r_get.url).rstrip("/")
    parsed_final = urllib.parse.urlparse(final_base)
    origin = f"{parsed_final.scheme}://{parsed_final.netloc}"

    ajax_headers = {
        "Origin": origin,
        "Referer": f"{final_base}/",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }

    resp = sess.post(
        f"{final_base}/login",
        data={"username": XUI_USER, "password": XUI_PASS},
        headers=ajax_headers,
        timeout=8,
    )
    if resp.status_code == 200:
        try:
            if resp.json().get("success"):
                return sess, final_base
        except Exception:
            pass

    if not silent:
        print(f"[Bridge] Отказ POST ({resp.status_code}) на {final_base}/login | headers={dict(resp.headers)}")
    return None, None


def main():
    if not all([XUI_URL, XUI_USER, XUI_PASS]):
        print("[Bridge] Секреты XUI_URL / XUI_USER / XUI_PASS не заданы — пропускаем.")
        return

    if not os.path.exists(STATE_FILE):
        print("[Bridge] Нет файла active_slots_state.json.")
        return

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        slots = json.load(f)

    sess = None
    active_url = None

    # 1. Пробуем прямой вход по рабочей HTTPS-ссылке панели
    try:
        sess, active_url = try_login(XUI_URL)
    except Exception as e:
        print(f"[Bridge] Ошибка прямого входа: {e}")

    # 2. Если сервер режет зарубежные IP — перебираем RU SOCKS5/HTTP прокси в 35 потоков
    if not sess:
        proxies_pool = fetch_ru_public_proxies()
        print(f"[Bridge] Прямой IP получил 403 — проверяем {len(proxies_pool)} RU-прокси для входа...")

        def probe_proxy(px: str):
            try:
                s, u = try_login(XUI_URL, proxy_url=px, silent=True)
                if s:
                    return s, u, px
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=35) as pool:
            futures = [pool.submit(probe_proxy, px) for px in proxies_pool]
            for fut in as_completed(futures):
                res = fut.result()
                if res:
                    sess, active_url, win_px = res
                    print(f"[Bridge] Пробили вход через российский прокси: {win_px}!")
                    for f_rem in futures:
                        f_rem.cancel()
                    break

    if not sess or not active_url:
        raise RuntimeError("Не удалось войти в 3X-UI ни напрямую, ни через RU-прокси.")

    print(f"[Bridge] Успешный вход в 3X-UI!")
    parsed_active = urllib.parse.urlparse(active_url)
    sess.headers.update({
        "Origin": f"{parsed_active.scheme}://{parsed_active.netloc}",
        "Referer": f"{active_url}/panel/",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    })

    inbounds_resp = sess.get(f"{active_url}/panel/api/inbounds/list", timeout=15).json()
    inbounds = inbounds_resp.get("obj", [])

    base_reality = None
    base_client = None
    existing_by_port = {}

    for ib in inbounds:
        existing_by_port[ib["port"]] = ib
        remark = ib.get("remark", "")
        stream = json.loads(ib.get("streamSettings", "{}"))
        if stream.get("security") == "reality" and stream.get("network") == "tcp" and not remark.startswith("Bridge-"):
            base_reality = stream
            clients = json.loads(ib.get("settings", "{}")).get("clients", [])
            if clients:
                base_client = clients[0]

    if not base_reality or not base_client:
        raise RuntimeError("Не найден базовый VLESS TCP Reality inbound в панели.")

    reality_cfg = base_reality["realitySettings"]
    inner_settings = reality_cfg.get("settings", {})
    pbk = inner_settings.get("publicKey") or reality_cfg.get("publicKey", "")
    sni = (reality_cfg.get("serverNames") or [""])[0]
    sid = (reality_cfg.get("shortIds") or [""])[0]
    spx = inner_settings.get("spiderX", "/")
    uuid = base_client["id"]
    flow = base_client.get("flow", "xtls-rprx-vision")

    bridge_links = []
    new_outbounds = []
    new_rules = []

    available_slots = [(c, t) for c, t in PRIORITY_SLOTS if c in slots]
    for port, (code, title) in zip(BRIDGE_PORTS, available_slots):
        in_tag = f"inbound-{port}"
        out_tag = f"bridge-out-{code}"

        if port not in existing_by_port:
            payload = {
                "up": 0,
                "down": 0,
                "total": 0,
                "remark": f"Bridge-{code}",
                "enable": True,
                "expiryTime": 0,
                "listen": "",
                "port": port,
                "protocol": "vless",
                "settings": json.dumps({
                    "clients": [{"id": uuid, "flow": flow, "email": f"bridge_{code.lower()}", "enable": True}],
                    "decryption": "none",
                    "fallbacks": [],
                }),
                "streamSettings": json.dumps(base_reality),
                "sniffing": json.dumps({"enabled": True, "destOverride": ["http", "tls", "quic"]}),
            }
            add_res = sess.post(f"{active_url}/panel/api/inbounds/add", data=payload, timeout=15).json()
            if add_res.get("success"):
                print(f"  [+] Создан мост {code} на порту {port}")
            else:
                print(f"  [!] Ошибка создания порта {port}: {add_res}")

        new_outbounds.append(uri_to_outbound(slots[code]["url"], out_tag))
        new_rules.append({
            "type": "field",
            "inboundTag": [in_tag],
            "outboundTag": out_tag,
        })

        params = urllib.parse.urlencode({
            "type": "tcp",
            "security": "reality",
            "pbk": pbk,
            "fp": "firefox",
            "sni": sni,
            "sid": sid,
            "spx": spx,
            "flow": flow,
        })
        bridge_links.append(f"vless://{uuid}@{SWISS_HOST}:{port}?{params}#{urllib.parse.quote(title)}")

    xray_resp = sess.get(f"{active_url}/panel/xray/", timeout=15).json()
    xray_cfg = json.loads(xray_resp["obj"]["xraySetting"])

    clean_outbounds = [o for o in xray_cfg.get("outbounds", []) if not o.get("tag", "").startswith("bridge-out-")]
    xray_cfg["outbounds"] = clean_outbounds + new_outbounds

    routing = xray_cfg.setdefault("routing", {"domainStrategy": "AsIs", "rules": []})
    clean_rules = [r for r in routing.get("rules", []) if not r.get("outboundTag", "").startswith("bridge-out-")]
    routing["rules"] = new_rules + clean_rules

    upd_res = sess.post(
        f"{active_url}/panel/xray/update",
        data={"xraySetting": json.dumps(xray_cfg, indent=2)},
        timeout=15,
    ).json()
    if not upd_res.get("success"):
        raise RuntimeError(f"Ошибка обновления Xray: {upd_res}")

    sess.post(f"{active_url}/panel/api/server/restartXrayService", timeout=15)
    print(f"[Bridge] Xray перезапущен! Активно мостов: {len(new_outbounds)}")

    if bridge_links:
        encoded = base64.b64encode("\n".join(bridge_links).encode("utf-8")).decode("utf-8")
        with open(OUTPUT_SUB_FILE, "w", encoding="utf-8") as f:
            f.write(encoded)
        print(f"[Bridge] В sub.txt записано {len(bridge_links)} рабочих мостов!")


if __name__ == "__main__":
    main()
