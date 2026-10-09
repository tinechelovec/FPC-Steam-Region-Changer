from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import shutil
import threading
import time
import requests
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping, Optional

import aiohttp
from aiohttp import ClientResponse, ClientSession

import html
try:
    from colorama import Fore, Style
except ImportError:
    class _DummyColor:
        def __getattr__(self, name):
            return ""
    Fore = _DummyColor()
    Style = _DummyColor()

logger = logging.getLogger("FPC.SteamRegionChanger")
LP = "[Steam Region Changer]"

def _mask_proxy(p: str) -> str:

    s = str(p or "")
    s = re.sub(r':([^@:]+)@', ':***@', s)
    parts = s.split(":")
    if len(parts) == 4 and not parts[0].startswith("http"):
        if parts[1].isdigit():
            return f"{parts[0]}:{parts[1]}:{parts[2]}:***"
        elif parts[3].isdigit():
            return f"{parts[0]}:***:{parts[2]}:{parts[3]}"
    return s

def _log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    tag = f"{Fore.CYAN}{LP}{Style.RESET_ALL}"
    ev_str = f"{Fore.YELLOW}event={event}{Style.RESET_ALL}"
    parts = [tag, ev_str]
    for k, v in fields.items():
        if v is None:
            continue
        v_str = str(v).replace('\r', ' ').replace('\n', ' ')
        if any(token in k.lower() for token in ('password', 'secret', 'token', 'code')):
            v_str = '***'
        elif k in ('proxy', 'proxy_url'):
            v_str = _mask_proxy(v_str)

        if k in ('status', 'result'):
            if str(v).lower() in ('ok', 'success', 'alive', '200', 'fixed'):
                val_colored = f"{Fore.GREEN}{v_str}{Style.RESET_ALL}"
            else:
                val_colored = f"{Fore.RED}{v_str}{Style.RESET_ALL}"
        elif any(w in k.lower() for w in ('err', 'fail', 'reason', 'error')):
            val_colored = f"{Fore.RED}{v_str}{Style.RESET_ALL}"
        elif k in ('ms', 'latency', 'latency_ms', 'ping'):
            val_colored = f"{Fore.MAGENTA}{v_str}{Style.RESET_ALL}"
        else:
            val_colored = f"{Fore.WHITE}{v_str}{Style.RESET_ALL}"
        parts.append(f"{Fore.LIGHTBLUE_EX}{k}={Style.RESET_ALL}{val_colored}")

    logger.log(level, " ".join(parts))

try:
    from tg_bot import CBT as _CBT
except Exception:
    _CBT = None
try:
    from telebot import types as tg_types
except Exception:
    tg_types = None

NAME = "Steam Region Changer"
VERSION = "1.0.0"
DESCRIPTION = "Смена региона Steam (Steam Region Changer)"
CREDITS = "@tinechelovec"
UUID = "001ab503-775a-41c2-8b96-4207daaf33a7"
SETTINGS_PAGE = True

_CBT_PLUGIN_SETTINGS = getattr(_CBT, 'PLUGIN_SETTINGS', None) if _CBT else None
CBT_SETTINGS = f"{_CBT_PLUGIN_SETTINGS}:{UUID}:0" if _CBT_PLUGIN_SETTINGS is not None else ""
CB_PLUGINS_LIST_OPEN = f"{getattr(_CBT, 'PLUGINS_LIST', '44')}:0" if _CBT else "44:0"

CREATOR_URL = "https://t.me/tinechelovec"
GROUP_URL = "https://t.me/dev_thc_chat"
CHANNEL_URL = "https://t.me/by_thc"
GITHUB_URL = "https://github.com/tinechelovec/FPC-Steam-Region-Changer"
GITHUB_UPDATE_URL = os.getenv("SRC_PLUGIN_UPDATE_URL", "https://raw.githubusercontent.com/tinechelovec/FPC-Steam-Region-Changer/main/SRC-Plugin.py").strip()
INSTRUCTION_URL = "https://teletype.in/@tinechelovec/Steam-Region-Changer"
ALT_INSTRUCTION_URL = "https://github.com/tinechelovec/FPC-Steam-Region-Changer/blob/main/instructions.md"

REGION_REQUEST_TIMEOUT = float(os.getenv("REGION_REQUEST_TIMEOUT", "30"))
REGION_CONNECT_TIMEOUT = float(os.getenv("REGION_CONNECT_TIMEOUT", "15"))

REGION_WAIT_INTERVAL = 1.5
REGION_WAIT_TRIES = 3

REGION_MAX_WORKERS = 10

GuardProvider = Callable[[str], Awaitable[str]]

QRDisplay = Callable[[str], Awaitable[None]]

URL_SET_COUNTRY = "https://store.steampowered.com/country/setcountry"
URL_SET_COUNTRY_CHK = "https://checkout.steampowered.com/country/setcountry"
URL_REDEEM = "https://store.steampowered.com/account/ajaxredeemwalletcode/"
URL_ADDFUNDS = "https://store.steampowered.com/steamaccount/addfunds"
URL_STORE = "https://store.steampowered.com/"
URL_REDEEM_GIFT = "https://store.steampowered.com/account/ajaxredeemwalletcode/"
URL_ACCOUNT_HISTORY = "https://store.steampowered.com/account/history/"
URL_ACCOUNT = "https://store.steampowered.com/account/"

WALLET_FIXED_RE = re.compile(
    r'id="header_wallet_balance"'
    r'|"wallet_currency"\s*:\s*[1-9]'
    r'|"has_wallet"\s*:\s*true'
    r'|class="[^"]*wallet_header[^"]*"'
    r'|wallet_balance_currency',
    re.IGNORECASE,
)

RATE_LIMIT_DELAY = 30

MAX_PROXY_ATTEMPTS_FLOOR = 10

GIFT_MAX_PROXY_ATTEMPTS = 3

_RETRYABLE_HTTP = {503, 502, 504, 429, 520, 521, 522, 523, 524}

def format_eta(seconds: float) -> str:

    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} сек"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} мин" + (f" {sec} сек" if sec else "")
    h, m = divmod(m, 60)
    return f"{h} ч" + (f" {m} мин" if m else "")

def _is_network_error(exc: BaseException) -> bool:

    if isinstance(exc, (
        asyncio.TimeoutError,
        aiohttp.ServerConnectionError,
        aiohttp.ClientConnectorError,
        aiohttp.ClientOSError,
        aiohttp.ServerDisconnectedError,
        aiohttp.ClientPayloadError,
        ConnectionResetError,
        OSError,
    )):
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        "503", "502", "504", "429",
        "proxy", "tunnel", "connection", "timeout",
        "reset by peer", "broken pipe", "eof",
    ))

async def _net(coro_fn, *args, label: str = "", **kwargs):

    result = await coro_fn(*args, **kwargs)
    if hasattr(result, "status") and result.status in _RETRYABLE_HTTP:
        raise aiohttp.ClientResponseError(
            request_info=result.request_info,
            history=(),
            status=result.status,
            message=f"HTTP {result.status}",
        )
    return result

COUNTRY_NAMES: dict[str, str] = {
    "RU": "🇷🇺 Россия",
    "KZ": "🇰🇿 Казахстан",
    "UA": "🇺🇦 Украина",
    "GB": "🇬🇧 Великобритания",
    "DE": "🇩🇪 Германия",
    "CH": "🇨🇭 Швейцария",
    "PL": "🇵🇱 Польша",
    "SE": "🇸🇪 Швеция",
    "BR": "🇧🇷 Бразилия",
    "JP": "🇯🇵 Япония",
    "ID": "🇮🇩 Индонезия",
    "MY": "🇲🇾 Малайзия",
    "PH": "🇵🇭 Филиппины",
    "SG": "🇸🇬 Сингапур",
    "TH": "🇹🇭 Таиланд",
    "VN": "🇻🇳 Вьетнам",
    "KR": "🇰🇷 Ю. Корея",
    "TR": "🇹🇷 Турция",
    "MX": "🇲🇽 Мексика",
    "NZ": "🇳🇿 Нов. Зеландия",
    "AR": "🇦🇷 Аргентина",
    "CL": "🇨🇱 Чили",
    "CO": "🇨🇴 Колумбия",
    "PE": "🇵🇪 Перу",
    "ZA": "🇿🇦 ЮАР",
    "HK": "🇭🇰 Гонконг",
    "TW": "🇹🇼 Тайвань",
    "SA": "🇸🇦 Саудовская Аравия",
    "IL": "🇮🇱 Израиль",
    "KW": "🇰🇼 Кувейт",
    "QA": "🇶🇦 Катар",
    "CR": "🇨🇷 Коста-Рика",
    "UY": "🇺🇾 Уругвай",
    "BY": "🇧🇾 Беларусь",
    "AZ": "🇦🇿 Азербайджан",
    "GE": "🇬🇪 Грузия",
}

class RegionResult:
    OK = "OK"
    ALREADY = "ALREADY"
    SENT = "SENT"
    SKIP_FIXED = "SKIP_FIXED"
    FAIL_WRONG_REGION = "FAIL_WRONG_REGION"
    FAIL_NO_MAFILE = "FAIL_NO_MAFILE"
    FAIL_WRONG_PASS = "FAIL_WRONG_PASS"
    FAIL_WRONG_GUARD = "FAIL_WRONG_GUARD"
    FAIL_RATE_LIMIT = "FAIL_RATE_LIMIT"
    FAIL_SESSION = "FAIL_SESSION"
    FAIL_CHANGE = "FAIL_CHANGE"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"

@dataclass
class AccountRegionResult:
    login: str
    status: str
    new_region: str | None = None
    error: str | None = None
    gift_results: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.status in (RegionResult.OK, RegionResult.ALREADY)

    @property
    def is_success(self) -> bool:
        return self.success

    @property
    def gift_redeemed(self) -> bool:
        return any(ok for _, ok, _ in self.gift_results)

    @property
    def skipped(self) -> bool:
        return self.status == RegionResult.SKIP_FIXED

    @property
    def critical_wrong_region(self) -> bool:
        return self.status == RegionResult.FAIL_WRONG_REGION

@dataclass
class BatchRegionResult:
    country_code: str
    total: int
    results: list[AccountRegionResult] = field(default_factory=list)

    @property
    def ok(self) -> list[AccountRegionResult]:
        return [r for r in self.results if r.success]

    @property
    def skipped(self) -> list[AccountRegionResult]:
        return [r for r in self.results if r.skipped]

    @property
    def wrong_region(self) -> list[AccountRegionResult]:
        return [r for r in self.results if r.critical_wrong_region]

    @property
    def failed(self) -> list[AccountRegionResult]:
        return [r for r in self.results
                if not r.success and not r.skipped and not r.critical_wrong_region
                and r.status not in (RegionResult.SKIPPED, RegionResult.SENT)]

    @property
    def sent(self) -> list[AccountRegionResult]:
        return [r for r in self.results if r.status == RegionResult.SENT]

    @property
    def user_skipped(self) -> list[AccountRegionResult]:
        return [r for r in self.results if r.status == RegionResult.SKIPPED]

    @property
    def gift_ok(self) -> list[tuple[str, str]]:
        return [(r.login, code) for r in self.results
                for (code, ok, _msg) in r.gift_results if ok]

    @property
    def gift_failed(self) -> list[tuple[str, str, str]]:
        return [(r.login, code, msg) for r in self.results
                for (code, ok, msg) in r.gift_results if not ok]

def normalize_proxy_url(p: str) -> str:

    p = (p or "").strip()
    if not p:
        return ""

    scheme = "http"
    if "://" in p:
        scheme, rest = p.split("://", 1)
        scheme = scheme.lower()
    else:
        rest = p

    if "@" in rest:
        left, right = rest.split("@", 1)
        left_parts = left.split(":")
        right_parts = right.split(":")
        left_has_port = len(left_parts) == 2 and left_parts[1].isdigit()
        right_has_port = len(right_parts) == 2 and right_parts[1].isdigit()

        if left_has_port and not right_has_port:
            return f"{scheme}://{right}@{left}"
        return f"{scheme}://{rest}"

    parts = rest.split(":")
    if len(parts) == 4:
        if parts[1].isdigit() and not parts[3].isdigit():

            host, port, user, pwd = parts
            return f"{scheme}://{user}:{pwd}@{host}:{port}"
        elif parts[3].isdigit() and not parts[1].isdigit():

            user, pwd, host, port = parts
            return f"{scheme}://{user}:{pwd}@{host}:{port}"
        else:
            host, port, user, pwd = parts
            return f"{scheme}://{user}:{pwd}@{host}:{port}"
    elif len(parts) == 2:
        return f"{scheme}://{parts[0]}:{parts[1]}"
    return f"{scheme}://{rest}"

class ProxyRequestStrategy:

    def __init__(self, proxy: str | None = None):
        self._proxy = normalize_proxy_url(proxy) if proxy else None
        self._session: ClientSession | None = None

    def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(
                    total=REGION_REQUEST_TIMEOUT,
                    connect=REGION_CONNECT_TIMEOUT,
                    sock_connect=REGION_CONNECT_TIMEOUT,
                ),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(self, url: str, method: str = "GET", **kwargs: Any) -> ClientResponse:
        session = self._get_session()
        if self._proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self._proxy
        logger.debug(f"[{NAME}] [HTTP {method}] {url} через {_mask_proxy(self._proxy or 'прямое соединение')}")
        response = await session.request(method, url, **kwargs)
        return response

    def cookies(self, domain: str = "steamcommunity.com") -> Mapping[str, str]:
        session = self._get_session()
        result = {}
        for cookie in session.cookie_jar:
            if cookie["domain"] == domain:
                result[cookie.key] = cookie.value
        return result

    async def text(self, url: str, method: str = "GET", **kwargs: Any) -> str:
        resp = await self.request(url, method, **kwargs)
        return await resp.text()

    async def bytes(self, url: str, method: str = "GET", **kwargs: Any) -> bytes:
        resp = await self.request(url, method, **kwargs)
        return await resp.read()

def parse_store_page(html: str) -> dict:
    country_code = None
    wallet_currency = None
    wallet_fixed = bool(WALLET_FIXED_RE.search(html))
    m = re.search(r'data-userinfo="([^"]+)"', html)
    if m:
        try:
            raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
            data = json.loads(raw)
            country_code = data.get("country_code")
            wallet_currency = data.get("wallet_currency")
        except Exception:
            pass
    return {
        "country_code": country_code,
        "wallet_fixed": wallet_fixed,
        "wallet_currency": wallet_currency,
    }

def get_shared_secret_from_mafile(mafile_path: str | None) -> str | None:

    if not mafile_path:
        return None
    try:
        with open(mafile_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("shared_secret") or None
    except Exception:
        return None

def find_shared_secret_in_dir(login: str, mafiles_dir: str) -> str | None:

    p = Path(mafiles_dir)
    if not p.is_dir():
        return None
    login_lower = login.lower()
    for f in p.iterdir():
        if f.suffix.lower() not in (".mafile", ".json"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        acc_name = (data.get("account_name") or "").lower()
        stem = f.stem.lower()
        if acc_name == login_lower or stem == login_lower or \
           (acc_name and (login_lower.startswith(acc_name) or acc_name.startswith(login_lower))):
            return data.get("shared_secret") or None
    return None

async def _try_import_pysteamauth():
    try:
        from pysteamauth.auth import Steam
        return Steam
    except ImportError:
        return None

class GuardCodeSkipped(Exception):
    pass

class GuardCodeRejected(Exception):
    pass

_ManualSteamClass = None

async def _try_import_manual_steam():

    global _ManualSteamClass
    if _ManualSteamClass is not None:
        return _ManualSteamClass
    try:
        from pysteamauth.auth import Steam
        from pysteamauth.pb2.steammessages_auth.steamclient_pb2 import EAuthSessionGuardType
        from urllib3.util import parse_url
    except ImportError:
        return None

    class ManualGuardSteam(Steam):
        _manual_code: str = ""
        _guard_provider: GuardProvider | None = None

        def set_guard_code(self, code: str) -> None:
            self._manual_code = (code or "").strip().upper()

        def set_guard_provider(self, provider: GuardProvider | None) -> None:
            self._guard_provider = provider

        async def login_to_steam(self) -> None:

            if await self.is_authorized():
                return
            if not self._requests.cookies().get("sessionid"):
                await self._requests.bytes(method="GET", url="https://steamcommunity.com")
            keys = await self._getrsakey()
            encrypted_password = self._encrypt_password(keys)
            auth_session = await self._begin_auth_session(
                encrypted_password=encrypted_password,
                rsa_timestamp=keys.timestamp,
            )
            if auth_session.allowed_confirmations:
                if self._is_twofactor_required(auth_session.allowed_confirmations[0]):

                    if self._guard_provider is not None:
                        code = (await self._guard_provider(self._login) or "").strip().upper()
                    else:
                        code = self._manual_code
                    if not code:
                        raise GuardCodeSkipped()
                    await self._update_auth_session(
                        client_id=auth_session.client_id,
                        steamid=auth_session.steamid,
                        code=code,
                        code_type=EAuthSessionGuardType.k_EAuthSessionGuardType_DeviceCode,
                    )
            session = await self._poll_auth_session_status(
                client_id=auth_session.client_id,
                request_id=auth_session.request_id,
            )
            if not session.refresh_token:

                raise GuardCodeRejected("Guard-код не принят Steam (неверный или просроченный)")
            tokens = await self._finalize_login(
                refresh_token=session.refresh_token,
                sessionid=self._requests.cookies()["sessionid"],
            )
            for token in tokens.transfer_info:
                await self._set_token(
                    url=token.url,
                    nonce=token.params.nonce,
                    auth=token.params.auth,
                    steamid=auth_session.steamid,
                )
            if not self._steamid and tokens.steamID:
                self._steamid = int(tokens.steamID)
            cookies = {"steamcommunity.com": self._requests.cookies("steamcommunity.com")}
            for url in ("https://store.steampowered.com", "https://help.steampowered.com"):
                await self._requests.bytes(url, "GET")
                cookies.update({parse_url(url).host: self._requests.cookies(parse_url(url).host)})
            await self._storage.set(login=self._login, cookies=cookies)

    _ManualSteamClass = ManualGuardSteam
    return _ManualSteamClass

class QRLoginTimeout(Exception):
    pass

_QRSteamClass = None

async def _try_import_qr_steam():

    global _QRSteamClass
    if _QRSteamClass is not None:
        return _QRSteamClass
    try:
        from aiohttp import FormData
        from pysteamauth.auth import Steam
        from pysteamauth.pb2.steammessages_auth.steamclient_pb2 import (
            CAuthentication_BeginAuthSessionViaQR_Request,
            CAuthentication_BeginAuthSessionViaQR_Response,
            EAuthTokenPlatformType,
        )
        from urllib3.util import parse_url
    except ImportError:
        return None

    class QRSteam(Steam):
        account_name: str = ""

        async def _begin_qr(self):
            message = CAuthentication_BeginAuthSessionViaQR_Request(
                device_friendly_name="Mozilla/5.0 (X11; Linux x86_64; rv:1.9.5.20) "
                                     "Gecko/2812-12-10 04:56:28 Firefox/3.8",
                platform_type=EAuthTokenPlatformType.k_EAuthTokenPlatformType_WebBrowser,
            )
            response = await self._requests.bytes(
                method="POST",
                url="https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaQR/v1",
                data=FormData(fields=[
                    ("input_protobuf_encoded", str(base64.b64encode(message.SerializeToString()), "utf8")),
                ]),
            )
            return CAuthentication_BeginAuthSessionViaQR_Response.FromString(response)

        async def login_via_qr(self, display_cb: QRDisplay, poll_timeout: float = 180.0) -> str:
            if not self._requests.cookies().get("sessionid"):
                await self._requests.bytes(method="GET", url="https://steamcommunity.com")
            qr = await self._begin_qr()
            client_id = qr.client_id
            request_id = qr.request_id
            interval = qr.interval or 2.0
            last_url = qr.challenge_url
            await display_cb(last_url)

            deadline = time.monotonic() + poll_timeout
            refresh_token = None
            while time.monotonic() < deadline:
                await asyncio.sleep(max(1.0, interval))
                session = await self._poll_auth_session_status(
                    client_id=client_id, request_id=request_id,
                )
                if session.new_client_id:
                    client_id = session.new_client_id
                if session.new_challenge_url and session.new_challenge_url != last_url:

                    last_url = session.new_challenge_url
                    await display_cb(last_url)
                if session.refresh_token:
                    refresh_token = session.refresh_token
                    if session.account_name:
                        self.account_name = session.account_name
                    break
            if not refresh_token:
                raise QRLoginTimeout("QR-код не подтверждён (истекло время ожидания)")

            tokens = await self._finalize_login(
                refresh_token=refresh_token,
                sessionid=self._requests.cookies()["sessionid"],
            )
            steamid = int(tokens.steamID) if tokens.steamID else 0
            for token in tokens.transfer_info:
                await self._set_token(
                    url=token.url,
                    nonce=token.params.nonce,
                    auth=token.params.auth,
                    steamid=steamid,
                )
            if steamid:
                self._steamid = steamid
            cookies = {"steamcommunity.com": self._requests.cookies("steamcommunity.com")}
            for url in ("https://store.steampowered.com", "https://help.steampowered.com"):
                await self._requests.bytes(url, "GET")
                cookies.update({parse_url(url).host: self._requests.cookies(parse_url(url).host)})
            await self._storage.set(login=self._login, cookies=cookies)
            return self.account_name

    _QRSteamClass = QRSteam
    return _QRSteamClass

async def set_country_raw(strategy: ProxyRequestStrategy, cc: str, session_id: str) -> bool:

    hdrs = {"X-Requested-With": "XMLHttpRequest"}
    ok_store = ok_chk = False
    try:
        await strategy.request(
            URL_SET_COUNTRY, method="POST",
            data=aiohttp.FormData(fields=[("sessionid", session_id), ("cc", cc)]),
            headers={**hdrs, "Origin": "https://store.steampowered.com",
                     "Referer": "https://store.steampowered.com/account/"},
        )
        ok_store = True
    except Exception as e:
        logger.debug(f"set_country store error: {e}")

    try:
        await strategy.request(
            URL_SET_COUNTRY_CHK, method="POST",
            data=aiohttp.FormData(fields=[("sessionid", session_id), ("cc", cc)]),
            headers={**hdrs, "Origin": "https://checkout.steampowered.com",
                     "Referer": "https://checkout.steampowered.com/"},
        )
        ok_chk = True
    except Exception as e:
        logger.debug(f"set_country checkout error: {e}")

    return ok_store or ok_chk

_REDEEM_DETAIL = {
    0: "NoDetail",
    2: "InsufficientFunds",
    13: "RestrictedCountry (регион ограничен)",
    14: "BadActivationCode (неверный код — ожидаемо для fake-redeem)",
    15: "DuplicateActivationCode (код уже использован)",
    23: "StoreBillingCountryMismatch (страна биллинга не совпадает)",
    48: "NoWallet (у аккаунта нет кошелька — Steam не создаёт его этим способом)",
    53: "RateLimited",
}

def _decode_redeem_body(body: str) -> str:
    try:
        j = json.loads(body)
        d = j.get("detail")
        if d in _REDEEM_DETAIL:
            return f"{body} → detail={d}: {_REDEEM_DETAIL[d]}"
    except Exception:
        pass
    return body

async def trigger_wallet_raw(strategy: ProxyRequestStrategy, session_id: str) -> str:

    fake = "".join(random.choices("ABCDEFGHJKMNPQRTVWXY23456789", k=15))
    fake = f"{fake[:5]}-{fake[5:10]}-{fake[10:15]}"
    try:
        resp = await strategy.request(
            URL_REDEEM, method="POST",
            data=aiohttp.FormData(fields=[("wallet_code", fake), ("sessionid", session_id)]),
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://store.steampowered.com",
                "Referer": "https://store.steampowered.com/account/",
            },
        )
        try:
            return _decode_redeem_body((await resp.text())[:300])
        except Exception:
            return f"<HTTP {getattr(resp, 'status', '?')}, тело не прочитано>"
    except Exception as e:
        return f"<ошибка запроса: {str(e)[:120]}>"

async def check_proxy(proxy_url: str, timeout: float = 8.0) -> tuple[bool, str]:

    clean_p = _mask_proxy(proxy_url)
    p_url = normalize_proxy_url(proxy_url)
    if not p_url:
        _log_event("proxy_check", level=logging.WARNING, status="fail", proxy=clean_p, err="invalid_url")
        return False, "пустой/неверный URL"

    t0 = time.monotonic()
    country_code = "??"
    geo_ok = False
    geo_err = ""

    geo_endpoints = [
        ("http://ip-api.com/json/", "countryCode"),
        ("https://ipwho.is/", "country_code"),
        ("https://api.myip.com", "cc"),
    ]

    connector = aiohttp.TCPConnector(ssl=False)
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=max(3.0, timeout / 2))

    try:
        async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session:

            for geo_url, key in geo_endpoints:
                try:
                    async with session.get(geo_url, proxy=p_url) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            if isinstance(data, dict):
                                val = data.get(key)
                                if val:
                                    country_code = str(val).upper()
                                    geo_ok = True
                                    break
                        else:
                            geo_err = f"HTTP {resp.status}"
                except asyncio.TimeoutError:
                    geo_err = "таймаут"
                except Exception as e:
                    geo_err = str(e)[:60]

            try:
                async with session.get(
                    "https://store.steampowered.com/",
                    proxy=p_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                ) as resp:
                    elapsed = time.monotonic() - t0
                    ms = int(elapsed * 1000)
                    if resp.status in (200, 301, 302):
                        _log_event("proxy_check", status="ok", proxy=clean_p, country=country_code, steam="OK", http=resp.status, ms=ms)
                        return True, country_code
                    else:
                        _log_event("proxy_check", level=logging.WARNING, status="fail", proxy=clean_p, err=f"Steam HTTP {resp.status}", ms=ms)
                        return False, f"Steam HTTP {resp.status}"
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - t0
                _log_event("proxy_check", level=logging.WARNING, status="fail", proxy=clean_p, err="SteamTimeout", ms=int(elapsed * 1000))
                return False, "таймаут Steam"
            except Exception as e:
                elapsed = time.monotonic() - t0
                err_msg = _mask_proxy(str(e))[:80]
                _log_event("proxy_check", level=logging.WARNING, status="fail", proxy=clean_p, err=err_msg, ms=int(elapsed * 1000))
                return False, f"Steam: {err_msg}"
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - t0
        _log_event("proxy_check", level=logging.WARNING, status="fail", proxy=clean_p, err="Timeout", ms=int(elapsed * 1000))
        return False, "таймаут"
    except Exception as e:
        elapsed = time.monotonic() - t0
        err_msg = _mask_proxy(str(e))[:80]
        _log_event("proxy_check", level=logging.WARNING, status="fail", proxy=clean_p, err=err_msg, ms=int(elapsed * 1000))
        return False, err_msg

