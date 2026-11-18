import base64
import json
import os
import socket
import subprocess
import time
import urllib.parse
from curl_cffi import requests

XUI_URL = os.getenv("XUI_URL", "").strip().rstrip("/")
XUI_USER = os.getenv("XUI_USER", "").strip()
XUI_PASS = os.getenv("XUI_PASS", "").strip()

STATE_FILE = "active_slots_state.json"
OUTPUT_SUB_FILE = "sub.txt"
XRAY_BIN = "./xray"

SWISS_HOST = "ch5.h1cloud.net"
BRIDGE_PORTS = [25384, 25385, 25386, 25397]

PRIORITY_SLOTS = [
    ("KZ", "🇰🇿 Казахстан • Almaty [Bridge]"),
    ("DE", "🇩🇪 Германия • Frankfurt [Bridge]"),
    ("NL", "🇳🇱 Нидерланды • Amsterdam [Bridge]"),
    ("US", "🇺🇸 США • Virginia [Bridge]"),
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


def start_local_proxy(node: dict):
    if not os.path.exists(XRAY_BIN) or not node:
        return None, None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    cfg = {
        "log": {"loglevel": "none"},
        "inbounds": [{"port": port, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": False}}],
        "outbounds": [uri_to_outbound(node["url"], "proxy")],
    }
    cfg_path = f"/tmp/bridge_proxy_{port}.json"
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)

    proc = subprocess.Popen([XRAY_BIN, "run", "-c", cfg_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    return proc, f"socks5h://127.0.0.1:{port}"


def try_login(url: str, proxy_url: str | None = None):
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    sess = requests.Session(impersonate="chrome", verify=False, proxies=proxies)
    r_get = sess.get(f"{url}/", timeout=15)
    print(f"[Bridge] GET {url}/ -> {r_get.status_code} (server={r_get.headers.get('server', '?')})")
    resp = sess.post(f"{url}/login", data={"username": XUI_USER, "password": XUI_PASS}, timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        if data.get("success"):
            return sess
    print(f"[Bridge] POST {url}/login -> {resp.status_code}: {resp.text[:150]}")
    return None


def main():
    if not all([XUI_URL, XUI_USER, XUI_PASS]):
        print("[Bridge] Секреты XUI_URL / XUI_USER / XUI_PASS не заданы — пропускаем.")
        return

    if not os.path.exists(STATE_FILE):
        print("[Bridge] Нет файла active_slots_state.json.")
        return

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        slots = json.load(f)

    parsed = urllib.parse.urlparse(XUI_URL)
    base_path = parsed.path.rstrip("/")
    candidate_urls = [XUI_URL]
    if "s-25373." in parsed.netloc:
        candidate_urls.append(f"http://{SWISS_HOST}:25373{base_path}")

    sess = None
    active_url = None
    xray_proc = None

    # 1. Пробуем прямой вход
    for url in candidate_urls:
        try:
            sess = try_login(url)
            if sess:
                active_url = url
                break
        except Exception as e:
            print(f"[Bridge] Ошибка прямого входа на {url}: {e}")

    # 2. Если H1Cloud отбил по GeoIP (403) — заходим через российский узел из _ru_proxies
    if not sess:
        ru_list = slots.get("_ru_proxies", [])
        print(f"[Bridge] Прямой IP не прошёл — пробуем вход через RU-шлюзы ({len(ru_list)} шт.)...")
        for idx, ru_node in enumerate(ru_list, 1):
            proc, proxy_url = start_local_proxy(ru_node)
            if not proxy_url:
                continue
            for url in candidate_urls:
                try:
                    sess = try_login(url, proxy_url=proxy_url)
                    if sess:
                        active_url = url
                        xray_proc = proc
                        print(f"[Bridge] Пробили вход через RU-шлюз #{idx}!")
                        break
                except Exception as e:
                    print(f"[Bridge] RU-шлюз #{idx} обрыв на {url}: {e}")
            if sess:
                break
            proc.kill()

    try:
        if not sess or not active_url:
            raise RuntimeError("Не удалось войти в 3X-UI ни напрямую, ни через RU-шлюзы.")

        print(f"[Bridge] Успешный вход в 3X-UI через {active_url}!")

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
    finally:
        if xray_proc:
            xray_proc.kill()


if __name__ == "__main__":
    main()