async def check_proxies_for_country(
    proxies: list[str],
    expected_cc: str,
    max_check: int = 5,
) -> tuple[list[str], list[str]]:

    _log_event("country_check_start", country=expected_cc, count=min(len(proxies), max_check), total=len(proxies))
    good, bad = [], []
    tasks = [check_proxy(p) for p in proxies[:max_check]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for proxy, result in zip(proxies[:max_check], results):
        clean_p = _mask_proxy(proxy)
        if isinstance(result, tuple) and result[0]:
            cc = result[1]
            if cc == expected_cc.upper() or expected_cc == "ANY":
                good.append(proxy)
                _log_event("country_proxy_result", status="ok", proxy=clean_p, country=cc)
            else:
                bad.append(proxy)
                _log_event("country_proxy_result", level=logging.WARNING, status="fail", proxy=clean_p, reason=f"WrongCountry ({cc} != {expected_cc})")
        else:
            bad.append(proxy)
            err_str = result[1] if isinstance(result, tuple) else str(result)[:60]
            _log_event("country_proxy_result", level=logging.WARNING, status="fail", proxy=clean_p, reason=err_str)
    good.extend(proxies[max_check:])
    _log_event("country_check_summary", country=expected_cc, good=len(good), bad=len(bad))
    return good, bad

async def check_account_steam_funding(steam, login: str = "") -> tuple[bool, str]:

    cfg = _load_config()
    if not cfg.get("check_steam_funding", True):
        return False, ""

    try:
        html_funds = await _net(steam.request, URL_ADDFUNDS, label=login)
        parsed = parse_store_page(html_funds)
        if parsed.get("wallet_fixed"):
            return True, "кошелёк Steam уже зафиксирован на странице addfunds (были пополнения)"
        if parsed.get("wallet_currency") and int(parsed.get("wallet_currency") or 0) > 0:
            return True, f"установлена валюта кошелька Steam (currency #{parsed.get('wallet_currency')})"
    except Exception as e:
        logger.warning(f"[{login}] Ошибка проверки addfunds: {e}")

    try:
        html_history = await _net(steam.request, URL_ACCOUNT_HISTORY, label=login)

        has_tx = bool(
            re.search(r'HelpWithTransaction\?transid=\d+', html_history, re.IGNORECASE)
            or re.search(r'class="[^"]*wallet_history_click[^"]*"', html_history, re.IGNORECASE)
            or re.search(r'<td[^>]*class="[^"]*wht_date[^"]*"[^>]*>\s*\d+', html_history, re.IGNORECASE)
            or re.search(r'<td[^>]*class="[^"]*wht_total[^"]*"[^>]*>\s*[^\s<]+', html_history, re.IGNORECASE)
            or re.search(r'<tr[^>]*class="[^"]*transactionRow[^"]*"[^>]*>\s*<td', html_history, re.IGNORECASE)
        )
        if has_tx:
            return True, "обнаружена история покупок или пополнений в Steam (account/history)"
    except Exception as e:
        logger.warning(f"[{login}] Ошибка проверки истории транзакций: {e}")

    try:
        html_acc = await _net(steam.request, URL_ACCOUNT, label=login)
        if WALLET_FIXED_RE.search(html_acc):
            return True, "обнаружен привязанный баланс/кошелёк на странице аккаунта"
        m_bal = re.search(r'accountData\s+price[^>]*>([^<]+)<', html_acc, re.IGNORECASE)
        if m_bal:
            bal_text = m_bal.group(1).strip()
            digits = re.findall(r'\d+', bal_text)
            if digits and any(int(d) > 0 for d in digits):
                return True, f"обнаружен ненулевой баланс Steam ({bal_text})"
    except Exception as e:
        logger.warning(f"[{login}] Ошибка проверки баланса на странице аккаунта: {e}")

    return False, ""

class ProxyPool:

    def __init__(self, proxies: list[str]):
        self._proxies = [p for p in (proxies or []) if p]
        self._idx = 0

    def __len__(self) -> int:
        return len(self._proxies)

    def get_next(self) -> str | None:
        if not self._proxies:
            return None
        p = self._proxies[self._idx % len(self._proxies)]
        self._idx += 1
        return p

    def ban(self, proxy: str | None) -> None:
        if not proxy:
            return
        try:
            self._proxies.remove(proxy)
            logger.warning(f"Прокси исключён из пула (постоянные сетевые ошибки): {proxy[:60]}")
        except ValueError:
            pass

class GiftCodePool:

    def __init__(self, codes: list[str], per_account: int = 1):
        self._codes = [c for c in (codes or []) if c]
        self._per = max(1, per_account)
        self._idx = 0

    def __len__(self) -> int:
        return max(0, len(self._codes) - self._idx)

    @property
    def codes(self) -> list[str]:
        return self._codes[self._idx:]

    def take(self) -> list[str]:
        if self._idx >= len(self._codes):
            return []
        batch = self._codes[self._idx:self._idx + self._per]
        self._idx += self._per
        return batch

async def _poll_store_page(steam, login: str, ready) -> dict | None:

    page = None
    for _ in range(max(1, REGION_WAIT_TRIES)):
        await asyncio.sleep(REGION_WAIT_INTERVAL)
        try:
            html = await _net(steam.request, URL_ADDFUNDS, label=login)
        except Exception as e:
            logger.warning(f"[{login}] страница addfunds недоступна при опросе: {e}")
            continue
        page = parse_store_page(html)
        if ready(page):
            return page
    return page

async def _finish_with_gifts(
    login: str, new_region: str | None, strategy, session_id, proxy_pool: "ProxyPool",
    gift_pool: "GiftCodePool | None", password: str, shared_secret: str | None,
    guard_provider: GuardProvider | None,
) -> AccountRegionResult:

    gift_results: list[tuple[str, bool, str]] = []
    if gift_pool is not None:
        codes = gift_pool.take()
        if codes:
            logger.info(f"[{login}] регион OK → активирую {len(codes)} гифт-код(ов)")
            gift_results = await _activate_gifts_inline(
                strategy, session_id, proxy_pool,
                login, password, shared_secret, codes,
                guard_provider=guard_provider,
            )
    return AccountRegionResult(login, RegionResult.OK, new_region=new_region, gift_results=gift_results)

async def _region_change_core(
    steam, strategy, session_id, login: str, country_code: str,
) -> AccountRegionResult | None:

    has_funding, fund_reason = await check_account_steam_funding(steam, login=login)
    if has_funding:
        logger.warning(f"[{login}] Защита от пополнений: {fund_reason}")
        return AccountRegionResult(
            login, RegionResult.SKIP_FIXED,
            error=f"На аккаунте обнаружены предыдущие пополнения/покупки Steam ({fund_reason}). Смена региона невозможна.",
        )

    try:
        html = await _net(steam.request, URL_ADDFUNDS, label=login)
        page_before = parse_store_page(html)
    except Exception as e:
        logger.warning(f"[{login}] страница до смены недоступна: {e}")
        page_before = {"country_code": None, "wallet_fixed": False}

    current = page_before["country_code"]
    fixed = page_before["wallet_fixed"]
    if fixed:
        return AccountRegionResult(
            login, RegionResult.SKIP_FIXED, new_region=current,
            error="Кошелёк аккаунта уже зафиксирован (были пополнения баланса).",
        )

    already_target = bool(current and current.upper() == country_code.upper())

    def _target_ready(p: dict) -> bool:
        return bool(p["country_code"] and p["country_code"].upper() == country_code.upper())

    if already_target:
        logger.info(f"[{login}] регион уже {country_code} в профиле, но не закреплён — закрепляем")
        page_mid = page_before
    else:
        await _net(set_country_raw, strategy, country_code, session_id, label=login)
        page_mid = await _poll_store_page(steam, login, ready=_target_ready)
        if page_mid is None:
            logger.warning(f"[{login}] страница перед закреплением недоступна — повтор")
            return None

    mid_region = page_mid["country_code"]
    mid_fixed = page_mid["wallet_fixed"]
    mid_ok = bool(mid_region and mid_region.upper() == country_code.upper())

    if mid_fixed:
        if mid_ok:
            return AccountRegionResult(login, RegionResult.OK, new_region=mid_region)
        return AccountRegionResult(
            login, RegionResult.FAIL_WRONG_REGION, new_region=mid_region,
            error=f"Ожидался {country_code}, зафиксирован {mid_region}",
        )

    if not mid_ok:
        logger.warning(
            f"[{login}] страна не подтверждена перед закреплением "
            f"(текущая: {mid_region!r}, нужна: {country_code}) — повтор без фиксации"
        )
        return None

    wallet_resp = await _net(trigger_wallet_raw, strategy, session_id, label=login)
    logger.debug(f"[{login}] ответ Steam на закрепление кошелька: {wallet_resp!r}")

    page_after = await _poll_store_page(steam, login, ready=lambda p: bool(p["wallet_fixed"]))
    if page_after is None:
        logger.warning(f"[{login}] страница после закрепления недоступна — повтор")
        return None

    new_region = page_after["country_code"]
    new_fixed = page_after["wallet_fixed"]
    region_ok = new_region and new_region.upper() == country_code.upper()
    wallet_ok = new_fixed

    if region_ok and wallet_ok:
        return AccountRegionResult(login, RegionResult.OK, new_region=new_region)
    elif region_ok and not wallet_ok:
        return AccountRegionResult(login, RegionResult.SENT, new_region=new_region)
    elif not region_ok and wallet_ok:
        return AccountRegionResult(
            login, RegionResult.FAIL_WRONG_REGION, new_region=new_region,
            error=f"Ожидался {country_code}, зафиксирован {new_region}",
        )
    logger.warning(
        f"[{login}] регион не применился (текущий: {new_region!r}, нужен: {country_code}) — повтор"
    )
    return None

async def process_one_account(
    login: str,
    password: str,
    shared_secret: str | None,
    proxy_pool: "ProxyPool",
    country_code: str,
    semaphore: asyncio.Semaphore,
    gift_pool: "GiftCodePool | None" = None,
    guard_provider: GuardProvider | None = None,
    max_attempts_override: int | None = None,
    cancel_event: asyncio.Event | None = None,
) -> AccountRegionResult:

    async with semaphore:
        if cancel_event is not None and cancel_event.is_set():
            return AccountRegionResult(login, RegionResult.SKIPPED, error="отменено пользователем")
        manual_mode = guard_provider is not None

        if manual_mode:
            Steam = await _try_import_manual_steam()
        else:
            Steam = await _try_import_pysteamauth()
        if Steam is None:
            return AccountRegionResult(
                login=login,
                status=RegionResult.ERROR,
                error="pysteamauth не установлен. pip install pysteamauth",
            )

        if max_attempts_override is not None:
            max_attempts = max(1, max_attempts_override)
        else:

            max_attempts = max(len(proxy_pool) * 3, MAX_PROXY_ATTEMPTS_FLOOR)
        last_status = RegionResult.FAIL_CHANGE
        last_error: str | None = None

        guard_rejections = 0
        guard_reject_limit = 3

        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            if attempt > 1:
                if last_status == RegionResult.FAIL_RATE_LIMIT:
                    logger.info(
                        f"[{login}] rate-limit Steam — пауза {RATE_LIMIT_DELAY}с "
                        f"(попытка {attempt}/{max_attempts})"
                    )
                    await asyncio.sleep(RATE_LIMIT_DELAY)
                else:
                    logger.info(f"[{login}] следующий прокси (попытка {attempt}/{max_attempts})")

            proxy = proxy_pool.get_next()
            if proxy is None:

                _log_event("account_proxy_exhausted", level=logging.WARNING, login=login, reason="pool_empty")
                last_status, last_error = RegionResult.FAIL_SESSION, "Все прокси недоступны (исключены из пула)"
                break

            masked_px = _mask_proxy(proxy)
            _log_event("account_attempt", login=login, attempt=attempt, max=max_attempts, proxy=masked_px)
            strategy = ProxyRequestStrategy(proxy)
            try:
                if manual_mode:
                    steam = Steam(login=login, password=password, request_strategy=strategy)

                    steam.set_guard_provider(guard_provider)
                else:
                    steam = Steam(
                        login=login,
                        password=password,
                        shared_secret=shared_secret,
                        request_strategy=strategy,
                    )

                try:
                    await _net(steam.login_to_steam, label=login)
                except GuardCodeSkipped:
                    return AccountRegionResult(login, RegionResult.SKIPPED, error="пропущено пользователем")
                except GuardCodeRejected as e:
                    guard_rejections += 1
                    last_status, last_error = RegionResult.FAIL_WRONG_GUARD, str(e)
                    _log_event("account_guard_rejected", level=logging.WARNING, login=login, count=guard_rejections, limit=guard_reject_limit)
                    if guard_rejections >= guard_reject_limit:
                        return AccountRegionResult(
                            login, RegionResult.FAIL_WRONG_GUARD,
                            error="Неверный/просроченный Guard-код (исчерпаны попытки ввода)",
                        )
                    continue
                except Exception as e:
                    msg = str(e)
                    if "shared_secret is not specified" in msg:
                        return AccountRegionResult(login, RegionResult.FAIL_NO_MAFILE, error=msg)
                    m = re.search(r"'code':\s*(\d+)", msg)
                    code_num = int(m.group(1)) if m else None
                    if code_num == 5:
                        return AccountRegionResult(login, RegionResult.FAIL_WRONG_PASS, error=msg)
                    if code_num == 84:
                        last_status, last_error = RegionResult.FAIL_RATE_LIMIT, msg
                        _log_event("account_rate_limit", level=logging.WARNING, login=login, code=84, attempt=attempt)
                        continue
                    if _is_network_error(e):
                        proxy_pool.ban(proxy)
                        _log_event("account_proxy_error", level=logging.WARNING, login=login, proxy=masked_px, err=msg[:80])
                    else:
                        _log_event("account_steam_error", level=logging.WARNING, login=login, err=msg[:80])
                    last_status, last_error = RegionResult.FAIL_SESSION, msg
                    continue

                if not await steam.is_authorized():
                    _log_event("account_not_authorized", level=logging.WARNING, login=login, attempt=attempt)
                    last_status = RegionResult.FAIL_SESSION
                    continue

                try:
                    session_id = await _net(steam.sessionid, "store.steampowered.com", label=login)
                except Exception:
                    try:
                        session_id = await _net(steam.sessionid, "steamcommunity.com", label=login)
                    except Exception as e:
                        proxy_pool.ban(proxy)
                        _log_event("account_session_error", level=logging.WARNING, login=login, proxy=masked_px, err=str(e)[:80])
                        continue

                result = await _region_change_core(steam, strategy, session_id, login, country_code)
                if result is None:
                    continue
                if result.status == RegionResult.OK:
                    return await _finish_with_gifts(
                        login, result.new_region, strategy, session_id, proxy_pool,
                        gift_pool, password, shared_secret, guard_provider,
                    )
                return result

            except Exception as e:
                last_error = str(e)
                if _is_network_error(e):
                    proxy_pool.ban(proxy)
                    _log_event("account_net_error", level=logging.WARNING, login=login, proxy=masked_px, err=str(e)[:80])
                else:
                    _log_event("account_error", level=logging.WARNING, login=login, err=str(e)[:80])
                continue
            finally:
                await strategy.close()

        _log_event("account_change_failed", level=logging.WARNING, login=login, attempts=max_attempts, status=str(last_status))
        return AccountRegionResult(
            login, last_status,
            error=last_error or f"Не удалось сменить регион после {max_attempts} попыток",
        )

async def process_one_account_qr(
    proxy_pool: "ProxyPool",
    country_code: str,
    display_cb: QRDisplay,
    gift_pool: "GiftCodePool | None" = None,
    poll_timeout: float = 180.0,
    begin_retries: int = 5,
) -> AccountRegionResult:

    QRSteam = await _try_import_qr_steam()
    if QRSteam is None:
        return AccountRegionResult("(qr)", RegionResult.ERROR,
                                   error="pysteamauth/QR недоступен. pip install pysteamauth")

    strategy = None
    steam = None
    account = None
    last_err: str | None = None
    tries = max(1, min(len(proxy_pool) or 1, begin_retries))
    for _ in range(tries):
        proxy = proxy_pool.get_next()
        if proxy is None:

            return AccountRegionResult("(qr)", RegionResult.FAIL_SESSION,
                                       error=last_err or "Все прокси недоступны (исключены из пула)")
        strategy = ProxyRequestStrategy(proxy)
        steam = QRSteam(login="qr_session", password="", request_strategy=strategy)
        try:
            account = await steam.login_via_qr(display_cb, poll_timeout=poll_timeout)
            break
        except asyncio.CancelledError:

            await strategy.close()
            raise
        except QRLoginTimeout as e:
            await strategy.close()
            return AccountRegionResult("(qr)", RegionResult.SKIPPED, error=str(e))
        except Exception as e:
            last_err = str(e)
            await strategy.close()
            strategy = None
            if _is_network_error(e):
                proxy_pool.ban(proxy)
                logger.warning(f"[QR] ошибка прокси при начале сессии: {str(e)[:80]}")
                continue
            return AccountRegionResult("(qr)", RegionResult.ERROR, error=str(e))
    if strategy is None:
        return AccountRegionResult("(qr)", RegionResult.FAIL_SESSION,
                                   error=last_err or "нет рабочего прокси для QR")

    login = account or "(qr)"
    try:
        if not await steam.is_authorized():
            return AccountRegionResult(login, RegionResult.FAIL_SESSION, error="сессия не авторизована")
        try:
            session_id = await _net(steam.sessionid, "store.steampowered.com", label=login)
        except Exception:
            session_id = await _net(steam.sessionid, "steamcommunity.com", label=login)

        result = await _region_change_core(steam, strategy, session_id, login, country_code)
        if result is None:
            return AccountRegionResult(login, RegionResult.FAIL_CHANGE,
                                       error="регион не применился — повторите вход по QR")
        if result.status == RegionResult.OK:
            return await _finish_with_gifts(
                login, result.new_region, strategy, session_id, proxy_pool,
                gift_pool, password="", shared_secret=None, guard_provider=None,
            )
        return result
    except Exception as e:
        logger.warning(f"[{login}] ошибка смены региона после QR-входа: {e}")
        return AccountRegionResult(login, RegionResult.ERROR, error=str(e))
    finally:
        await strategy.close()

def _gift_log(login: str, code: str, ok: bool, msg: str) -> None:
    logger.log(
        logging.INFO if ok else logging.WARNING,
        f"[{login}] gift redeem {code!r}: {'OK' if ok else 'FAIL'} — {msg}",
    )

async def _redeem_code_on_session(strategy, session_id, login: str, code: str) -> tuple[bool, str, bool]:

    try:
        resp = await strategy.request(
            URL_REDEEM_GIFT, method="POST",
            data=aiohttp.FormData(fields=[
                ("wallet_code", code.strip()),
                ("sessionid", session_id),
            ]),
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://store.steampowered.com",
                "Referer": "https://store.steampowered.com/account/",
            },
        )
    except Exception as e:
        msg = f"redeem error: {str(e)[:80]}"
        _gift_log(login, code, False, msg)
        return False, msg, _is_network_error(e)

    if resp.status in _RETRYABLE_HTTP:
        msg = f"HTTP {resp.status}"
        _gift_log(login, code, False, msg)
        return False, msg, True
    try:
        data = await resp.json(content_type=None)
        success = data.get("success", 1) == 1
        detail = data.get("detail", "")
        if success:
            _gift_log(login, code, True, "OK")
            return True, "OK", False
        hint = _REDEEM_DETAIL.get(detail)
        msg = f"detail={detail}: {hint}" if hint else (str(detail) or "Ошибка активации")
        _gift_log(login, code, False, msg)
        return False, msg, False
    except Exception:
        text = await resp.text()
        _gift_log(login, code, False, text[:80])
        return False, text[:80], False

async def _quick_login(proxy, login: str, password: str, shared_secret: str | None,
                       guard_provider: GuardProvider | None = None):

    strategy = ProxyRequestStrategy(proxy)
    try:
        if guard_provider is not None:
            Steam = await _try_import_manual_steam()
            if Steam is None:
                await strategy.close()
                return None
            steam = Steam(login=login, password=password, request_strategy=strategy)
            code = await guard_provider(login)
            if not (code or "").strip():
                await strategy.close()
                return None
            steam.set_guard_code(code)
        else:
            Steam = await _try_import_pysteamauth()
            if Steam is None:
                await strategy.close()
                return None
            steam = Steam(login=login, password=password,
                          shared_secret=shared_secret, request_strategy=strategy)
        await _net(steam.login_to_steam, label=login)
        if not await steam.is_authorized():
            await strategy.close()
            return None
        try:
            session_id = await _net(steam.sessionid, "store.steampowered.com", label=login)
        except Exception:
            session_id = await _net(steam.sessionid, "steamcommunity.com", label=login)
        return steam, strategy, session_id
    except Exception as e:
        logger.warning(f"[{login}] gift relogin failed: {str(e)[:80]}")
        await strategy.close()
        return None

async def _activate_gifts_inline(
    strategy, session_id, proxy_pool: "ProxyPool",
    login: str, password: str, shared_secret: str | None, codes: list[str],
    guard_provider: GuardProvider | None = None,
) -> list[tuple[str, bool, str]]:

    results: list[tuple[str, bool, str]] = []
    cur_strategy, cur_sid = strategy, session_id
    extra_strategy = None
    try:
        for code in codes:
            ok, msg, net = await _redeem_code_on_session(cur_strategy, cur_sid, login, code)
            attempt = 1
            while (not ok) and net and attempt < GIFT_MAX_PROXY_ATTEMPTS:
                attempt += 1
                proxy = proxy_pool.get_next()
                relog = await _quick_login(proxy, login, password, shared_secret,
                                           guard_provider=guard_provider)
                if relog is None:
                    continue
                if extra_strategy is not None:
                    await extra_strategy.close()
                _steam2, extra_strategy, cur_sid = relog
                cur_strategy = extra_strategy
                ok, msg, net = await _redeem_code_on_session(cur_strategy, cur_sid, login, code)
            results.append((code, ok, msg))
    finally:
        if extra_strategy is not None:
            await extra_strategy.close()
    return results

async def change_region_batch(
    accounts: list[dict],
    proxies: list[str],
    country_code: str,
    max_workers: int = 3,
    progress_cb = None,
    gift_pool: "GiftCodePool | None" = None,
    guard_provider: GuardProvider | None = None,
    max_attempts_override: int | None = None,
    cancel_event: asyncio.Event | None = None,
) -> BatchRegionResult:

    manual_mode = guard_provider is not None

    workers = 1 if manual_mode else max(1, max_workers)
    semaphore = asyncio.Semaphore(workers)
    batch = BatchRegionResult(country_code=country_code, total=len(accounts))
    proxy_pool = ProxyPool(proxies)

    async def _worker(acc: dict) -> AccountRegionResult:
        return await process_one_account(
            login = acc["login"],
            password = acc["password"],
            shared_secret = acc.get("shared_secret"),
            proxy_pool = proxy_pool,
            country_code = country_code,
            semaphore = semaphore,
            gift_pool = gift_pool,
            guard_provider = guard_provider,
            max_attempts_override = max_attempts_override,
            cancel_event = cancel_event,
        )

    if manual_mode:

        done = 0
        try:
            for acc in accounts:
                if cancel_event is not None and cancel_event.is_set():
                    batch.results.append(AccountRegionResult(
                        acc["login"], RegionResult.SKIPPED, error="отменено пользователем"))
                    done += 1
                    if progress_cb:
                        try:
                            await progress_cb(done, len(accounts), batch.results[-1])
                        except Exception:
                            pass
                    continue
                result = await _worker(acc)
                batch.results.append(result)
                done += 1
                logger.info(
                    f"[{result.login}] итог: {result.status}"
                    + (f" region={result.new_region}" if result.new_region else "")
                    + (f" error={result.error}" if result.error else "")
                )
                if progress_cb:
                    try:
                        await progress_cb(done, len(accounts), result)
                    except Exception:
                        pass
        except asyncio.CancelledError:

            logger.info("change_region_batch (manual) отменён — частичный результат")
        return batch

    tasks = [asyncio.ensure_future(_worker(acc)) for acc in accounts]
    done = 0
    try:
        for coro in asyncio.as_completed(tasks):
            result = await coro
            batch.results.append(result)
            done += 1
            logger.info(
                f"[{result.login}] итог: {result.status}"
                + (f" region={result.new_region}" if result.new_region else "")
                + (f" error={result.error}" if result.error else "")
            )
            if progress_cb:
                try:
                    await progress_cb(done, len(accounts), result)
                except Exception:
                    pass
    except asyncio.CancelledError:

        for t in tasks:
            if not t.done():
                t.cancel()
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        batch.results = [r for r in gathered if isinstance(r, AccountRegionResult)]
        logger.info("change_region_batch отменён — частичный результат")
    return batch

def parse_proxies(text: str) -> list[str]:

    proxies: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        norm = normalize_proxy_url(line)
        if norm:
            proxies.append(norm)
    logger.info(f"[{NAME}] Распознано {len(proxies)} прокси из входного текста")
    return proxies

def parse_gift_codes(text: str) -> list[str]:

    codes = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cleaned = re.sub(r"[^A-Za-z0-9\-]", "", line)
        if len(cleaned) >= 15:
            codes.append(cleaned.upper())
    return codes

def validate_gift_codes_format(codes: list[str]) -> tuple[list[str], list[str]]:

    pattern = re.compile(r'^[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}$|^[A-Z0-9]{15,20}$')
    valid = [c for c in codes if pattern.match(c)]
    invalid = [c for c in codes if not pattern.match(c)]
    return valid, invalid

cardinal_instance = None
bot_instance = None
admin_chat_id = None

BASE_DIR = os.getcwd()
STORAGE_DIR = os.path.join(BASE_DIR, "storage", "plugins", "src_plugin")
CONFIG_FILE = os.path.join(STORAGE_DIR, "settings.json")
PROXIES_FILE = os.path.join(STORAGE_DIR, "proxies.txt")
GIFTS_FILE = os.path.join(STORAGE_DIR, "gift_codes.txt")
MAFILES_DIR = os.path.join(STORAGE_DIR, "mafiles")
STATS_FILE = os.path.join(STORAGE_DIR, "stats.json")

_config_lock = threading.Lock()
_waiting: dict[int, dict[str, Any]] = {}

DEFAULT_BUYER_MESSAGES: dict[str, str] = {
    "welcome": (
        "👋 Здравствуйте, {buyer_username}!\n\n"
        "Вы оформили заказ #{order_id} на смену региона Steam на {country_name}.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔹 Шаг 1 из 2: Логин аккаунта\n\n"
        "Пожалуйста, отправьте логин от вашего аккаунта Steam в этот чат:\n"
        "(Или отправьте сразу «логин:пароль»)\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    ),
    "ask_password": (
        "✅ Логин принят: {login}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔹 Шаг 2 из 2: Пароль аккаунта\n\n"
        "Теперь отправьте пароль от вашего аккаунта Steam в этот чат:\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    ),
    "data_received": (
        "✅ Данные получены!\n\n"
        "⏳ Начинаем авторизацию в Steam и смену региона на {country_name}...\n"
        "Пожалуйста, оставайтесь на связи. При необходимости бот запросит код Steam Guard."
    ),
    "guard_request": (
        "🔐 Требуется код Steam Guard!\n\n"
        "На ваш мобильный Guard или почту отправлен проверочный код.\n"
        "Пожалуйста, отправьте 5-значный код сообщением в этот чат:"
    ),
    "success": (
        "🎉 Регион Steam успешно изменён!\n\n"
        "• Новый регион: {country_name}\n"
        "• Аккаунт: {login}\n\n"
        "Спасибо за покупку! Пожалуйста, подтвердите выполнение заказа на FunPay и оставьте отзыв ⭐⭐⭐⭐⭐"
    ),
    "fixed_wallet_refund": (
        "🛑 Смена региона невозможна!\n\n"
        "На вашем Steam аккаунте обнаружены предыдущие пополнения баланса или покупки (смена региона доступна только на чистых аккаунтах без пополнений).\n\n"
        "💸 Мы оформили автоматический возврат средств за ваш заказ #{order_id}.\n"
        "Деньги уже возвращены на ваш баланс FunPay."
    ),
    "no_proxies_refund": (
        "⚠️ Временная недоступность прокси\n\n"
        "К сожалению, для целевого региона {country_name} временно закончились рабочие прокси в пуле.\n\n"
        "💸 Оформлен автоматический возврат средств за ваш заказ #{order_id}.\n"
        "Приносим извинения за неудобства!"
    ),
    "error_refund": (
        "❌ Не удалось сменить регион Steam ({reason})\n\n"
        "💸 Мы оформили автоматический возврат средств за ваш заказ #{order_id}.\n"
        "Деньги уже возвращены на ваш баланс FunPay."
    ),
}

BUYER_MESSAGE_LABELS: dict[str, str] = {
    "welcome": "👋 Шаг 1: Приветствие (запрос логина)",
    "ask_password": "🔑 Шаг 2: Запрос пароля",
    "data_received": "⏳ Данные получены (старт)",
    "guard_request": "🔐 Запрос Steam Guard",
    "success": "✅ Успешная смена региона",
    "fixed_wallet_refund": "🛑 Были пополнения (автовозврат)",
    "no_proxies_refund": "⚠️ Закончились прокси (автовозврат)",
    "error_refund": "❌ Ошибка смены (автовозврат)",
}

BUYER_MESSAGE_HINTS: dict[str, str] = {
    "welcome": "Переменные: <code>{buyer_username}</code>, <code>{country_name}</code>, <code>{order_id}</code>",
    "ask_password": "Переменные: <code>{login}</code>, <code>{country_name}</code>, <code>{order_id}</code>",
    "data_received": "Переменные: <code>{login}</code>, <code>{country_name}</code>, <code>{order_id}</code>",
    "guard_request": "Переменные: <code>{login}</code>, <code>{order_id}</code>",
    "success": "Переменные: <code>{country_name}</code>, <code>{login}</code>, <code>{order_id}</code>",
    "fixed_wallet_refund": "Переменные: <code>{order_id}</code>, <code>{login}</code>",
    "no_proxies_refund": "Переменные: <code>{order_id}</code>, <code>{country_name}</code>",
    "error_refund": "Переменные: <code>{order_id}</code>, <code>{reason}</code>",
}

def _get_buyer_messages() -> dict[str, str]:
    cfg = _load_config()
    saved = cfg.get("buyer_messages")
    merged = dict(DEFAULT_BUYER_MESSAGES)
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k in DEFAULT_BUYER_MESSAGES and v:
                merged[k] = str(v)
    return merged

def _render_buyer_msg(key: str, **kwargs) -> str:
    msgs = _get_buyer_messages()
    tmpl = msgs.get(key, DEFAULT_BUYER_MESSAGES.get(key, ""))
    defaults = {
        "buyer_username": "покупатель",
        "login": "аккаунт",
        "country_name": "выбранную страну",
        "order_id": "",
        "reason": "ошибка смены региона",
    }
    merged_kwargs = {**defaults, **kwargs}
    try:
        return tmpl.format(**merged_kwargs)
    except Exception:
        res = tmpl
        for k, v in merged_kwargs.items():
            res = res.replace(f"{{{k}}}", str(v))
        return res

DEFAULT_CONFIG: dict[str, Any] = {
    "plugin_enabled": True,
    "auto_refund_enabled": True,
    "auto_deactivate_lots": True,
    "check_steam_funding": True,
    "notify_tg_orders": True,
    "notify_tg_success": True,
    "notify_tg_errors": True,
    "max_workers": 5,
    "request_timeout": 30.0,
    "connect_timeout": 15.0,
    "auto_redeem_gift": True,
    "rate_limit_delay": 30,
    "lots": {},
    "buyer_messages": dict(DEFAULT_BUYER_MESSAGES),
}

def _get_lots_dict(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:

    raw = cfg.get("lots")
    res: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                res[str(k)] = {
                    "id": str(v.get("id", k)),
                    "title": str(v.get("title", f"Лот {k}")),
                    "enabled": bool(v.get("enabled", True)),
                    "country": str(v.get("country", "KZ")).upper(),
                    "proxies": list(v.get("proxies") or []),
                }
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                lid = str(item.get("id", ""))
                if lid:
                    res[lid] = {
                        "id": lid,
                        "title": str(item.get("title", f"Лот {lid}")),
                        "enabled": bool(item.get("enabled", True)),
                        "country": str(item.get("country", "KZ")).upper(),
                        "proxies": list(item.get("proxies") or []),
                    }
            elif isinstance(item, (int, str)):
                lid = str(item)
                res[lid] = {
                    "id": lid,
                    "title": f"Лот {lid}",
                    "enabled": True,
                    "country": "KZ",
                    "proxies": [],
                }
    return res

DEFAULT_STATS: dict[str, Any] = {
    "total_operations": 0,
    "success": 0,
    "failed": 0,
    "gifts_redeemed": 0,
    "last_operation": "нет данных",
}

def _init_storage():
    os.makedirs(STORAGE_DIR, exist_ok=True)
    os.makedirs(MAFILES_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
    if not os.path.exists(PROXIES_FILE):
        with open(PROXIES_FILE, "w", encoding="utf-8") as f:
            f.write("# Список прокси (каждый с новой строки):\n# ip:port\n# ip:port:user:pass\n# http://user:pass@ip:port\n")
    if not os.path.exists(GIFTS_FILE):
        with open(GIFTS_FILE, "w", encoding="utf-8") as f:
            f.write("# Список Steam Wallet Gift-кодов (каждый с новой строки):\n# XXXXX-XXXXX-XXXXX\n")
    if not os.path.exists(STATS_FILE):
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_STATS, f, indent=2, ensure_ascii=False)

def _load_config() -> dict[str, Any]:
    with _config_lock:
        if not os.path.exists(CONFIG_FILE):
            return dict(DEFAULT_CONFIG)
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = dict(DEFAULT_CONFIG)
            if isinstance(data, dict):
                cfg.update(data)
            return cfg
        except Exception as e:
            logger.error(f"[{NAME}] Ошибка чтения {CONFIG_FILE}: {e}")
            return dict(DEFAULT_CONFIG)

def _save_config(cfg: dict[str, Any]) -> None:
    with _config_lock:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[{NAME}] Ошибка сохранения {CONFIG_FILE}: {e}")

def _load_stats() -> dict[str, Any]:
    if not os.path.exists(STATS_FILE):
        return dict(DEFAULT_STATS)
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        res = dict(DEFAULT_STATS)
        if isinstance(data, dict):
            res.update(data)
        return res
    except Exception:
        return dict(DEFAULT_STATS)

def _save_stats(st: dict[str, Any]) -> None:
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

def _load_proxies() -> list[str]:
    if not os.path.exists(PROXIES_FILE):
        return []
    try:
        with open(PROXIES_FILE, "r", encoding="utf-8") as f:
            return parse_proxies(f.read())
    except Exception as e:
        logger.error(f"[{NAME}] Ошибка чтения {PROXIES_FILE}: {e}")
        return []

def _save_proxies(proxies: list[str]) -> None:
    try:
        with open(PROXIES_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(proxies) + ("\n" if proxies else ""))
    except Exception as e:
        logger.error(f"[{NAME}] Ошибка сохранения {PROXIES_FILE}: {e}")

def _load_gift_codes() -> list[str]:
    if not os.path.exists(GIFTS_FILE):
        return []
    try:
        with open(GIFTS_FILE, "r", encoding="utf-8") as f:
            return parse_gift_codes(f.read())
    except Exception as e:
        logger.error(f"[{NAME}] Ошибка чтения {GIFTS_FILE}: {e}")
        return []

def _save_gift_codes(codes: list[str]) -> None:
    try:
        with open(GIFTS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(codes) + ("\n" if codes else ""))
    except Exception as e:
        logger.error(f"[{NAME}] Ошибка сохранения {GIFTS_FILE}: {e}")

def _list_mafiles() -> list[str]:
    if not os.path.exists(MAFILES_DIR):
        return []
    try:
        return [f for f in os.listdir(MAFILES_DIR) if f.endswith(".maFile")]
    except Exception:
        return []

def _country_display(code: str) -> str:
    code = (code or "").upper()
    return COUNTRY_NAMES.get(code, f"🌐 {code}")

def _is_authorized(user_id) -> bool:
    try:
        auth = getattr(getattr(cardinal_instance, 'telegram', None), 'authorized_users', None)
        if isinstance(auth, dict) and auth:
            return user_id in auth or int(user_id) in auth
    except Exception:
        pass
    return True

def _tg_send(chat_id, text: str, reply_markup=None) -> None:
    if not bot_instance or not chat_id:
        return
    try:
        bot_instance.send_message(int(chat_id), text, parse_mode='HTML', reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"[{NAME}] TG send error: {e}")

def _tg_edit(chat_id, message_id, text: str, reply_markup=None) -> None:
    if not bot_instance or not chat_id or not message_id:
        return
    try:
        bot_instance.edit_message_text(text, int(chat_id), int(message_id), parse_mode='HTML', reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception:
        _tg_send(chat_id, text, reply_markup)

def _notify_tg(text: str, ntype: str = "order") -> None:

    cfg = _load_config()
    if ntype == "order" and not cfg.get("notify_tg_orders", True):
        return
    if ntype == "success" and not cfg.get("notify_tg_success", True):
        return
    if ntype == "error" and not cfg.get("notify_tg_errors", True):
        return

    targets = []
    if cardinal_instance and getattr(cardinal_instance, 'telegram', None):
        auth = getattr(cardinal_instance.telegram, 'authorized_users', None)
        if isinstance(auth, dict) and auth:
            targets = list(auth.keys())
    if not targets and admin_chat_id:
        targets = [admin_chat_id]

    if not targets or not bot_instance:
        return

    for cid in targets:
        try:
            _tg_send(cid, text)
        except Exception as e:
            logger.debug(f"[{NAME}] TG notification failed: {e}")

def _make_kb(rows: list[list[tuple[str, str]]]):
    if not tg_types:
        return None
    kb = tg_types.InlineKeyboardMarkup()
    for row in rows:
        kb.row(*[tg_types.InlineKeyboardButton(text, callback_data=data) for text, data in row])
    return kb

def _mask_secret(val: str, prefix_len=3, suffix_len=3) -> str:
    if len(val) <= prefix_len + suffix_len:
        return "***"
    return f"{val[:prefix_len]}...{val[-suffix_len:]}"

def _about_text():
    return (
        f"🧩 <b>Плагин:</b> {NAME}\n"
        f"📦 <b>Версия:</b> {VERSION}\n"
        f"👤 <b>Автор:</b> <a href=\"{CREATOR_URL}\">{CREDITS}</a>\n\n"
        "Выберите раздел ниже."
    )

def _home_kb():
    return _make_kb([
        [("⚙️ Настройки", "src_settings_panel"), ("ℹ️ Информация", "src_info")],
        [("⬆️ Обновить плагин", "src_update_menu"), ("🗑 Удалить", "src_delete_ask")],
        [("🔙 К списку плагинов", CB_PLUGINS_LIST_OPEN)],
    ])

def _open_home(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    _tg_edit(chat_id, message_id, _about_text(), _home_kb()) if message_id else _tg_send(chat_id, _about_text(), _home_kb())

def _info_text():
    return (
        "ℹ️ <b>Информация</b>\n\n"
        f"Здесь находятся официальные ссылки {NAME}.\n\n"
        "• <b>Чат</b> - помощь и общение.\n"
        "• <b>Канал</b> - новости и обновления.\n"
        "• <b>Инструкция</b> - настройка и использование плагина.\n"
        "• <b>Мой Telegram</b> - связь с автором."
    )

def _info_kb():
    if not tg_types:
        return None
    kb = tg_types.InlineKeyboardMarkup()
    kb.row(tg_types.InlineKeyboardButton("💬 Чат", url=GROUP_URL), tg_types.InlineKeyboardButton("📢 Канал", url=CHANNEL_URL))
    kb.row(tg_types.InlineKeyboardButton("📖 Инструкция", url=INSTRUCTION_URL))
    kb.row(tg_types.InlineKeyboardButton("📚 Альтернативная инструкция", url=ALT_INSTRUCTION_URL))
    kb.row(tg_types.InlineKeyboardButton("👤 Мой Telegram", url=CREATOR_URL))
    kb.add(tg_types.InlineKeyboardButton("◀️ Назад", callback_data="src_home"))
    return kb

def _open_info(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    _tg_edit(chat_id, message_id, _info_text(), _info_kb()) if message_id else _tg_send(chat_id, _info_text(), _info_kb())

def _update_menu_text():
    return (
        f"⬆️ <b>Обновление {NAME}</b>\n\n"
        f"Текущая версия: {VERSION}\n\n"
        "• <b>Обновить локально</b> — пришлите новый файл плагина <code>.py</code> в этот чат. Он будет проверен и установлен вместо текущего файла.\n"
        "• <b>Обновить онлайн</b> — проверить новую версию и скачать её с GitHub.\n\n"
        "Перед заменой автоматически создаётся резервная копия текущего плагина и настроек."
    )

def _update_menu_kb():
    return _make_kb([
        [("📥 Обновить локально", "src_update_local")],
        [("🌐 Обновить онлайн", "src_update_online")],
        [("◀️ Назад", "src_home")],
    ])

def _open_update_menu(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    _tg_edit(chat_id, message_id, _update_menu_text(), _update_menu_kb()) if message_id else _tg_send(chat_id, _update_menu_text(), _update_menu_kb())

def _delete_confirm_text():
    return (
        "⚠️ <b>Удаление плагина</b>\n\n"
        f"Вы точно хотите удалить <b>{NAME}</b>?\n\n"
        "Будут удалены:\n"
        "• файлы плагина\n"
        "• настройки и логи\n\n"
        "<b>Действие необратимо.</b>\n"
        "После удаления выполните перезапуск: напишите команду <code>/restart</code>."
    )

def _delete_confirm_kb():
    return _make_kb([
        [("✅ Да, удалить", "src_delete_yes"), ("❌ Нет", "src_home")],
    ])

def _open_delete_confirm(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    _tg_edit(chat_id, message_id, _delete_confirm_text(), _delete_confirm_kb()) if message_id else _tg_send(chat_id, _delete_confirm_text(), _delete_confirm_kb())

def _settings_panel_text(chat_id):
    cfg = _load_config()
    plugin_state = "🟢 Включено" if cfg.get("plugin_enabled", True) else "🔴 Выключено"
    lots = _get_lots_dict(cfg)
    total_lots = len(lots)
    active_lots = sum(1 for l in lots.values() if l.get("enabled", False))
    proxies = _load_proxies()
    gifts = _load_gift_codes()

    return (
        "⚙️ <b>Панель настроек</b>\n\n"
        f"• Плагин: <b>{plugin_state}</b>\n"
        f"• Лоты: <b>🟢 Активных: {active_lots} / Всего: {total_lots} шт.</b>\n"
        f"• База: <code>{len(proxies)}</code> прокси / <code>{len(gifts)}</code> гифтов\n\n"
        "Выберите раздел:"
    )

def _settings_panel_kb(chat_id):
    cfg = _load_config()
    lots = _get_lots_dict(cfg)
    rows = [
        [("⚙️ Настройки плагина", "src_mini_settings")],
        [(f"⭐ Настройка лотов · {len(lots)}", "src_lots_menu")],
        [("📊 Статистика", "src_stats")],
        [("◀️ Назад", "src_home")],
    ]
    return _make_kb(rows)

def _open_settings_panel(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    text = _settings_panel_text(chat_id)
    kb = _settings_panel_kb(chat_id)
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _mini_settings_text(chat_id):
    cfg = _load_config()
    enabled = cfg.get("plugin_enabled", True)
    auto_refund = cfg.get("auto_refund_enabled", True)
    auto_deact = cfg.get("auto_deactivate_lots", True)
    notif_orders = cfg.get("notify_tg_orders", True)
    redeem = cfg.get("auto_redeem_gift", True)
    workers = cfg.get("max_workers", 5)
    timeout = cfg.get("request_timeout", 30.0)
    gifts = _load_gift_codes()
    lots = _get_lots_dict(cfg)

    return (
        f"⚙️ <b>Настройки плагина</b>\n\n"
        f"• Состояние: <b>{'🟢 включён' if enabled else '🔴 выключен'}</b>\n"
        f"• Автовозврат: <b>{'🟢 ВКЛ' if auto_refund else '🔴 ВЫКЛ'}</b>\n"
        f"• Автодеактивация лотов: <b>{'🟢 ВКЛ' if auto_deact else '🔴 ВЫКЛ'}</b>\n"
        f"• Уведомления Telegram: <b>{'🟢 ВКЛ' if notif_orders else '🔴 ВЫКЛ'}</b>\n"
        f"• Одновременных замен: <b>{workers}</b>\n"
        f"• Таймаут запросов: <b>{int(timeout)} сек</b>\n"
        f"• Активация гифтов: <b>{'🟢 ВКЛ' if redeem else '🔴 ВЫКЛ'}</b>\n"
        f"• База гифтов: <b>{len(gifts)} шт.</b>\n"
        f"• Привязано лотов: <b>{len(lots)} шт.</b>\n\n"
        "Выберите категорию:"
    )

def _mini_settings_kb(chat_id):
    cfg = _load_config()
    enabled = cfg.get("plugin_enabled", True)
    rows = [
        [(f"🧩 Состояние плагина: {'🟢 ВКЛ' if enabled else '🔴 ВЫКЛ'}", "src_tgl_state")],
        [("🛒 Заказы", "src_cat_orders")],
        [("🔔 Уведомления", "src_cat_notif")],
        [("📦 Очередь и лимиты", "src_cat_queue")],
        [("🎁 Управление гифтами", "src_gifts")],
        [("🧰 Обслуживание", "src_cat_maint")],
        [("◀️ Назад", "src_settings_panel")],
    ]
    return _make_kb(rows)

def _open_mini_settings(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    text = _mini_settings_text(chat_id)
    kb = _mini_settings_kb(chat_id)
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _notif_menu_text():
    cfg = _load_config()
    n_ord = cfg.get("notify_tg_orders", True)
    n_suc = cfg.get("notify_tg_success", True)
    n_err = cfg.get("notify_tg_errors", True)
    return (
        "🔔 <b>Настройки уведомлений в Telegram</b>\n\n"
        "Настройте события, о которых плагин присылает уведомления вам в Telegram:\n\n"
        f"• Новые заказы: <b>{'🟢 ВКЛ' if n_ord else '🔴 ВЫКЛ'}</b>\n"
        "  <i>(Оповещение при получении нового заказа и запросе данных у покупателя)</i>\n\n"
        f"• Успешная смена: <b>{'🟢 ВКЛ' if n_suc else '🔴 ВЫКЛ'}</b>\n"
        "  <i>(Оповещение об успешной смене региона и активации гифт-карты)</i>\n\n"
        f"• Ошибки и возвраты: <b>{'🟢 ВКЛ' if n_err else '🔴 ВЫКЛ'}</b>\n"
        "  <i>(Оповещение о неудачной смене, пополненном кошельке или автовозврате)</i>\n\n"
        "Нажмите на кнопку для переключения:"
    )

def _notif_menu_kb():
    cfg = _load_config()
    n_ord = cfg.get("notify_tg_orders", True)
    n_suc = cfg.get("notify_tg_success", True)
    n_err = cfg.get("notify_tg_errors", True)
    rows = [
        [(f"🛒 Новые заказы: {'🟢 ВКЛ' if n_ord else '🔴 ВЫКЛ'}", "src_tgl_notif_orders")],
        [(f"✅ Успешная смена: {'🟢 ВКЛ' if n_suc else '🔴 ВЫКЛ'}", "src_tgl_notif_success")],
        [(f"⚠️ Ошибки и возвраты: {'🟢 ВКЛ' if n_err else '🔴 ВЫКЛ'}", "src_tgl_notif_errors")],
        [("◀️ Назад", "src_mini_settings")],
    ]
    return _make_kb(rows)

def _open_notif_menu(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    text = _notif_menu_text()
    kb = _notif_menu_kb()
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _orders_menu_text():
    cfg = _load_config()
    auto_refund = cfg.get("auto_refund_enabled", True)
    auto_deact = cfg.get("auto_deactivate_lots", True)
    check_fund = cfg.get("check_steam_funding", True)
    return (
        "🛒 <b>Настройки заказов</b>\n\n"
        f"• Автовозврат: <b>{'🟢 ВКЛ' if auto_refund else '🔴 ВЫКЛ'}</b>\n"
        "  <i>(Если у покупателя уже были пополнения Steam или смена невозможна — автоматический возврат средств)</i>\n\n"
        f"• Защита от пополнений: <b>{'🟢 ВКЛ' if check_fund else '🔴 ВЫКЛ'}</b>\n"
        "  <i>(Проверка истории покупок и кошелька Steam перед сменой региона)</i>\n\n"
        f"• Автодеактивация лотов: <b>{'🟢 ВКЛ' if auto_deact else '🔴 ВЫКЛ'}</b>\n"
        "  <i>(Если у лота закончились рабочие прокси — лот автоматически деактивируется на FunPay)</i>\n\n"
        "Выберите параметр для переключения:"
    )

def _orders_menu_kb():
    cfg = _load_config()
    auto_refund = cfg.get("auto_refund_enabled", True)
    auto_deact = cfg.get("auto_deactivate_lots", True)
    check_fund = cfg.get("check_steam_funding", True)
    rows = [
        [(f"💸 Автовозврат: {'🟢 ВКЛ' if auto_refund else '🔴 ВЫКЛ'}", "src_tgl_refund")],
        [(f"🛡 Защита от пополнений: {'🟢 ВКЛ' if check_fund else '🔴 ВЫКЛ'}", "src_tgl_funding")],
        [(f"🛑 Автодеактивация лотов: {'🟢 ВКЛ' if auto_deact else '🔴 ВЫКЛ'}", "src_tgl_deact")],
        [("💬 Шаблоны сообщений", "src_cat_messages")],
        [("◀️ Назад", "src_mini_settings")],
    ]
    return _make_kb(rows)

def _open_orders_menu(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    text = _orders_menu_text()
    kb = _orders_menu_kb()
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _messages_menu_text():
    return (
        "💬 <b>Шаблоны сообщений покупателям</b>\n\n"
        "Здесь вы можете изменить тексты сообщений, которые плагин отправляет покупателям в чат FunPay при обработке заказа.\n\n"
        "Выберите сообщение для настройки:"
    )

def _messages_menu_kb():
    rows = []
    for key, label in BUYER_MESSAGE_LABELS.items():
        rows.append([(f"✏️ {label}", f"src_msg_view:{key}")])
    rows.append([("🔄 Сбросить все шаблоны", "src_msg_reset_all")])
    rows.append([("◀️ Назад", "src_cat_orders")])
    return _make_kb(rows)

def _open_messages_menu(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    text = _messages_menu_text()
    kb = _messages_menu_kb()
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _open_msg_editor(chat_id, key, message_id=None):
    _waiting[chat_id] = {"action": "edit_buyer_message", "key": key, "prompt_id": message_id}
    label = BUYER_MESSAGE_LABELS.get(key, key)
    cur = _get_buyer_messages().get(key, "")
    hint = BUYER_MESSAGE_HINTS.get(key, "")
    safe_cur = html.escape(cur)
    text = (
        f"💬 <b>Редактирование сообщения</b>\n"
        f"<b>Тип:</b> {label}\n\n"
        f"<b>Текущий текст:</b>\n"
        f"<pre>{safe_cur}</pre>\n\n"
        f"💡 {hint}\n\n"
        "Отправьте новый текст сообщением в чат или выберите действие ниже:"
    )
    kb = _make_kb([
        [("🔄 Сбросить на стандартный", f"src_msg_reset_one:{key}")],
        [("◀️ К списку сообщений", "src_cat_messages")],
    ])
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _queue_menu_text():
    cfg = _load_config()
    workers = cfg.get("max_workers", 5)
    timeout = cfg.get("request_timeout", 30.0)
    delay = cfg.get("rate_limit_delay", 30)
    return (
        "📦 <b>Очередь и лимиты</b>\n\n"
        f"• Одновременных замен аккаунтов: <b>{workers}</b>\n"
        f"• Таймаут запросов к Steam: <b>{int(timeout)} сек</b>\n"
        f"• Задержка при Steam rate-limit: <b>{delay} сек</b>\n\n"
        "Здесь настраивается многопоточность для смены региона и сетевые таймауты запросов через прокси."
    )

def _queue_menu_kb():
    cfg = _load_config()
    workers = cfg.get("max_workers", 5)
    timeout = cfg.get("request_timeout", 30.0)
    rows = [
        [(f"⚡️ Одновременных смен: {workers}", "src_cycle_workers")],
        [(f"⏱ Таймаут запросов: {int(timeout)}с", "src_cycle_timeout")],
        [("◀️ Назад", "src_mini_settings")],
    ]
    return _make_kb(rows)

def _open_queue_menu(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    text = _queue_menu_text()
    kb = _queue_menu_kb()
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _gifts_menu_text():
    cfg = _load_config()
    redeem = cfg.get("auto_redeem_gift", True)
    codes = _load_gift_codes()
    preview = []
    for i, c in enumerate(codes[:5], 1):
        masked = f"{c[:4]}-*****-{c[-4:]}" if len(c) >= 12 else _mask_secret(c)
        preview.append(f"{i}. <code>{masked}</code>")
    preview_str = "\n".join(preview) if preview else "<i>(база пуста)</i>"
    more = f"\n<i>... и ещё {len(codes) - 5} кодов</i>" if len(codes) > 5 else ""

    return (
        "🎁 <b>Управление гифтами Steam Wallet</b>\n\n"
        f"• Активация гифта при смене: <b>{'🟢 ВКЛ' if redeem else '🔴 ВЫКЛ'}</b>\n"
        f"• Всего кодов в базе: <b>{len(codes)} шт.</b>\n\n"
        f"<b>Доступные коды:</b>\n{preview_str}{more}"
    )

def _gifts_menu_kb():
    cfg = _load_config()
    redeem = cfg.get("auto_redeem_gift", True)
    codes = _load_gift_codes()
    rows = [
        [(f"🎁 Активация гифта: {'🟢 ВКЛ' if redeem else '🔴 ВЫКЛ'}", "src_tgl_redeem")],
        [("➕ Добавить ключи (текст / .txt)", "src_input_gifts")],
    ]
    if codes:
        rows.append([("🗑 Очистить базу гифтов", "src_clear_gifts_ask")])
    rows.append([("◀️ Назад", "src_mini_settings")])
    return _make_kb(rows)

def _menu_gifts(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    text = _gifts_menu_text()
    kb = _gifts_menu_kb()
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _proxies_menu_text():
    proxies = _load_proxies()
    preview = []
    for i, p in enumerate(proxies[:5], 1):
        clean_p = re.sub(r':([^@:]+)@', ':***@', p)
        preview.append(f"{i}. <code>{clean_p}</code>")
    preview_str = "\n".join(preview) if preview else "<i>(список пуст)</i>"
    more = f"\n<i>... и ещё {len(proxies) - 5} прокси</i>" if len(proxies) > 5 else ""

    return (
        "🌐 <b>Управление общей базой прокси</b>\n\n"
        f"• Всего прокси в пуле: <b>{len(proxies)} шт.</b>\n"
        f"• Файл: <code>{PROXIES_FILE}</code>\n\n"
        f"<b>Текущие прокси:</b>\n{preview_str}{more}\n\n"
        "💡 Поддерживаются форматы (HTTP/HTTPS):\n"
        "• <code>ip:port</code>\n"
        "• <code>ip:port:user:pass</code>\n"
        "• <code>http://user:pass@ip:port</code>"
    )

def _proxies_menu_kb():
    rows = [
        [("➕ Добавить прокси текстом", "src_input_proxies")],
        [("🧪 Тестировать прокси", "src_test_proxies"), ("🗑 Очистить базу", "src_clear_proxies_ask")],
        [("◀️ Назад", "src_mini_settings")],
    ]
    return _make_kb(rows)

def _menu_proxies(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    text = _proxies_menu_text()
    kb = _proxies_menu_kb()
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _maint_menu_text():
    return (
        "🧰 <b>Обслуживание плагина</b>\n\n"
        "Здесь вы можете управлять конфигурацией плагина или скачать файлы логов:\n\n"
        "• <b>Конфигурация</b> — резервное копирование и восстановление настроек (settings.json).\n"
        "• <b>Скачать логи</b> — выгрузка лог-файлов Cardinal и плагина."
    )

def _maint_menu_kb():
    rows = [
        [("⚙️ Конфигурация", "src_maint_cfg_menu")],
        [("📄 Скачать логи", "src_maint_download_logs")],
        [("◀️ Назад", "src_mini_settings")],
    ]
    return _make_kb(rows)

def _open_maint_menu(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    text = _maint_menu_text()
    kb = _maint_menu_kb()
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _maint_cfg_menu_text():
    cfg = _load_config()
    lots = _get_lots_dict(cfg)
    return (
        "⚙️ <b>Управление конфигурацией</b>\n\n"
        f"• Файл: <code>{CONFIG_FILE}</code>\n"
        f"• Привязано лотов: <b>{len(lots)} шт.</b>\n\n"
        "Выберите действие:"
    )

def _maint_cfg_menu_kb():
    rows = [
        [("💾 Скачать конфиг (.json)", "src_maint_export_cfg")],
        [("📥 Импортировать конфиг (.json)", "src_maint_import_cfg")],
        [("◀️ Назад", "src_cat_maint")],
    ]
    return _make_kb(rows)

def _open_maint_cfg_menu(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    text = _maint_cfg_menu_text()
    kb = _maint_cfg_menu_kb()
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

LOTS_PER_PAGE = 5

def _open_lots_menu(chat_id, message_id=None, page: int = 0):
    _waiting.pop(chat_id, None)
    cfg = _load_config()
    lots = _get_lots_dict(cfg)
    total_lots = len(lots)
    active_lots = sum(1 for l in lots.values() if l.get("enabled", False))

    text = (
        f"⭐ <b>Настройка лотов {NAME}</b>\n\n"
        f"• Всего лотов в плагине: <b>{total_lots} шт.</b>\n"
        f"• Активных: <b>🟢 {active_lots}</b> | Выключенных: <b>🔴 {total_lots - active_lots}</b>"
    )

    rows: list[list[tuple[str, str]]] = [
        [("🔄 Автодобавление (кат. 2044)", "src_lots_auto_2044"), ("➕ Добавить вручную", "src_lot_add_wizard")],
    ]
    lot_items = list(lots.values())

    total_pages = max(1, (total_lots + LOTS_PER_PAGE - 1) // LOTS_PER_PAGE) if total_lots > 0 else 1
    page = max(0, min(page, total_pages - 1))

    start = page * LOTS_PER_PAGE
    end = start + LOTS_PER_PAGE
    page_lots = lot_items[start:end]

    for l in page_lots:
        lid = str(l["id"])
        st_ico = "🟢" if l.get("enabled", False) else "🔴"
        cc = l.get("country", "KZ")
        px_cnt = len(l.get("proxies") or [])
        lbl = f"{st_ico} #{lid} · {_country_display(cc)} ({px_cnt} px)"
        rows.append([(lbl, f"src_lot_card:{lid}")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(("◀️ Пред.", f"src_lots_page:{page - 1}"))
        nav.append((f"Стр {page + 1}/{total_pages}", "src_lots_noop"))
        if page < total_pages - 1:
            nav.append(("След. ▶️", f"src_lots_page:{page + 1}"))
        rows.append(nav)

    rows.append([("◀️ Назад", "src_settings_panel")])

    kb = _make_kb(rows)
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _open_lot_card(chat_id, lot_id: str, message_id=None):
    _waiting.pop(chat_id, None)
    cfg = _load_config()
    lots = _get_lots_dict(cfg)
    lot = lots.get(str(lot_id))
    if not lot:
        _tg_send(chat_id, f"⚠️ Лот #{lot_id} не найден.")
        _open_lots_menu(chat_id, message_id)
        return

    title = lot.get("title", f"Лот {lot_id}")
    enabled = lot.get("enabled", True)
    country = lot.get("country", "KZ")
    proxies = lot.get("proxies", [])

    text = (
        f"📦 <b>Настройки лота #{lot_id}</b>\n\n"
        f"• Название: <i>{title}</i>\n"
        f"• Состояние: <b>{'🟢 Включён' if enabled else '🔴 Выключен'}</b>\n"
        f"• Целевая страна: <b>{_country_display(country)}</b> (<code>{country}</code>)\n"
        f"• Привязано прокси: <b>{len(proxies)} шт.</b>"
    )

    rows = [
        [(f"🧩 Состояние: {'🟢 ВКЛ' if enabled else '🔴 ВЫКЛ'}", f"src_lot_tgl:{lot_id}")],
        [(f"🌍 Изменить страну ({country})", f"src_lot_change_cc:{lot_id}")],
        [(f"🌐 Настройка прокси · {len(proxies)} шт.", f"src_lot_px_menu:{lot_id}")],
        [("🗑 Удалить лот из плагина", f"src_lot_delete_ask:{lot_id}")],
        [("◀️ К списку лотов", "src_lots_menu")],
    ]
    kb = _make_kb(rows)
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _lot_proxies_menu_text(lot_id: str):
    cfg = _load_config()
    lots = _get_lots_dict(cfg)
    lot = lots.get(str(lot_id)) or {}
    title = lot.get("title", f"Лот {lot_id}")
    country = lot.get("country", "KZ")
    proxies = lot.get("proxies", [])

    preview = []
    for i, p in enumerate(proxies[:5], 1):
        clean_p = re.sub(r':([^@:]+)@', ':***@', p)
        preview.append(f"{i}. <code>{clean_p}</code>")
    px_preview = "\n".join(preview) if preview else "<i>(нет привязанных прокси — добавьте для работы смены региона!)</i>"
    if len(proxies) > 5:
        px_preview += f"\n<i>... и ещё {len(proxies) - 5} прокси</i>"

    return (
        f"🌐 <b>Настройка прокси лота #{lot_id}</b>\n\n"
        f"• Лот: <i>{title}</i>\n"
        f"• Страна: <b>{_country_display(country)}</b> (<code>{country}</code>)\n"
        f"• Всего прокси: <b>{len(proxies)} шт.</b>\n\n"
        f"<b>Текущие прокси:</b>\n{px_preview}\n\n"
        "💡 <i>Вы можете добавить прокси текстом или файлом .txt, проверить их доступность или экспортировать.</i>"
    )

def _lot_proxies_menu_kb(lot_id: str):
    cfg = _load_config()
    lots = _get_lots_dict(cfg)
    proxies = (lots.get(str(lot_id)) or {}).get("proxies", [])
    rows = [
        [("➕ Добавить прокси (текст / .txt)", f"src_lot_add_px:{lot_id}")],
    ]
    if proxies:
        rows.append([("🧪 Тестировать прокси", f"src_lot_test_px:{lot_id}"), ("🗑 Очистить прокси", f"src_lot_clear_px:{lot_id}")])
    rows.append([("◀️ Назад к лоту", f"src_lot_card:{lot_id}")])
    return _make_kb(rows)

def _open_lot_proxies_menu(chat_id, lot_id: str, message_id=None):
    _waiting.pop(chat_id, None)
    text = _lot_proxies_menu_text(lot_id)
    kb = _lot_proxies_menu_kb(lot_id)
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _open_lot_change_cc(chat_id, lot_id: str, message_id=None):
    _waiting.pop(chat_id, None)
    cfg = _load_config()
    lots = _get_lots_dict(cfg)
    lot = lots.get(str(lot_id)) or {}
    cur_cc = lot.get("country", "KZ")

    text = (
        f"🌍 <b>Выбор страны для лота #{lot_id}</b>\n\n"
        f"Текущая страна: <b>{_country_display(cur_cc)}</b> (<code>{cur_cc}</code>)\n\n"
        "Выберите страну из списка или отправьте двухбуквенный код текстом:"
    )
    rows = [
        [("🇰🇿 Казахстан", f"src_lot_set_cc:{lot_id}:KZ"), ("🇺🇸 США", f"src_lot_set_cc:{lot_id}:US")],
        [("🇺🇦 Украина", f"src_lot_set_cc:{lot_id}:UA"), ("🇷🇺 Россия", f"src_lot_set_cc:{lot_id}:RU")],
        [("🇹🇷 Турция", f"src_lot_set_cc:{lot_id}:TR"), ("🇦🇷 Аргентина", f"src_lot_set_cc:{lot_id}:AR")],
        [("✍️ Ввести свой код", f"src_lot_input_cc:{lot_id}")],
        [("◀️ Назад", f"src_lot_card:{lot_id}")],
    ]
    kb = _make_kb(rows)
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _test_lot_proxies_worker(chat_id, lot_id: str, message_id=None):
    cfg = _load_config()
    lots = _get_lots_dict(cfg)
    lot = lots.get(str(lot_id)) or {}
    proxies = lot.get("proxies", [])
    if not proxies:
        _tg_edit(chat_id, message_id, f"⚠️ У лота #{lot_id} нет привязанных прокси.", _make_kb([[("◀️ Назад", f"src_lot_px_menu:{lot_id}")]]))
        return

    _tg_edit(chat_id, message_id, f"⏳ <b>Тестирование {len(proxies)} прокси лота #{lot_id}...</b>\nПожалуйста, подождите.", _make_kb([[("◀️ Назад", f"src_lot_px_menu:{lot_id}")]]))
    _log_event("lot_proxy_test_start", lot_id=lot_id, count=len(proxies))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async def _test():
        details = []
        alive = 0
        dead = 0
        sem = asyncio.Semaphore(5)
        async def _check(p):
            nonlocal alive, dead
            clean_p = _mask_proxy(p)
            async with sem:
                ok, diag = await check_proxy(p, timeout=8.0)
                if ok:
                    alive += 1
                    details.append(f"🟢 <code>{clean_p}</code> → {diag} (Steam OK)")
                    _log_event("lot_proxy_test", status="ok", lot_id=lot_id, proxy=clean_p, diag=diag)
                else:
                    dead += 1
                    details.append(f"🔴 <code>{clean_p}</code> → {diag}")
                    _log_event("lot_proxy_test", level=logging.WARNING, status="fail", lot_id=lot_id, proxy=clean_p, diag=diag)
        await asyncio.gather(*[_check(p) for p in proxies])
        return alive, dead, details

    try:
        alive, dead, details = loop.run_until_complete(_test())
        _log_event("lot_proxy_summary", lot_id=lot_id, total=len(proxies), alive=alive, dead=dead)
        det_str = "\n".join(details[:10])
        if len(details) > 10:
            det_str += f"\n<i>... и ещё {len(details) - 10} прокси</i>"
        res_text = (
            f"🧪 <b>Результаты тестирования прокси лота #{lot_id}:</b>\n\n"
            f"• Всего проверено: <b>{len(proxies)}</b>\n"
            f"• Живые (Steam доступен): <b>🟢 {alive}</b>\n"
            f"• Недоступны / ошибки: <b>🔴 {dead}</b>\n\n"
            f"<b>Статус по прокси:</b>\n{det_str}"
        )
    except Exception as e:
        _log_event("lot_proxy_test_error", level=logging.ERROR, lot_id=lot_id, err=str(e))
        res_text = f"❌ Ошибка проверки: {e}"
    finally:
        loop.close()

    _tg_edit(chat_id, message_id, res_text, _make_kb([[("🔄 Проверить снова", f"src_lot_test_px:{lot_id}"), ("◀️ К прокси лота", f"src_lot_px_menu:{lot_id}")]]))

def _handle_auto_lots_2044(chat_id, message_id=None):
    if not cardinal_instance or not getattr(cardinal_instance, 'account', None):
        _tg_edit(chat_id, message_id, "⚠️ Аккаунт FunPay в Cardinal не инициализирован. Убедитесь, что Cardinal запущен и авторизован в FunPay.", _make_kb([[("◀️ Назад", "src_lots_menu")]]))
        return

    _tg_edit(chat_id, message_id, "⏳ <b>Поиск ваших лотов в категории 2044...</b>", _make_kb([[("◀️ Назад", "src_lots_menu")]]))

    def _worker():
        try:
            account = cardinal_instance.account
            found_lots = []
            try:
                found_lots = account.get_my_subcategory_lots(2044)
            except Exception as e:
                logger.warning(f"[{NAME}] get_my_subcategory_lots(2044): {e}")

            if not found_lots and getattr(account, 'id', None):
                try:
                    profile = account.get_user(int(account.id))
                    if profile:
                        for lot in profile.get_lots():
                            sub = getattr(lot, 'subcategory', None)
                            if sub and getattr(sub, 'id', None) == 2044:
                                found_lots.append(lot)
                except Exception as e:
                    logger.warning(f"[{NAME}] get_user lots fallback: {e}")

            if not found_lots:
                _tg_edit(chat_id, message_id, "ℹ️ В категории 2044 не найдено ваших лотов. Создайте лот на FunPay или добавьте лот вручную по ID.", _make_kb([[("➕ Добавить вручную", "src_lot_add_wizard"), ("◀️ Назад", "src_lots_menu")]]))
                return

            cfg = _load_config()
            lots = _get_lots_dict(cfg)
            added_cnt = 0
            already_cnt = 0

            for l in found_lots:
                lid = str(getattr(l, 'id', '') or '')
                if not lid:
                    continue
                if lid in lots:
                    already_cnt += 1
                    continue

                title = str(getattr(l, 'description', None) or getattr(l, 'title', None) or f"Лот {lid}")
                t_low = title.lower()
                cc = "KZ"
                if any(k in t_low for k in ["казахстан", "тенге", " kz", "(kz)"]):
                    cc = "KZ"
                elif any(k in t_low for k in ["турци", "лир", " tr", "(tr)"]):
                    cc = "TR"
                elif any(k in t_low for k in ["украин", "гривн", " ua", "(ua)"]):
                    cc = "UA"
                elif any(k in t_low for k in ["росси", "рубл", " ru", "(ru)"]):
                    cc = "RU"
                elif any(k in t_low for k in ["сша", "доллар", " us", "(us)", "usa"]):
                    cc = "US"
                elif any(k in t_low for k in ["аргентин", "песо", " ar", "(ar)"]):
                    cc = "AR"

                lots[lid] = {
                    "id": lid,
                    "title": title,
                    "enabled": False,
                    "country": cc,
                    "proxies": [],
                }
                added_cnt += 1

            cfg["lots"] = lots
            _save_config(cfg)

            msg = (
                f"✅ <b>Автодобавление лотов (кат. 2044) завершено!</b>\n\n"
                f"• Найдено лотов: <b>{len(found_lots)} шт.</b>\n"
                f"• Добавлено новых: <b>{added_cnt} шт.</b>\n"
                f"• Уже было в плагине: <b>{already_cnt} шт.</b>"
            )
            _tg_edit(chat_id, message_id, msg, _make_kb([[("⭐ Открыть список лотов", "src_lots_menu")]]))
        except Exception as e:
            logger.error(f"[{NAME}] Ошибка автодобавления лотов: {e}", exc_info=True)
            _tg_edit(chat_id, message_id, f"❌ Ошибка сканирования: {e}", _make_kb([[("◀️ Назад", "src_lots_menu")]]))

    threading.Thread(target=_worker, daemon=True).start()

def _start_lot_wizard(chat_id, message_id=None):
    _waiting[chat_id] = {"action": "wiz_step1_id", "prompt_id": message_id}
    text = (
        "➕ <b>Добавление лота · Шаг 1 из 3</b>\n\n"
        "Введите числовой ID лота FunPay (например: <code>12345678</code>):"
    )
    kb = _make_kb([[("❌ Отмена", "src_lots_menu")]])
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _lot_wizard_ask_country(chat_id, lot_id: str, title: str, message_id=None):
    _waiting[chat_id] = {"action": "wiz_step2_country", "lot_id": lot_id, "title": title, "prompt_id": message_id}
    text = (
        f"🌍 <b>Добавление лота · Шаг 2 из 3</b>\n\n"
        f"Лот: <code>{lot_id}</code>\n"
        f"Название: <i>{title}</i>\n\n"
        "Выберите целевую страну или отправьте её 2-буквенный код текстом (например: <code>RU</code>):"
    )
    rows = [
        [("🇰🇿 Казахстан", "src_wiz_cc:KZ"), ("🇺🇸 США", "src_wiz_cc:US")],
        [("🇺🇦 Украина", "src_wiz_cc:UA"), ("🇷🇺 Россия", "src_wiz_cc:RU")],
        [("🇹🇷 Турция", "src_wiz_cc:TR"), ("🇦🇷 Аргентина", "src_wiz_cc:AR")],
        [("❌ Отмена", "src_lots_menu")],
    ]
    kb = _make_kb(rows)
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _lot_wizard_ask_proxy(chat_id, lot_id: str, title: str, cc: str, message_id=None):
    _waiting[chat_id] = {"action": "wiz_step3_proxy", "lot_id": lot_id, "title": title, "country": cc, "prompt_id": message_id}
    text = (
        f"🌐 <b>Добавление лота · Шаг 3 из 3</b>\n\n"
        f"Лот: <code>{lot_id}</code> | Страна: <b>{_country_display(cc)}</b>\n\n"
        f"Для работы лота необходим прокси под целевой регион.\n"
        f"Отправьте прокси текстом (каждый с новой строки) или загрузите файл <code>.txt</code>:\n\n"
        "<b>Поддерживаемые форматы (HTTP/HTTPS):</b>\n"
        "• <code>ip:port</code>\n"
        "• <code>ip:port:login:password</code>\n"
        "• <code>http://ip:port</code>\n"
        "• <code>http://login:password@ip:port</code>"
    )
    kb = _make_kb([[("❌ Отмена", "src_lots_menu")]])
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _test_proxies_worker(chat_id, message_id=None):
    proxies = _load_proxies()
    if not proxies:
        _tg_edit(chat_id, message_id, "⚠️ Список прокси пуст. Добавьте прокси для проверки.", _make_kb([[("◀️ Назад", "src_proxies")]]))
        return

    _tg_edit(chat_id, message_id, f"⏳ <b>Тестирование {len(proxies)} прокси...</b>\nПожалуйста, подождите.", _make_kb([[("◀️ Назад", "src_proxies")]]))
    _log_event("pool_proxy_test_start", count=len(proxies))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async def _test_all():
        details = []
        alive = 0
        dead = 0
        sem = asyncio.Semaphore(10)
        async def _check(p):
            nonlocal alive, dead
            clean_p = _mask_proxy(p)
            async with sem:
                ok, diag = await check_proxy(p, timeout=8.0)
                if ok:
                    alive += 1
                    details.append(f"🟢 <code>{clean_p}</code> → {diag} (Steam OK)")
                    _log_event("pool_proxy_test", status="ok", proxy=clean_p, diag=diag)
                else:
                    dead += 1
                    details.append(f"🔴 <code>{clean_p}</code> → {diag}")
                    _log_event("pool_proxy_test", level=logging.WARNING, status="fail", proxy=clean_p, diag=diag)
        await asyncio.gather(*[_check(p) for p in proxies])
        return alive, dead, details

    try:
        alive, dead, details = loop.run_until_complete(_test_all())
        _log_event("pool_proxy_summary", total=len(proxies), alive=alive, dead=dead)
        det_str = "\n".join(details[:10])
        if len(details) > 10:
            det_str += f"\n<i>... и ещё {len(details) - 10} прокси</i>"
        res_text = (
            f"🧪 <b>Результаты тестирования общей базы:</b>\n\n"
            f"• Проверено: <b>{len(proxies)}</b>\n"
            f"• Живые (Steam доступен): <b>🟢 {alive}</b>\n"
            f"• Недоступны / таймаут: <b>🔴 {dead}</b>\n\n"
            f"<b>Статус по прокси:</b>\n{det_str}"
        )
    except Exception as e:
        _log_event("pool_proxy_test_error", level=logging.ERROR, err=str(e))
        res_text = f"❌ Ошибка проверки прокси: {e}"
    finally:
        loop.close()

    kb = _make_kb([[("🔄 Проверить снова", "src_test_proxies"), ("◀️ К прокси", "src_proxies")]])
    _tg_edit(chat_id, message_id, res_text, kb)

def _stats_text():
    stats = _load_stats()
    return (
        f"📊 <b>Статистика {NAME}</b>\n\n"
        f"• Всего попыток: <b>{stats.get('total_operations', 0)}</b>\n"
        f"• Успешно сменено: <b>🟢 {stats.get('success', 0)}</b>\n"
        f"• Ошибок / отмен: <b>🔴 {stats.get('failed', 0)}</b>\n"
        f"• Активировано гифтов: <b>🎁 {stats.get('gifts_redeemed', 0)}</b>\n"
        f"• Последняя операция: <code>{stats.get('last_operation', 'нет данных')}</code>"
    )

def _stats_kb():
    return _make_kb([
        [("🔄 Сбросить статистику", "src_stats_reset")],
        [("◀️ Назад", "src_settings_panel")],
    ])

def _open_stats(chat_id, message_id=None):
    _waiting.pop(chat_id, None)
    text = _stats_text()
    kb = _stats_kb()
    _tg_edit(chat_id, message_id, text, kb) if message_id else _tg_send(chat_id, text, kb)

def _plugin_version_key(value):
    nums = [int(x) for x in re.findall(r'\d+', str(value or ''))[:4]]
    nums.extend([0] * (4 - len(nums)))
    return tuple(nums[:4])

def _online_update_worker(chat_id, message_id=None):
    _tg_edit(chat_id, message_id, "⏳ <b>Проверка обновлений на GitHub...</b>", _make_kb([[("◀️ Назад", "src_update_menu")]]))
    try:
        resp = requests.get(GITHUB_UPDATE_URL, timeout=15)
        if resp.status_code != 200 or not resp.content:
            _tg_edit(chat_id, message_id, f"⚠️ Не удалось загрузить обновление с GitHub (код {resp.status_code}).\nПроверьте репозиторий: {GITHUB_URL}", _make_kb([[("◀️ Назад", "src_update_menu")]]))
            return
        source = resp.text
        m = re.search(r'(?m)^\s*VERSION\s*=\s*["\']([^"\']+)["\']', source)
        if not m:
            _tg_edit(chat_id, message_id, "❌ В файле на GitHub не найдено поле VERSION.", _make_kb([[("◀️ Назад", "src_update_menu")]]))
            return
        remote_version = m.group(1).strip()
        if _plugin_version_key(remote_version) <= _plugin_version_key(VERSION):
            _tg_edit(chat_id, message_id, f"✅ <b>У вас уже установлена актуальная версия {VERSION}.</b>", _make_kb([[("◀️ Назад", "src_update_menu")]]))
            return

        plugin_file = os.path.abspath(__file__)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        backup_file = plugin_file + f'.pre-online-update.{stamp}.bak'
        shutil.copy2(plugin_file, backup_file)

        with open(plugin_file, "w", encoding="utf-8") as f:
            f.write(source)

        text = f"✅ <b>Онлайн-обновление установлено: v{remote_version}!</b>\n\nРезервная копия сохранена в <code>{os.path.basename(backup_file)}</code>.\nВыполните <code>/restart</code> для применения."
        _tg_edit(chat_id, message_id, text, _make_kb([[("◀️ В меню", "src_home")]]))
    except Exception as e:
        _tg_edit(chat_id, message_id, f"❌ Ошибка обновления: {e}", _make_kb([[("◀️ Назад", "src_update_menu")]]))

def _self_delete(chat_id, message_id=None):
    errors = []
    try:
        shutil.rmtree(STORAGE_DIR, ignore_errors=True)
    except Exception as e:
        errors.append(f"данные: {e}")

    plugin_file = os.path.abspath(__file__)
    try:
        if os.path.exists(plugin_file):
            os.remove(plugin_file)
    except Exception as e:
        errors.append(f"файл: {e}")

    if not errors:
        text = f"✅ <b>Плагин {NAME} успешно удалён.</b>\n\nВыполните команду <code>/restart</code>."
    else:
        text = f"⚠️ <b>Плагин удалён с замечаниями:</b>\n" + "\n".join(errors)
    _tg_edit(chat_id, message_id, text, _make_kb([[("🔙 К списку плагинов", CB_PLUGINS_LIST_OPEN)]]))

def _cb_router(call):
    chat_id = getattr(getattr(getattr(call, 'message', None), 'chat', None), 'id', None)
    message_id = getattr(getattr(call, 'message', None), 'message_id', None)
    data = str(getattr(call, 'data', '') or '')
    from_user = getattr(call, 'from_user', None)
    user_id = getattr(from_user, 'id', None)

    if not chat_id or not _is_authorized(user_id):
        return

    try:
        bot_instance.answer_callback_query(call.id)
    except Exception:
        pass

    if data == "src_home":
        _open_home(chat_id, message_id)
    elif data == "src_info":
        _open_info(chat_id, message_id)
    elif data == "src_update_menu":
        _open_update_menu(chat_id, message_id)
    elif data == "src_settings_panel":
        _open_settings_panel(chat_id, message_id)
    elif data == "src_delete_ask":
        _open_delete_confirm(chat_id, message_id)
    elif data == "src_delete_yes":
        _self_delete(chat_id, message_id)

    elif data == "src_update_local":
        _waiting[chat_id] = {"action": "update_local", "prompt_id": message_id}
        text = (
            "📥 <b>Локальное обновление</b>\n\n"
            f"Пришлите новый файл <code>SRC-Plugin.py</code> с расширением <code>.py</code> в этот чат.\n"
            "После проверки файл заменит текущий плагин. Старый файл будет сохранён в резервной копии.\n\n"
            "Для отмены нажмите кнопку ниже."
        )
        _tg_edit(chat_id, message_id, text, _make_kb([[("❌ Отмена", "src_update_menu")]]))
    elif data == "src_update_online":
        threading.Thread(target=_online_update_worker, args=(chat_id, message_id), daemon=True).start()

    elif data == "src_mini_settings":
        _open_mini_settings(chat_id, message_id)
    elif data == "src_lots_menu":
        _open_lots_menu(chat_id, message_id)
    elif data.startswith("src_lots_page:"):
        p = int(data.split(":", 1)[1])
        _open_lots_menu(chat_id, message_id, page=p)
    elif data == "src_lots_noop":
        pass
    elif data == "src_stats":
        _open_stats(chat_id, message_id)

    elif data == "src_tgl_state":
        cfg = _load_config()
        cfg["plugin_enabled"] = not cfg.get("plugin_enabled", True)
        _save_config(cfg)
        _open_mini_settings(chat_id, message_id)
    elif data == "src_cat_orders":
        _open_orders_menu(chat_id, message_id)
    elif data == "src_tgl_refund":
        cfg = _load_config()
        cfg["auto_refund_enabled"] = not cfg.get("auto_refund_enabled", True)
        _save_config(cfg)
        _open_orders_menu(chat_id, message_id)
    elif data == "src_tgl_funding":
        cfg = _load_config()
        cfg["check_steam_funding"] = not cfg.get("check_steam_funding", True)
        _save_config(cfg)
        _open_orders_menu(chat_id, message_id)
    elif data == "src_tgl_deact":
        cfg = _load_config()
        cfg["auto_deactivate_lots"] = not cfg.get("auto_deactivate_lots", True)
        _save_config(cfg)
        _open_orders_menu(chat_id, message_id)
    elif data == "src_cat_notif":
        _open_notif_menu(chat_id, message_id)
    elif data == "src_tgl_notif_orders":
        cfg = _load_config()
        cfg["notify_tg_orders"] = not cfg.get("notify_tg_orders", True)
        _save_config(cfg)
        _open_notif_menu(chat_id, message_id)
    elif data == "src_tgl_notif_success":
        cfg = _load_config()
        cfg["notify_tg_success"] = not cfg.get("notify_tg_success", True)
        _save_config(cfg)
        _open_notif_menu(chat_id, message_id)
    elif data == "src_tgl_notif_errors":
        cfg = _load_config()
        cfg["notify_tg_errors"] = not cfg.get("notify_tg_errors", True)
        _save_config(cfg)
        _open_notif_menu(chat_id, message_id)
    elif data == "src_cat_messages":
        _open_messages_menu(chat_id, message_id)
    elif data.startswith("src_msg_view:"):
        key = data.split(":", 1)[1]
        _open_msg_editor(chat_id, key, message_id)
    elif data.startswith("src_msg_reset_one:"):
        key = data.split(":", 1)[1]
        cfg = _load_config()
        bm = cfg.get("buyer_messages") or {}
        if key in bm:
            bm.pop(key, None)
            cfg["buyer_messages"] = bm
            _save_config(cfg)
        _open_msg_editor(chat_id, key, message_id)
    elif data == "src_msg_reset_all":
        cfg = _load_config()
        cfg["buyer_messages"] = dict(DEFAULT_BUYER_MESSAGES)
        _save_config(cfg)
        _open_messages_menu(chat_id, message_id)
    elif data == "src_cat_queue":
        _open_queue_menu(chat_id, message_id)
    elif data in ("src_cycle_workers", "src_set_workers"):
        _waiting[chat_id] = {"action": "input_workers", "prompt_id": message_id}
        cfg = _load_config()
        cur = cfg.get("max_workers", 5)
        text = (
            f"⚡ <b>Количество одновременных смен</b>\n\n"
            f"Текущее значение: <b>{cur}</b>\n\n"
            "Введите желаемое число параллельных замен аккаунтов (например: <code>5</code> или <code>10</code>):"
        )
        _tg_edit(chat_id, message_id, text, _make_kb([[("❌ Отмена", "src_cat_queue")]]))
    elif data in ("src_cycle_timeout", "src_set_timeout"):
        _waiting[chat_id] = {"action": "input_timeout", "prompt_id": message_id}
        cfg = _load_config()
        cur = int(cfg.get("request_timeout", 30.0))
        text = (
            f"⏱ <b>Таймаут сетевых запросов</b>\n\n"
            f"Текущее значение: <b>{cur} сек</b>\n\n"
            "Введите таймаут запросов в секундах (например: <code>30</code> или <code>60</code>):"
        )
        _tg_edit(chat_id, message_id, text, _make_kb([[("❌ Отмена", "src_cat_queue")]]))
    elif data == "src_cat_maint":
        _open_maint_menu(chat_id, message_id)

    elif data == "src_maint_cfg_menu":
        _open_maint_cfg_menu(chat_id, message_id)
    elif data == "src_maint_export_cfg":
        if os.path.exists(CONFIG_FILE) and bot_instance:
            try:
                with open(CONFIG_FILE, "rb") as doc:
                    bot_instance.send_document(chat_id, doc, caption=f"📄 Конфигурация {NAME} (settings.json)")
            except Exception as e:
                _tg_send(chat_id, f"❌ Ошибка отправки файла: {e}")
    elif data == "src_maint_import_cfg":
        _waiting[chat_id] = {"action": "import_config", "prompt_id": message_id}
        text = "📥 <b>Отправьте файл settings.json или текст JSON в этот чат:</b>"
        _tg_edit(chat_id, message_id, text, _make_kb([[("❌ Отмена", "src_maint_cfg_menu")]]))
    elif data == "src_maint_download_logs":
        log_paths = [os.path.join(BASE_DIR, "logs", "cardinal.log"), os.path.join(BASE_DIR, "cardinal.log")]
        sent = False
        for lp in log_paths:
            if os.path.exists(lp) and bot_instance:
                try:
                    with open(lp, "rb") as doc:
                        bot_instance.send_document(chat_id, doc, caption=f"📄 Лог {NAME}")
                    sent = True
                    break
                except Exception:
                    pass
        if not sent:
            _tg_send(chat_id, "ℹ️ Файл логов пока не найден или пуст.")

    elif data == "src_gifts":
        _waiting.pop(chat_id, None)
        _menu_gifts(chat_id, message_id)
    elif data == "src_tgl_redeem":
        cfg = _load_config()
        cfg["auto_redeem_gift"] = not cfg.get("auto_redeem_gift", True)
        _save_config(cfg)
        _menu_gifts(chat_id, message_id)
    elif data == "src_input_gifts":
        _waiting[chat_id] = {"action": "input_gifts", "prompt_id": message_id}
        text = (
            "➕ <b>Добавление кодов Steam Wallet</b>\n\n"
            "Вы можете:\n"
            "1. Отправить список кодов текстом (каждый с новой строки)\n"
            "2. Загрузить текстовый файл <code>.txt</code> с кодами"
        )
        codes = _load_gift_codes()
        rows = []
        if codes:
            rows.append([("💾 Скачать базу ключей (.txt)", "src_export_gifts")])
        rows.append([("❌ Отмена", "src_gifts")])
        _tg_edit(chat_id, message_id, text, _make_kb(rows))
    elif data == "src_export_gifts":
        codes = _load_gift_codes()
        if not codes:
            _tg_send(chat_id, "ℹ️ База ключей пуста.")
        elif bot_instance:
            try:
                bio = io.BytesIO("\n".join(codes).encode('utf-8'))
                bio.name = "steam_gift_codes.txt"
                bot_instance.send_document(chat_id, bio, caption=f"🎁 База ключей Steam ({len(codes)} шт.)")
            except Exception as e:
                _tg_send(chat_id, f"❌ Ошибка отправки файла: {e}")
    elif data == "src_clear_gifts_ask":
        text = "⚠️ <b>Очистить все сохранённые гифт-коды?</b>"
        _tg_edit(chat_id, message_id, text, _make_kb([[("✅ Да, очистить", "src_clear_gifts_yes"), ("❌ Отмена", "src_gifts")]]))
    elif data == "src_clear_gifts_yes":
        _save_gift_codes([])
        _menu_gifts(chat_id, message_id)

    elif data == "src_proxies":
        _waiting.pop(chat_id, None)
        _menu_proxies(chat_id, message_id)
    elif data == "src_input_proxies":
        _waiting[chat_id] = {"action": "input_proxies", "prompt_id": message_id}
        text = (
            "➕ <b>Отправьте список прокси</b> (каждый с новой строки) или <b>.txt файл</b>:\n\n"
            "Форматы:\n"
            "• <code>ip:port</code>\n"
            "• <code>ip:port:user:pass</code>\n"
            "• <code>http://user:pass@ip:port</code>"
        )
        _tg_edit(chat_id, message_id, text, _make_kb([[("❌ Отмена", "src_proxies")]]))
    elif data == "src_clear_proxies_ask":
        text = "⚠️ <b>Очистить всю общую базу прокси?</b>"
        _tg_edit(chat_id, message_id, text, _make_kb([[("✅ Да, очистить", "src_clear_proxies_yes"), ("❌ Отмена", "src_proxies")]]))
    elif data == "src_clear_proxies_yes":
        _save_proxies([])
        _menu_proxies(chat_id, message_id)
    elif data == "src_test_proxies":
        threading.Thread(target=_test_proxies_worker, args=(chat_id, message_id), daemon=True).start()

    elif data == "src_lots_auto_2044":
        _handle_auto_lots_2044(chat_id, message_id)
    elif data == "src_lot_add_wizard":
        _start_lot_wizard(chat_id, message_id)
    elif data.startswith("src_wiz_cc:"):
        cc = data.split(":", 1)[1].upper()
        st = _waiting.get(chat_id) or {}
        lid = st.get("lot_id", "")
        title = st.get("title", f"Лот {lid}")
        _lot_wizard_ask_proxy(chat_id, lid, title, cc, message_id)
    elif data == "src_lots_clear_ask":
        text = "⚠️ <b>Очистить все привязанные лоты?</b>"
        _tg_edit(chat_id, message_id, text, _make_kb([[("✅ Да, очистить", "src_lots_clear_yes"), ("❌ Отмена", "src_lots_menu")]]))
    elif data == "src_lots_clear_yes":
        cfg = _load_config()
        cfg["lots"] = {}
        _save_config(cfg)
        _open_lots_menu(chat_id, message_id)

    elif data.startswith("src_lot_card:"):
        lid = data.split(":", 1)[1]
        _open_lot_card(chat_id, lid, message_id)
    elif data.startswith("src_lot_tgl:"):
        lid = data.split(":", 1)[1]
        cfg = _load_config()
        lots = _get_lots_dict(cfg)
        if lid in lots:
            new_st = not lots[lid].get("enabled", True)
            lots[lid]["enabled"] = new_st
            cfg["lots"] = lots
            _save_config(cfg)
            if cfg.get("auto_deactivate_lots", True):
                _set_funpay_lot_active(cardinal_instance, lid, new_st)
        _open_lot_card(chat_id, lid, message_id)
    elif data.startswith("src_lot_change_cc:"):
        lid = data.split(":", 1)[1]
        _open_lot_change_cc(chat_id, lid, message_id)
    elif data.startswith("src_lot_set_cc:"):
        parts = data.split(":")
        lid, cc = parts[1], parts[2].upper()
        cfg = _load_config()
        lots = _get_lots_dict(cfg)
        if lid in lots:
            lots[lid]["country"] = cc
            cfg["lots"] = lots
            _save_config(cfg)
        _open_lot_card(chat_id, lid, message_id)
    elif data.startswith("src_lot_input_cc:"):
        lid = data.split(":", 1)[1]
        _waiting[chat_id] = {"action": "lot_input_cc", "lot_id": lid, "prompt_id": message_id}
        text = f"✍️ <b>Введите двухбуквенный код страны для лота #{lid}</b> (например: <code>KZ</code>, <code>US</code>, <code>RU</code>):"
        _tg_edit(chat_id, message_id, text, _make_kb([[("❌ Отмена", f"src_lot_card:{lid}")]]))
    elif data.startswith("src_lot_px_menu:"):
        lid = data.split(":", 1)[1]
        _open_lot_proxies_menu(chat_id, lid, message_id)
    elif data.startswith("src_lot_add_px:"):
        lid = data.split(":", 1)[1]
        _waiting[chat_id] = {"action": "lot_add_px", "lot_id": lid, "prompt_id": message_id}
        text = (
            f"➕ <b>Добавление прокси для лота #{lid}</b>\n\n"
            "Вы можете:\n"
            "1. Отправить список прокси текстом (каждый с новой строки)\n"
            "2. Загрузить файл <code>.txt</code> со списком прокси\n\n"
            "<b>Форматы (HTTP/HTTPS):</b>\n"
            "• <code>ip:port</code>\n"
            "• <code>ip:port:user:pass</code>\n"
            "• <code>http://user:pass@ip:port</code>"
        )
        cfg = _load_config()
        lots = _get_lots_dict(cfg)
        proxies = (lots.get(str(lid)) or {}).get("proxies", [])
        rows = []
        if proxies:
            rows.append([("💾 Скачать текущие прокси (.txt)", f"src_lot_export_px:{lid}")])
        rows.append([("❌ Отмена", f"src_lot_px_menu:{lid}")])
        _tg_edit(chat_id, message_id, text, _make_kb(rows))
    elif data.startswith("src_lot_export_px:"):
        lid = data.split(":", 1)[1]
        cfg = _load_config()
        lots = _get_lots_dict(cfg)
        proxies = (lots.get(str(lid)) or {}).get("proxies", [])
        if not proxies:
            _tg_send(chat_id, f"ℹ️ У лота #{lid} нет привязанных прокси.")
        elif bot_instance:
            try:
                bio = io.BytesIO("\n".join(proxies).encode('utf-8'))
                bio.name = f"proxies_lot_{lid}.txt"
                bot_instance.send_document(chat_id, bio, caption=f"🌐 Прокси лота #{lid} ({len(proxies)} шт.)")
            except Exception as e:
                _tg_send(chat_id, f"❌ Ошибка отправки файла: {e}")
    elif data.startswith("src_lot_test_px:"):
        lid = data.split(":", 1)[1]
        threading.Thread(target=_test_lot_proxies_worker, args=(chat_id, lid, message_id), daemon=True).start()
    elif data.startswith("src_lot_clear_px:"):
        lid = data.split(":", 1)[1]
        cfg = _load_config()
        lots = _get_lots_dict(cfg)
        if lid in lots:
            lots[lid]["proxies"] = []
            if cfg.get("auto_deactivate_lots", True):
                lots[lid]["enabled"] = False
                _deactivate_funpay_lot(cardinal_instance, lid)
            cfg["lots"] = lots
            _save_config(cfg)
        _open_lot_proxies_menu(chat_id, lid, message_id)
    elif data.startswith("src_lot_delete_ask:"):
        lid = data.split(":", 1)[1]
        text = f"⚠️ <b>Удалить лот #{lid} из плагина?</b>"
        _tg_edit(chat_id, message_id, text, _make_kb([[("✅ Да, удалить", f"src_lot_delete_yes:{lid}"), ("❌ Отмена", f"src_lot_card:{lid}")]]))
    elif data.startswith("src_lot_delete_yes:"):
        lid = data.split(":", 1)[1]
        cfg = _load_config()
        lots = _get_lots_dict(cfg)
        lots.pop(lid, None)
        cfg["lots"] = lots
        _save_config(cfg)
        _open_lots_menu(chat_id, message_id)

    elif data == "src_stats_reset":
        _save_stats(DEFAULT_STATS)
        _open_stats(chat_id, message_id)

def _handle_waiting_message(message):
    chat_id = getattr(getattr(message, 'chat', None), 'id', None)
    from_user = getattr(message, 'from_user', None)
    user_id = getattr(from_user, 'id', None)
    if not chat_id or not _is_authorized(user_id):
        return

    document = getattr(message, 'document', None)
    file_text = None
    if document is not None:
        file_name = str(getattr(document, 'file_name', '') or '').strip()

        st = _waiting.get(chat_id) or {}
        if st.get("action") == "update_local" or file_name.endswith('.py'):
            if file_name.endswith('.py'):
                try:
                    file_info = bot_instance.get_file(document.file_id)
                    payload = bot_instance.download_file(file_info.file_path)
                    source = payload.decode('utf-8-sig')

                    plugin_file = os.path.abspath(__file__)
                    compile(source, plugin_file, 'exec')

                    m = re.search(r'(?m)^\s*VERSION\s*=\s*["\']([^"\']+)["\']', source)
                    new_ver = m.group(1).strip() if m else 'неизвестно'

                    stamp = time.strftime('%Y%m%d-%H%M%S')
                    backup_file = plugin_file + f'.pre-local-update.{stamp}.bak'
                    shutil.copy2(plugin_file, backup_file)

                    with open(plugin_file, "w", encoding="utf-8") as f:
                        f.write(source)

                    _waiting.pop(chat_id, None)
                    _tg_send(chat_id, f"✅ <b>Локальное обновление установлено: v{new_ver}!</b>\n\nРезервная копия: <code>{os.path.basename(backup_file)}</code>\nВыполните <code>/restart</code> для применения.", _make_kb([[("◀️ В меню", "src_home")]]))
                    return
                except Exception as e:
                    _tg_send(chat_id, f"❌ Ошибка проверки/установки файла: {e}")
                    return

        if st.get("action") == "import_config" or file_name.endswith('.json'):
            try:
                file_info = bot_instance.get_file(document.file_id)
                payload = bot_instance.download_file(file_info.file_path)
                data = json.loads(payload.decode('utf-8-sig'))
                if isinstance(data, dict):
                    stamp = time.strftime('%Y%m%d-%H%M%S')
                    if os.path.exists(CONFIG_FILE):
                        shutil.copy2(CONFIG_FILE, CONFIG_FILE + f'.{stamp}.bak')
                    _save_config(data)
                    _waiting.pop(chat_id, None)
                    _tg_send(chat_id, "✅ <b>Конфигурация успешно импортирована!</b>", _make_kb([[("⚙️ В настройки", "src_mini_settings")]]))
                    return
            except Exception as e:
                _tg_send(chat_id, f"❌ Ошибка импорта конфига: {e}")
                return

        if file_name.endswith('.txt') or st.get("action") in ("input_gifts", "lot_add_px", "wiz_step3_proxy", "input_proxies"):
            try:
                file_info = bot_instance.get_file(document.file_id)
                payload = bot_instance.download_file(file_info.file_path)
                file_text = payload.decode('utf-8-sig', errors='ignore').strip()
            except Exception as e:
                _tg_send(chat_id, f"❌ Ошибка чтения файла: {e}")
                return

    st = _waiting.get(chat_id)
    if not st:
        return

    action = st.get("action")
    text = (file_text if file_text is not None else str(getattr(message, "text", "") or "")).strip()

    if action == "import_config":
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                stamp = time.strftime('%Y%m%d-%H%M%S')
                if os.path.exists(CONFIG_FILE):
                    shutil.copy2(CONFIG_FILE, CONFIG_FILE + f'.{stamp}.bak')
                _save_config(data)
                _waiting.pop(chat_id, None)
                _tg_send(chat_id, "✅ <b>Конфигурация успешно импортирована!</b>", _make_kb([[("⚙️ В настройки", "src_mini_settings")]]))
                return
        except Exception as e:
            _tg_send(chat_id, f"❌ Ошибка разбора JSON: {e}")
            return

    elif action == "wiz_step1_id":
        lot_id = re.sub(r'\D', '', text)
        if not lot_id:
            _tg_send(chat_id, "⚠️ ID лота должен содержать только цифры. Попробуйте ещё раз:")
            return

        title = f"Лот #{lot_id}"
        try:
            if cardinal_instance and getattr(cardinal_instance, 'account', None):
                lf = cardinal_instance.account.get_lot_fields(int(lot_id))
                if lf and getattr(lf, 'description', None):
                    title = lf.description
        except Exception:
            pass

        _lot_wizard_ask_country(chat_id, lot_id, title)

    elif action == "wiz_step2_country":
        cc = re.sub(r'[^A-Za-z]', '', text).upper()
        if len(cc) == 2:
            lid = st.get("lot_id", "")
            title = st.get("title", f"Лот {lid}")
            _lot_wizard_ask_proxy(chat_id, lid, title, cc)
        else:
            _tg_send(chat_id, "⚠️ Код страны должен состоять ровно из 2 букв (например KZ, US, RU). Выберите кнопку или введите 2 буквы:")

    elif action == "wiz_step3_proxy":
        lower_t = text.lower()
        if any(w in lower_t for w in ["нет", "нету", "no", "none", "-", "отсутствует"]):
            _tg_send(chat_id, "⚠️ <b>Для добавления лота необходим прокси!</b>\n\nБез прокси смена региона Steam невозможна. Отправьте хотя бы один рабочий прокси под этот регион или нажмите «Отмена».")
            return

        new_p = parse_proxies(text)
        if not new_p:
            _tg_send(
                chat_id,
                "⚠️ Не удалось распознать прокси. Пожалуйста, отправьте прокси в поддерживаемом формате:\n"
                "• <code>ip:port</code>\n"
                "• <code>ip:port:login:password</code>\n"
                "• <code>http://ip:port</code>\n"
                "• <code>http://login:password@ip:port</code>"
            )
            return

        lid = st.get("lot_id", "")
        title = st.get("title", f"Лот #{lid}")
        cc = st.get("country", "KZ")

        cfg = _load_config()
        lots = _get_lots_dict(cfg)
        lots[lid] = {
            "id": lid,
            "title": title,
            "enabled": True,
            "country": cc,
            "proxies": new_p,
        }
        cfg["lots"] = lots
        _save_config(cfg)
        _waiting.pop(chat_id, None)

        msg = (
            f"✅ <b>Лот #{lid} успешно настроен и добавлен!</b>\n\n"
            f"• Название: <i>{title}</i>\n"
            f"• Целевая страна: <b>{_country_display(cc)}</b> (<code>{cc}</code>)\n"
            f"• Привязано прокси: <b>{len(new_p)} шт.</b>\n"
            f"• Состояние: <b>🟢 Включён</b>\n\n"
            "Теперь при покупке этого лота плагин сразу начнёт автоматическую смену региона."
        )
        _tg_send(chat_id, msg, _make_kb([[("⚙️ Настройки этого лота", f"src_lot_card:{lid}"), ("⭐ К списку лотов", "src_lots_menu")]]))

    elif action == "lot_add_px":
        lid = st.get("lot_id", "")
        _waiting.pop(chat_id, None)
        new_p = parse_proxies(text)
        if new_p:
            cfg = _load_config()
            lots = _get_lots_dict(cfg)
            if lid in lots:
                old_p = lots[lid].get("proxies", [])
                comb = list(dict.fromkeys(old_p + new_p))
                lots[lid]["proxies"] = comb
                cfg["lots"] = lots
                _save_config(cfg)
                _tg_send(chat_id, f"✅ Добавлено <b>{len(comb) - len(old_p)}</b> прокси к лоту #{lid}. Всего: <b>{len(comb)}</b>.")
        else:
            _tg_send(chat_id, "⚠️ Не удалось распознать прокси.")
        _open_lot_proxies_menu(chat_id, lid)

    elif action == "lot_input_cc":
        lid = st.get("lot_id", "")
        _waiting.pop(chat_id, None)
        cc = re.sub(r'[^A-Za-z]', '', text).upper()
        if len(cc) == 2:
            cfg = _load_config()
            lots = _get_lots_dict(cfg)
            if lid in lots:
                lots[lid]["country"] = cc
                cfg["lots"] = lots
                _save_config(cfg)
                _tg_send(chat_id, f"✅ Страна для лота #{lid} изменена на <b>{_country_display(cc)}</b>.")
        else:
            _tg_send(chat_id, "⚠️ Код страны должен состоять ровно из 2 букв.")
        _open_lot_card(chat_id, lid)

    elif action == "input_proxies":
        _waiting.pop(chat_id, None)
        new_p = parse_proxies(text)
        if new_p:
            existing = _load_proxies()
            combined = list(dict.fromkeys(existing + new_p))
            _save_proxies(combined)
            _tg_send(chat_id, f"✅ Добавлено <b>{len(combined) - len(existing)}</b> новых прокси. Всего в пуле: <b>{len(combined)}</b>.")
        else:
            _tg_send(chat_id, "⚠️ Не удалось распознать прокси в сообщении.")
        _menu_proxies(chat_id)

    elif action == "input_gifts":
        _waiting.pop(chat_id, None)
        raw_codes = parse_gift_codes(text)
        valid, invalid = validate_gift_codes_format(raw_codes)
        if valid:
            existing = _load_gift_codes()
            combined = list(dict.fromkeys(existing + valid))
            _save_gift_codes(combined)
            msg = f"✅ Добавлено <b>{len(combined) - len(existing)}</b> гифт-кодов. Всего доступно: <b>{len(combined)}</b>."
            if invalid:
                msg += f"\n⚠️ Пропущено невалидных кодов: {len(invalid)}."
            _tg_send(chat_id, msg)
        else:
            _tg_send(chat_id, "⚠️ Валидных кодов Steam Wallet не найдено.")
        _menu_gifts(chat_id)

    elif action == "input_workers":
        val_str = re.sub(r'\D', '', text)
        if val_str and int(val_str) > 0:
            val = min(50, max(1, int(val_str)))
            cfg = _load_config()
            cfg["max_workers"] = val
            _save_config(cfg)
            _waiting.pop(chat_id, None)
            _tg_send(chat_id, f"✅ Установлено одновременных смен: <b>{val}</b>.")
            _open_queue_menu(chat_id)
        else:
            _tg_send(chat_id, "⚠️ Введите целое число больше 0 (например: <code>5</code> или <code>10</code>):")

    elif action == "input_timeout":
        val_str = re.sub(r'[^\d.]', '', text)
        try:
            val = float(val_str)
            if 5.0 <= val <= 300.0:
                cfg = _load_config()
                cfg["request_timeout"] = val
                _save_config(cfg)
                _waiting.pop(chat_id, None)
                _tg_send(chat_id, f"✅ Установлен таймаут запросов: <b>{int(val)} сек</b>.")
                _open_queue_menu(chat_id)
            else:
                _tg_send(chat_id, "⚠️ Таймаут должен быть в диапазоне от 5 до 300 секунд.")
        except Exception:
            _tg_send(chat_id, "⚠️ Введите число секунд (например: <code>30</code> или <code>60</code>):")

    elif action == "edit_buyer_message":
        key = st.get("key", "")
        _waiting.pop(chat_id, None)
        if key in BUYER_MESSAGE_LABELS and text:
            cfg = _load_config()
            bm = cfg.get("buyer_messages") or {}
            bm[key] = text
            cfg["buyer_messages"] = bm
            _save_config(cfg)
            _tg_send(
                chat_id,
                f"✅ Шаблон <b>«{BUYER_MESSAGE_LABELS[key]}»</b> успешно обновлен!",
                _make_kb([[("💬 К списку сообщений", "src_cat_messages")]])
            )
        else:
            _tg_send(chat_id, "⚠️ Текст не может быть пустым.")
            _open_messages_menu(chat_id)

_buyer_sessions: dict[Any, dict[str, Any]] = {}
_active_sessions: list[dict[str, Any]] = []
_sessions_lock = threading.Lock()

def _extract_session_keys(sess: dict[str, Any]) -> set[Any]:

    keys: set[Any] = set()
    cid = sess.get("chat_id")
    if cid is not None:
        keys.add(cid)
        keys.add(str(cid))
        nums = re.findall(r'\d+', str(cid))
        for num in nums:
            try:
                keys.add(int(num))
            except Exception:
                pass
            keys.add(str(num))

    bid = sess.get("buyer_id")
    if bid is not None:
        try:
            keys.add(int(bid))
        except Exception:
            pass
        keys.add(str(bid))

    bname = str(sess.get("buyer_username") or "").strip().lower()
    if bname and bname != '?':
        keys.add(f"user:{bname}")

    oid = str(sess.get("order_id") or "").strip()
    if oid:
        if oid.startswith('#'):
            oid = oid[1:]
        keys.add(f"order:{oid}")
        keys.add(f"order:#{oid}")
        keys.add(oid)
    return keys

def _register_buyer_session(sess: dict[str, Any], buyer_id: Any = None) -> None:

    if buyer_id is not None:
        sess["buyer_id"] = buyer_id
    with _sessions_lock:
        oid = str(sess.get("order_id") or "")
        _active_sessions[:] = [
            s for s in _active_sessions
            if str(s.get("order_id") or "") != oid and s.get("step") not in ("finished", "refunded", "failed_closed")
        ]
        _active_sessions.append(sess)

        to_del = [k for k, v in _buyer_sessions.items() if str(v.get("order_id") or "") == oid]
        for k in to_del:
            _buyer_sessions.pop(k, None)

        for k in _extract_session_keys(sess):
            _buyer_sessions[k] = sess

def _find_buyer_session(
    chat_id: Any = None,
    chat_name: Any = None,
    author: Any = None,
    author_id: Any = None,
    order_id: Any = None
) -> dict[str, Any] | None:

    with _sessions_lock:
        candidates: list[Any] = []
        if chat_id is not None:
            candidates.extend([chat_id, str(chat_id)])
            try:
                candidates.append(int(chat_id))
            except Exception:
                pass
            for num in re.findall(r'\d+', str(chat_id)):
                try:
                    candidates.append(int(num))
                except Exception:
                    pass
                candidates.append(str(num))

        if author_id is not None:
            candidates.extend([author_id, str(author_id)])
            try:
                candidates.append(int(author_id))
            except Exception:
                pass

        if chat_name:
            candidates.append(f"user:{str(chat_name).strip().lower()}")
        if author:
            candidates.append(f"user:{str(author).strip().lower()}")

        if order_id:
            oid = str(order_id).strip()
            if oid.startswith('#'):
                oid = oid[1:]
            candidates.extend([f"order:{oid}", f"order:#{oid}", oid])

        for cand in candidates:
            if cand in _buyer_sessions:
                s = _buyer_sessions[cand]
                if s.get("step") not in ("finished", "refunded", "failed_closed"):
                    return s

        for s in reversed(_active_sessions):
            if s.get("step") in ("finished", "refunded", "failed_closed"):
                continue
            s_keys = _extract_session_keys(s)
            for cand in candidates:
                if cand in s_keys:
                    return s
        return None

def _is_valid_steam_login(s: str) -> bool:

    val = str(s or '').strip()
    if not (2 <= len(val) <= 64):
        return False
    if any(c in val for c in (' ', '\t', '\n', '\r')):
        return False
    try:
        val.encode('ascii')
    except UnicodeEncodeError:
        return False
    if any(c in val for c in ('@', '!', '?', ',', ';', '/', '\\', '|', '<', '>', '"', "'", '`', '(', ')', '[', ']', '{', '}', '#', '$', '%', '^', '&', '*', '=')):
        return False
    return True

def _clean_funpay_text(text: str) -> str:

    s = re.sub(r'<[^>]+>', '', text)
    s = html.unescape(s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

def _send_buyer_fp_msg(cardinal, chat_id, text: str, buyer_username: str | None = None) -> bool:

    acc = getattr(cardinal, 'account', None) if cardinal else None
    if not acc:
        acc = getattr(cardinal_instance, 'account', None) if cardinal_instance else None
    if not acc:
        logger.error(f"[{NAME}] send_message: аккаунт Cardinal недоступен")
        return False

    cid = chat_id
    if (cid is None or str(cid).strip() in ("", "?")) and buyer_username and buyer_username != "?":
        try:
            chat = acc.get_chat_by_name(buyer_username, True)
            if chat:
                cid = int(chat.id)
        except Exception as e:
            logger.warning(f"[{NAME}] get_chat_by_name({buyer_username}): {e}")

    if cid is None:
        logger.error(f"[{NAME}] Нет chat_id для отправки сообщения покупателю {buyer_username}")
        return False

    target_cids = [cid]
    if isinstance(cid, str) and cid.isdigit():
        target_cids = [int(cid), cid]

    clean_text = _clean_funpay_text(text)

    for attempt in range(3):
        for target in target_cids:
            try:
                acc.send_message(target, clean_text)
                return True
            except Exception as e:
                logger.warning(f"[{NAME}] Ошибка отправки сообщения в FunPay (target={target}, попытка {attempt + 1}): {e}")
                time.sleep(0.5)

        if attempt == 0 and buyer_username and buyer_username != "?":
            try:
                chat = acc.get_chat_by_name(buyer_username, True)
                if chat and getattr(chat, 'id', None):
                    new_cid = int(chat.id)
                    if new_cid not in target_cids:
                        target_cids.insert(0, new_cid)
            except Exception:
                pass
        time.sleep(1)

    return False

def _try_refund(cardinal, order_id: str | int) -> bool:

    acc = getattr(cardinal, 'account', None) if cardinal else None
    if not acc:
        acc = getattr(cardinal_instance, 'account', None) if cardinal_instance else None
    if not acc:
        logger.error(f"[{NAME}] Не удалось выполнить возврат #{order_id}: аккаунт Cardinal недоступен")
        return False
    try:
        oid = str(order_id).strip()
        if oid.startswith('#'):
            oid = oid[1:]
        acc.refund(oid)
        logger.info(f"[{NAME}] Возврат средств по заказу #{oid} успешно выполнен")
        return True
    except Exception as e:
        logger.error(f"[{NAME}] Ошибка возврата средств #{order_id}: {e}")
        return False

def _set_funpay_lot_active(cardinal, lot_id: str | int, active: bool) -> bool:

    acc = getattr(cardinal, 'account', None) if cardinal else None
    if not acc:
        acc = getattr(cardinal_instance, 'account', None) if cardinal_instance else None
    if not acc:
        return False
    try:
        lf = acc.get_lot_fields(int(lot_id))
        if getattr(lf, "active", None) == active:
            return True
        lf.active = bool(active)
        renew = getattr(lf, "renew_fields", None)
        if callable(renew):
            renewed = renew()
            if renewed is not None:
                lf = renewed
        acc.save_lot(lf)
        logger.info(f"[{NAME}] Статус лота #{lot_id} на FunPay изменён: active={active}")
        return True
    except Exception as e:
        logger.warning(f"[{NAME}] Ошибка изменения активности лота #{lot_id} на FunPay: {e}")
        return False

def _deactivate_funpay_lot(cardinal, lot_id: str | int) -> bool:

    return _set_funpay_lot_active(cardinal, lot_id, False)

_processed_orders: set[str] = set()
_processed_orders_lock = threading.Lock()
ORDER_PAID_RE = re.compile(r'оплатил(?:а)?\s+заказ\s*#([A-Za-z0-9]+)', re.IGNORECASE)

def _find_lot_for_order(cardinal, order, event=None, order_text: str = "") -> tuple[str | None, dict[str, Any] | None, str]:

    cfg = _load_config()
    lots = _get_lots_dict(cfg)
    if not lots:
        return (None, None, "no_configured_lots")

    candidates: list[tuple[str, str]] = []
    for obj in (event, order):
        if obj is not None:
            lid = getattr(obj, 'lot_id', None)
            if lid is not None and str(lid).strip():
                candidates.append((str(lid).strip(), "event_attribute"))
            lot_obj = getattr(obj, 'lot', None) or getattr(obj, 'offer', None)
            if lot_obj is not None:
                lid2 = getattr(lot_obj, 'id', None)
                if lid2 is not None and str(lid2).strip():
                    candidates.append((str(lid2).strip(), "lot_object"))

    for cand_id, src in candidates:
        if cand_id in lots:
            return (cand_id, lots[cand_id], f"direct_candidate_{src}")

    texts: list[str] = []
    for s in [
        getattr(order, 'description', None),
        getattr(order, 'title', None),
        getattr(order, 'full_description', None),
        getattr(order, 'subcategory_name', None),
        order_text,
    ]:
        txt = str(s or '').strip()
        if txt and txt not in texts:
            texts.append(txt)

    seller_lots = []
    if cardinal:
        prof = getattr(cardinal, 'profile', None)
        if prof and hasattr(prof, 'get_lots'):
            try:
                seller_lots = prof.get_lots() or []
            except Exception:
                pass
        if not seller_lots and getattr(cardinal, 'account', None):
            try:
                seller_lots = cardinal.account.get_my_subcategory_lots(2044) or []
            except Exception:
                pass

    for s_lot in seller_lots:
        slid = str(getattr(s_lot, 'id', '') or '')
        sdesc = str(getattr(s_lot, 'description', None) or getattr(s_lot, 'title', None) or '').strip()
        if slid and sdesc:
            sdesc_low = sdesc.lower()
            for t in texts:
                t_low = t.lower()
                if (sdesc_low in t_low or t_low in sdesc_low) and len(sdesc_low) >= 3:
                    if slid in lots:
                        return (slid, lots[slid], "profile_desc_match")
                    else:
                        candidates.append((slid, "profile_desc_unconfigured"))

    for lid, lcfg in lots.items():
        ltitle = str(lcfg.get("title") or '').strip().lower()
        if ltitle and not ltitle.startswith("лот ") and not ltitle.startswith("лот #"):
            for t in texts:
                t_low = t.lower()
                if (ltitle in t_low or t_low in ltitle) and len(ltitle) >= 3:
                    return (lid, lcfg, "config_title_match")

    is_steam_rc = False
    subcat = getattr(order, 'subcategory', None)
    if subcat and getattr(subcat, 'id', None) == 2044:
        is_steam_rc = True
    combined_texts = " ".join(texts).lower()
    if any(kw in combined_texts for kw in ["смена региона", "steam", "стим", "регион"]):
        is_steam_rc = True

    if is_steam_rc:
        detected_cc = None
        if any(k in combined_texts for k in ["казахстан", "тенге", " kz", "(kz)", "kz "]):
            detected_cc = "KZ"
        elif any(k in combined_texts for k in ["турци", "лир", " tr", "(tr)", "tr "]):
            detected_cc = "TR"
        elif any(k in combined_texts for k in ["украин", "гривн", " ua", "(ua)", "ua "]):
            detected_cc = "UA"
        elif any(k in combined_texts for k in ["росси", "рубл", " ru", "(ru)", "ru "]):
            detected_cc = "RU"
        elif any(k in combined_texts for k in ["сша", "доллар", " us", "(us)", "usa"]):
            detected_cc = "US"
        elif any(k in combined_texts for k in ["аргентин", "песо", " ar", "(ar)", "ar "]):
            detected_cc = "AR"
        elif any(k in combined_texts for k in ["болгари", " bg", "(bg)", "bg "]):
            detected_cc = "BG"

        if detected_cc:
            matched = [
                (lid, lcfg) for lid, lcfg in lots.items()
                if lcfg.get("country", "").upper() == detected_cc and lcfg.get("enabled", True)
            ]
            if not matched:
                matched = [
                    (lid, lcfg) for lid, lcfg in lots.items()
                    if lcfg.get("country", "").upper() == detected_cc
                ]
            if matched:
                lid, lcfg = matched[0]
                return (lid, lcfg, f"country_match_{detected_cc}")

    for cand_id, src in candidates:
        if cand_id:
            return (cand_id, None, f"candidate_not_in_config_{src}")

    return (None, None, "no_matching_lot")

def _process_incoming_order(cardinal, order, event=None, order_text: str = ""):

    global cardinal_instance
    if cardinal_instance is None:
        cardinal_instance = cardinal
    cfg = _load_config()
    if not cfg.get("plugin_enabled", True):
        return

    oid = str(getattr(order, 'id', '') or getattr(event, 'order_id', '') or getattr(event, 'id', '') or '').strip()
    if oid.startswith('#'):
        oid = oid[1:]
    if not oid:
        return

    with _processed_orders_lock:
        if oid in _processed_orders:
            return

    buyer_name = str(
        getattr(order, 'buyer_username', None)
        or getattr(event, 'buyer_username', None)
        or getattr(event, 'user_name', None)
        or '?'
    ).strip()
    buyer_id = (
        getattr(order, 'buyer_id', None)
        or getattr(event, 'buyer_id', None)
        or getattr(event, 'user_id', None)
    )

    desc_sample = str(getattr(order, 'description', None) or order_text or '')[:100]

    _log_event("order_received", order_id=oid, buyer=buyer_name, desc=desc_sample)

    lot_id, lot_cfg, match_reason = _find_lot_for_order(cardinal, order, event, order_text)

    if not lot_id or not lot_cfg:
        _log_event("order_miss", level=logging.INFO, order_id=oid, reason=match_reason, configured_lots=list(_get_lots_dict(cfg).keys()))
        return

    with _processed_orders_lock:
        if oid in _processed_orders:
            return
        _processed_orders.add(oid)

    country = lot_cfg.get("country", "KZ")
    _log_event("order_matched", order_id=oid, lot_id=lot_id, method=match_reason, country=country)

    if not lot_cfg.get("enabled", False):
        _log_event("lot_disabled", level=logging.WARNING, order_id=oid, lot_id=lot_id)
        return

    chat_id = getattr(order, 'chat_id', None) or getattr(event, 'chat_id', None)
    if not chat_id and getattr(cardinal, 'account', None):
        try:
            full_ord = cardinal.account.get_order(oid)
            chat_id = getattr(full_ord, 'chat_id', None)
            if not buyer_name or buyer_name == '?':
                buyer_name = str(getattr(full_ord, 'buyer_username', '?') or '?')
            if not buyer_id:
                buyer_id = getattr(full_ord, 'buyer_id', None)
        except Exception as e:
            logger.warning(f"[{NAME}] get_order({oid}) fallback: {e}")

    if not chat_id and buyer_name and buyer_name != '?' and getattr(cardinal, 'account', None):
        try:
            chat = cardinal.account.get_chat_by_name(buyer_name, True)
            if chat:
                chat_id = getattr(chat, 'id', None)
        except Exception as e:
            logger.warning(f"[{NAME}] get_chat_by_name({buyer_name}) fallback: {e}")

    if not chat_id:
        _log_event("order_no_chat_id", level=logging.WARNING, order_id=oid, lot_id=lot_id)
        return

    country_name = _country_display(country)
    proxies = list(lot_cfg.get("proxies") or [])
    if not proxies:
        _log_event("order_rejected", level=logging.WARNING, order_id=oid, lot_id=lot_id, reason="no_proxies")
        if cfg.get("auto_deactivate_lots", True):
            lot_cfg["enabled"] = False
            cfg["lots"][str(lot_id)] = lot_cfg
            _save_config(cfg)
            _deactivate_funpay_lot(cardinal, lot_id)
            _log_event("lot_auto_deactivated", level=logging.WARNING, lot_id=lot_id, reason="proxy_pool_empty")
        if cfg.get("auto_refund_enabled", True):
            _try_refund(cardinal, oid)
            _send_buyer_fp_msg(cardinal, chat_id, _render_buyer_msg("no_proxies_refund", order_id=oid, country_name=country_name), buyer_username=buyer_name)
            _log_event("order_refund", level=logging.WARNING, order_id=oid, reason="no_proxies")
        else:
            _send_buyer_fp_msg(cardinal, chat_id, _render_buyer_msg("no_proxies_refund", order_id=oid, country_name=country_name), buyer_username=buyer_name)

        _notify_tg(
            f"🛑 <b>Заказ #{oid} отклонён</b>\n\n"
            f"• Лот: <b>#{lot_id}</b> ({country_name})\n"
            f"• Причина: закончились прокси\n"
            f"{'🛑 Лот деактивирован на FunPay.' if cfg.get('auto_deactivate_lots', True) else ''}\n"
            f"{'💸 Оформлен автовозврат.' if cfg.get('auto_refund_enabled', True) else ''}",
            ntype="error"
        )
        return

    sess = {
        "order_id": oid,
        "buyer_username": buyer_name,
        "buyer_id": buyer_id,
        "lot_id": str(lot_id),
        "lot_cfg": lot_cfg,
        "step": "waiting_login",
        "chat_id": chat_id,
        "country": country,
        "proxies": proxies,
        "created_at": time.time(),
    }
    _register_buyer_session(sess, buyer_id=buyer_id)

    welcome_text = _render_buyer_msg(
        "welcome",
        country_name=country_name,
        order_id=oid,
        buyer_username=buyer_name,
        login=""
    )
    sent_ok = _send_buyer_fp_msg(cardinal, chat_id, welcome_text, buyer_username=buyer_name)
    if sent_ok:
        _log_event("order_welcome_sent", order_id=oid, chat_id=chat_id, country=country)
    else:
        _log_event("order_welcome_error", level=logging.ERROR, order_id=oid, chat_id=chat_id)

    _notify_tg(
        f"🛒 <b>Новый заказ #{oid}</b>\n\n"
        f"• Покупатель: <b>{buyer_name}</b>\n"
        f"• Лот: <b>#{lot_id}</b> ({country_name})\n"
        f"• Статус: <b>Шаг 1 из 2</b> (ожидаем логин Steam)",
        ntype="order"
    )

def handle_new_order(cardinal, event, *args):
    order = getattr(event, 'order', None) or event
    _process_incoming_order(cardinal, order, event=event)

def handle_new_message(cardinal, event, *args):
    global cardinal_instance
    if cardinal_instance is None:
        cardinal_instance = cardinal
    cfg = _load_config()
    if not cfg.get("plugin_enabled", True):
        return

    message = getattr(event, 'message', None) or event
    author_id = getattr(message, 'author_id', None)
    my_id = getattr(getattr(cardinal, 'account', None), 'id', None)
    if my_id is not None and author_id is not None and str(author_id) == str(my_id):
        return

    chat_id = getattr(message, 'chat_id', None)
    chat_name = getattr(message, 'chat_name', None) or getattr(event, 'chat_name', None)
    author = getattr(message, 'author', None) or getattr(message, 'author_name', None)
    msg_text = str(getattr(message, 'text', '') or getattr(message, 'content', '') or '').strip()
    if not msg_text:
        return

    author_str = str(author or '').strip().lower()
    chat_name_str = str(chat_name or '').strip().lower()
    msg_low = msg_text.lower()
    is_system_msg = (
        author_str in ('funpay', 'system', 'фанпей')
        or chat_name_str in ('funpay', 'system')
        or msg_text.startswith('FunPay:')
        or 'оплатил заказ #' in msg_low
        or 'оплатила заказ #' in msg_low
        or 'подтвердить выполнение заказа' in msg_low
        or 'вернул деньги по заказу' in msg_low
        or 'служба поддержки funpay' in msg_low
    )

    m = ORDER_PAID_RE.search(msg_text)
    if m:
        oid = m.group(1).strip()
        with _processed_orders_lock:
            already = oid in _processed_orders
        if not already:
            _log_event("order_msg_fallback", order_id=oid, text=msg_text[:120])
            full_ord = None
            if getattr(cardinal, 'account', None):
                try:
                    full_ord = cardinal.account.get_order(oid)
                except Exception as e:
                    logger.warning(f"[{NAME}] Fallback get_order({oid}): {e}")
            order_obj = full_ord or SimpleNamespace(id=oid, chat_id=chat_id, description=msg_text, buyer_username=chat_name or author)
            _process_incoming_order(cardinal, order_obj, event=event, order_text=msg_text)
        return

    if is_system_msg:
        return

    sess = _find_buyer_session(
        chat_id=chat_id,
        chat_name=chat_name,
        author=author,
        author_id=author_id
    )
    if not sess:
        return

    if chat_id is not None:
        sess["chat_id"] = chat_id
    if author_id is not None and not sess.get("buyer_id"):
        sess["buyer_id"] = author_id

    oid = str(sess.get("order_id", ""))
    country_name = _country_display(sess.get("country", "KZ"))
    step = sess.get("step")

    if step in ("waiting_login", "waiting_credentials"):
        creds = None
        if ":" in msg_text:
            parts = msg_text.split(":", 1)
            p0, p1 = parts[0].strip(), parts[1].strip()
            if _is_valid_steam_login(p0) and p1:
                creds = (p0, p1)
        elif "\n" in msg_text:
            lines = [l.strip() for l in msg_text.splitlines() if l.strip()]
            if len(lines) >= 2 and _is_valid_steam_login(lines[0]) and lines[1]:
                creds = (lines[0], lines[1])

        if creds:
            sess["login"], sess["password"] = creds
            sess["step"] = "processing"
            _register_buyer_session(sess)

            ack_msg = _render_buyer_msg(
                "data_received",
                login=creds[0],
                country_name=country_name,
                order_id=oid,
                buyer_username=sess.get("buyer_username", "")
            )
            _send_buyer_fp_msg(cardinal, chat_id, ack_msg, buyer_username=sess.get("buyer_username"))
            _log_event("order_creds_received", order_id=oid, login=creds[0])

            _notify_tg(
                f"⏳ <b>Заказ #{oid} взят в работу</b>\n\n"
                f"• Покупатель: <b>{sess.get('buyer_username', '?')}</b>\n"
                f"• Логин Steam: <code>{creds[0]}</code>\n"
                f"• Целевой регион: <b>{country_name}</b>",
                ntype="order"
            )
            threading.Thread(target=_run_order_process, args=(cardinal, sess), daemon=True).start()
        else:
            login_val = msg_text.strip()
            if not _is_valid_steam_login(login_val):
                _log_event("order_invalid_login_format", level=logging.INFO, order_id=oid, text=login_val[:30])
                warn_msg = (
                    "⚠️ <b>Некорректный логин Steam!</b>\n\n"
                    "Логин должен содержать только латинские буквы, цифры и символы без пробелов (например: <code>my_login</code>).\n"
                    "Пожалуйста, отправьте правильный логин (или сразу <code>логин:пароль</code>):"
                )
                _send_buyer_fp_msg(cardinal, chat_id, warn_msg, buyer_username=sess.get("buyer_username"))
                return

            sess["login"] = login_val
            sess["step"] = "waiting_password"
            _register_buyer_session(sess)

            ask_pwd = _render_buyer_msg(
                "ask_password",
                login=login_val,
                country_name=country_name,
                order_id=oid,
                buyer_username=sess.get("buyer_username", "")
            )
            _send_buyer_fp_msg(cardinal, chat_id, ask_pwd, buyer_username=sess.get("buyer_username"))
            _log_event("order_login_received", order_id=oid, login=login_val)

    elif step == "waiting_password":
        pwd_val = msg_text.strip()
        if ":" in pwd_val:
            parts = pwd_val.split(":", 1)
            p0, p1 = parts[0].strip(), parts[1].strip()
            if _is_valid_steam_login(p0) and p1:
                sess["login"] = p0
                sess["password"] = p1
            else:
                sess["password"] = pwd_val
        else:
            sess["password"] = pwd_val

        login_val = sess.get("login", "")
        sess["step"] = "processing"
        _register_buyer_session(sess)

        ack_msg = _render_buyer_msg(
            "data_received",
            login=login_val,
            country_name=country_name,
            order_id=oid,
            buyer_username=sess.get("buyer_username", "")
        )
        _send_buyer_fp_msg(cardinal, chat_id, ack_msg, buyer_username=sess.get("buyer_username"))
        _log_event("order_password_received", order_id=oid, login=login_val)

        _notify_tg(
            f"⏳ <b>Заказ #{oid} взят в работу</b>\n\n"
            f"• Покупатель: <b>{sess.get('buyer_username', '?')}</b>\n"
            f"• Логин Steam: <code>{login_val}</code>\n"
            f"• Целевой регион: <b>{country_name}</b>",
            ntype="order"
        )
        threading.Thread(target=_run_order_process, args=(cardinal, sess), daemon=True).start()

    elif step == "waiting_guard":
        guard_event = sess.get("guard_event")
        if guard_event and isinstance(guard_event, threading.Event):
            guard_val = msg_text.strip().upper()
            sess["guard_code"] = guard_val
            _log_event("order_guard_entered", order_id=oid, code=guard_val)
            guard_event.set()

def _run_order_process(cardinal, sess):
    chat_id = sess["chat_id"]
    login = sess["login"]
    password = sess["password"]
    country = sess["country"]
    proxies = sess.get("proxies") or _load_proxies()
    lot_id = sess.get("lot_id")
    oid = str(sess.get("order_id", ""))
    buyer_user = sess.get("buyer_username", "")

    cfg = _load_config()
    if not proxies:
        if cfg.get("auto_deactivate_lots", True) and lot_id:
            _deactivate_funpay_lot(cardinal, lot_id)
            lots = _get_lots_dict(cfg)
            if str(lot_id) in lots:
                lots[str(lot_id)]["enabled"] = False
                cfg["lots"] = lots
                _save_config(cfg)
        if cfg.get("auto_refund_enabled", True):
            _try_refund(cardinal, oid)
            _send_buyer_fp_msg(cardinal, chat_id, _render_buyer_msg("no_proxies_refund", order_id=oid, country_name=_country_display(country)), buyer_username=buyer_user)
            _log_event("order_refund", level=logging.WARNING, order_id=oid, reason="no_proxies")
        else:
            _send_buyer_fp_msg(cardinal, chat_id, _render_buyer_msg("no_proxies_refund", order_id=oid, country_name=_country_display(country)), buyer_username=buyer_user)
        sess["step"] = "failed"
        with _sessions_lock:
            to_del = [k for k, v in _buyer_sessions.items() if str(v.get("order_id") or "") == oid]
            for k in to_del:
                _buyer_sessions.pop(k, None)
            _active_sessions[:] = [s for s in _active_sessions if str(s.get("order_id") or "") != oid]
        return

    _log_event("order_start", order_id=oid, lot_id=lot_id, login=login, country=country)

    async def _async_worker():
        proxy_pool = ProxyPool(proxies)
        gift_pool = None
        if cfg.get("auto_redeem_gift", True):
            g_codes = _load_gift_codes()
            if g_codes:
                gift_pool = GiftCodePool(g_codes, per_account=1)

        async def _guard_cb(login_str: str) -> str:
            sess["step"] = "waiting_guard"
            sess["guard_event"] = threading.Event()
            _send_buyer_fp_msg(cardinal, chat_id, _render_buyer_msg("guard_request", login=login, order_id=oid, buyer_username=buyer_user), buyer_username=buyer_user)
            _log_event("order_guard_requested", order_id=oid, login=login)
            got = await asyncio.get_event_loop().run_in_executor(None, lambda: sess["guard_event"].wait(timeout=120))
            if got:
                code = sess.get("guard_code", "")
                sess["step"] = "processing"
                _log_event("order_guard_received", order_id=oid, login=login)
                return code
            sess["step"] = "failed"
            _log_event("order_guard_timeout", level=logging.WARNING, order_id=oid, login=login)
            return ""

        sem = asyncio.Semaphore(1)
        res = await process_one_account(
            login=login,
            password=password,
            shared_secret=None,
            proxy_pool=proxy_pool,
            country_code=country,
            semaphore=sem,
            gift_pool=gift_pool,
            guard_provider=_guard_cb,
        )

        if gift_pool is not None:
            _save_gift_codes(gift_pool.codes)

        st = _load_stats()
        st["total_operations"] = st.get("total_operations", 0) + 1
        st["last_operation"] = f"{time.strftime('%d.%m.%Y %H:%M:%S')} (Заказ #{oid})"

        if cfg.get("auto_deactivate_lots", True) and lot_id and len(proxy_pool) == 0:
            _log_event("lot_auto_deactivated", level=logging.WARNING, lot_id=lot_id, reason="proxy_pool_empty")
            _deactivate_funpay_lot(cardinal, lot_id)
            cfg_curr = _load_config()
            lots_curr = _get_lots_dict(cfg_curr)
            if str(lot_id) in lots_curr:
                lots_curr[str(lot_id)]["enabled"] = False
                cfg_curr["lots"] = lots_curr
                _save_config(cfg_curr)
            if bot_instance and admin_chat_id:
                _tg_send(admin_chat_id, f"🛑 <b>Лот #{lot_id} деактивирован на FunPay</b>\nЗакончились рабочие прокси в пуле.")

        if res.is_success:
            st["success"] = st.get("success", 0) + 1
            if res.gift_redeemed:
                st["gifts_redeemed"] = st.get("gifts_redeemed", 0) + 1
            _save_stats(st)
            sess["step"] = "finished"
            success_msg = _render_buyer_msg(
                "success",
                country_name=_country_display(country),
                order_id=oid,
                login=login,
                buyer_username=buyer_user
            )
            _send_buyer_fp_msg(cardinal, chat_id, success_msg, buyer_username=buyer_user)
            _log_event("order_success", order_id=oid, login=login, country=country, gift=res.gift_redeemed)

            gift_line = "\n• Активация гифта: 🎁 успешно" if res.gift_redeemed else ""
            _notify_tg(
                f"✅ <b>Заказ #{oid} успешно выполнен!</b>\n\n"
                f"• Покупатель: <b>{buyer_user or '?'}</b>\n"
                f"• Логин Steam: <code>{login}</code>\n"
                f"• Новый регион: <b>{_country_display(country)}</b>"
                f"{gift_line}",
                ntype="success"
            )
        else:
            st["failed"] = st.get("failed", 0) + 1
            _save_stats(st)
            sess["step"] = "failed"

            is_funding_issue = (
                res.status == RegionResult.SKIP_FIXED
                or "пополнен" in str(res.error or "").lower()
                or "кошел" in str(res.error or "").lower()
                or "баланс" in str(res.error or "").lower()
            )

            if cfg.get("auto_refund_enabled", True):
                refund_ok = _try_refund(cardinal, oid)
                if is_funding_issue:
                    fail_msg = _render_buyer_msg("fixed_wallet_refund", order_id=oid, login=login, buyer_username=buyer_user)
                else:
                    fail_msg = _render_buyer_msg("error_refund", order_id=oid, reason=str(res.error or "ошибка смены региона"), login=login, buyer_username=buyer_user)
                _send_buyer_fp_msg(cardinal, chat_id, fail_msg, buyer_username=buyer_user)
                _log_event("order_refund", level=logging.WARNING, order_id=oid, login=login, refund_ok=refund_ok, is_funding=is_funding_issue)
                _notify_tg(
                    f"💸 <b>{'Автовозврат выполнен' if refund_ok else 'Сбой автовозврата'} · Заказ #{oid}</b>\n\n"
                    f"• Покупатель: <b>{buyer_user or '?'}</b>\n"
                    f"• Логин Steam: <code>{login}</code>\n"
                    f"• Причина: {res.error or ('обнаружены предыдущие пополнения Steam' if is_funding_issue else 'ошибка смены региона')}",
                    ntype="error"
                )
            else:
                if is_funding_issue:
                    fail_msg = _render_buyer_msg("fixed_wallet_refund", order_id=oid, login=login, buyer_username=buyer_user)
                else:
                    fail_msg = _render_buyer_msg("error_refund", order_id=oid, reason=str(res.error or "ошибка авторизации"), login=login, buyer_username=buyer_user)
                _send_buyer_fp_msg(cardinal, chat_id, fail_msg, buyer_username=buyer_user)
                _log_event("order_failed", level=logging.WARNING, order_id=oid, login=login, err=str(res.error or "unknown"))
                _notify_tg(
                    f"⚠️ <b>Заказ #{oid} не выполнен</b>\n\n"
                    f"• Покупатель: <b>{buyer_user or '?'}</b>\n"
                    f"• Логин Steam: <code>{login}</code>\n"
                    f"• Причина: {res.error or ('обнаружены предыдущие пополнения Steam' if is_funding_issue else 'ошибка смены региона')}\n"
                    f"• Автовозврат выключен в настройках плагина.",
                    ntype="error"
                )

        with _sessions_lock:
            to_del = [k for k, v in _buyer_sessions.items() if str(v.get("order_id") or "") == oid]
            for k in to_del:
                _buyer_sessions.pop(k, None)
            _active_sessions[:] = [s for s in _active_sessions if str(s.get("order_id") or "") != oid]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_async_worker())
    except Exception as e:
        logger.error(f"[{NAME}] Order processing error: {e}", exc_info=True)
        try:
            _send_buyer_fp_msg(cardinal, chat_id, f"❌ Произошла непредвиденная ошибка: {e}", buyer_username=buyer_user)
        except Exception:
            pass
        with _sessions_lock:
            to_del = [k for k, v in _buyer_sessions.items() if str(v.get("order_id") or "") == oid]
            for k in to_del:
                _buyer_sessions.pop(k, None)
            _active_sessions[:] = [s for s in _active_sessions if str(s.get("order_id") or "") != oid]
    finally:
        loop.close()

def init_cardinal(cardinal, *args):
    global cardinal_instance, bot_instance, admin_chat_id
    cardinal_instance = cardinal
    _init_storage()

    bot_obj = getattr(getattr(cardinal, 'telegram', None), 'bot', None)
    globals()['bot_instance'] = bot_obj

    try:
        auth = getattr(getattr(cardinal, 'telegram', None), 'authorized_users', None)
        if isinstance(auth, dict) and auth:
            admin_chat_id = int(list(auth.keys())[0])
    except Exception:
        admin_chat_id = None

    try:
        cardinal.add_telegram_commands(UUID, [('src', 'Steam Region Changer: меню смены региона Steam', True)])
    except Exception as e:
        logger.warning(f"[{NAME}] Ошибка регистрации команд: {e}")

    if bot_obj:
        def _open_plugin_home(call):
            try:
                bot_obj.answer_callback_query(call.id)
            except Exception:
                pass
            cid = getattr(getattr(getattr(call, 'message', None), 'chat', None), 'id', None)
            mid = getattr(getattr(call, 'message', None), 'message_id', None)
            if cid:
                _open_home(cid, mid)

        def _plugin_entry(data):
            data = str(data or '')
            if data in (CBT_SETTINGS, f'{UUID}:0'):
                return True
            edit = getattr(_CBT, 'EDIT_PLUGIN', None)
            settings = getattr(_CBT, 'PLUGIN_SETTINGS', None)
            return bool((edit is not None and data.startswith(f'{edit}:{UUID}')) or
                        (settings is not None and data.startswith(f'{settings}:{UUID}')))

        try:
            cardinal.telegram.cbq_handler(_open_plugin_home, func=lambda call: _plugin_entry(getattr(call, 'data', None)))
        except Exception as e:
            logger.warning(f"[{NAME}] Не удалось привязать кнопку настроек: {e}")

        try:
            bot_obj.register_callback_query_handler(
                _cb_router,
                func=lambda call: isinstance(getattr(call, 'data', None), str) and call.data.startswith('src_')
            )
        except Exception as e:
            logger.warning(f"[{NAME}] Не удалось привязать callback-обработчик: {e}")

        def _cmd_src(message):
            cid = getattr(getattr(message, 'chat', None), 'id', None)
            if cid and _is_authorized(getattr(getattr(message, 'from_user', None), 'id', None)):
                _open_home(cid)

        try:
            bot_obj.register_message_handler(_cmd_src, commands=['src', 'src_menu'])
        except Exception as e:
            logger.warning(f"[{NAME}] Не удалось привязать команду /src: {e}")

        def _msg_predicate(message):
            cid = getattr(getattr(message, 'chat', None), 'id', None)
            if not cid:
                return False
            if cid in _waiting:
                return True
            doc = getattr(message, 'document', None)
            if doc:
                fname = str(getattr(doc, 'file_name', '') or '').lower()
                if fname.endswith('.py') or fname.endswith('.json') or fname.endswith('.txt'):
                    return True
            return False

        try:
            bot_obj.register_message_handler(_handle_waiting_message, func=_msg_predicate, content_types=['text', 'document'])
        except Exception as e:
            logger.warning(f"[{NAME}] Не удалось привязать обработчик ввода: {e}")

    logger.info(f"[{NAME}] Плагин инициализирован (v{VERSION})")

def on_delete(cardinal, *args, **kwargs):
    logger.info(f"[{NAME}] Плагин удален.")

BIND_TO_PRE_INIT = [init_cardinal]
BIND_TO_NEW_ORDER = [handle_new_order]
BIND_TO_NEW_MESSAGE = [handle_new_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [handle_new_message]
BIND_TO_DELETE = on_delete
