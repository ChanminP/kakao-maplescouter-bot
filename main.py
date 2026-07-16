from fastapi import FastAPI, Request, Response
from urllib.parse import quote
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import os
import re
import math
import time
import httpx
import asyncio

load_dotenv()

app = FastAPI()

FEE_RATE = 0.03

# Nexon Open API
BASE_URL = "https://open.api.nexon.com/maplestory/v1"
NEXON_API_KEY = os.getenv("NEXON_API_KEY", "").strip()

# Maplescouter API
MAPLESCOUTER_API_KEY = os.getenv("MAPLESCOUTER_API_KEY", "").strip()

# Maplescouter API cache
MAPLESCOUTER_CACHE = {}
MAPLESCOUTER_CACHE_TTL = 300  # 5 minutes

ALL_CHARACTERS = [
    "병찬형",
    "지냑지",
    "담아요란",
    "뽈드시그널",
    "도나땅",
    "바보밍경",
    "담요가좋아요",
]

CHALLENGE_CHARACTERS = [
    "망틴캡",
    "트란스포머",
    "페레페레테",
    "병동생",
    "도사밍경",
    "레테맹이",
]

PET_ATTACK_CORRECTION = {
    "담아요란": 154,
    "담요가좋아요": 154,
}

MAPLESCOUTER_ALL_CACHE_TTL = 300

MAPLESCOUTER_ALL_CACHES = {
    "normal": {
        "saved_time": 0,
        "text": None,
    },
    "challenge": {
        "saved_time": 0,
        "text": None,
    },
}

MAPLESCOUTER_ALL_REFRESH_TASKS = {
    "normal": None,
    "challenge": None,
}

@app.get("/")
def home():
    return {"message": "Kakao Maple Bot is running"}

@app.head("/")
def home_head():
    return Response(status_code=200)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.head("/health")
def health_check_head():
    return Response(status_code=200)

def simple_text(message: str):
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": message
                    }
                }
            ]
        }
    }


def remove_bot_mention(utterance: str) -> str:
    utterance = utterance.strip()

    # 이상한 공백/제로폭 문자 제거
    utterance = utterance.replace("\u00a0", " ")
    utterance = utterance.replace("\u200b", "")
    utterance = utterance.replace("\ufeff", "")

    # @메이플봇 환산 all → 환산 all
    utterance = re.sub(r"^@\S+\s*", "", utterance).strip()

    # 여러 공백을 하나로 정리
    utterance = re.sub(r"\s+", " ", utterance)

    return utterance


def is_valid_nickname(nickname: str) -> bool:
    nickname = nickname.strip()

    if len(nickname) < 2 or len(nickname) > 12:
        return False

    return bool(re.match(r"^[가-힣A-Za-z0-9]+$", nickname))


def floor_to_100(value: float) -> int:
    return int(math.floor(value / 100) * 100)


def format_meso(value: int) -> str:
    value = int(value)

    eok = value // 100_000_000
    man = (value % 100_000_000) // 10_000
    rest = value % 10_000

    parts = []

    if eok:
        parts.append(f"{eok}억")
    if man:
        parts.append(f"{man}만")
    if rest:
        parts.append(f"{rest}")

    if not parts:
        return "0 메소"

    return " ".join(parts) + " 메소"


def format_korean_number(value) -> str:
    try:
        value = int(value)
    except Exception:
        return "-"

    eok = value // 100_000_000
    man = (value % 100_000_000) // 10_000
    rest = value % 10_000

    parts = []

    if eok:
        parts.append(f"{eok}억")
    if man:
        parts.append(f"{man}만")
    if rest:
        parts.append(str(rest))

    return " ".join(parts) if parts else "0"


def format_stat(value) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value) if value is not None else "-"


def stat_to_int(value) -> int:
    try:
        return int(str(value).replace(",", ""))
    except Exception:
        return 0


def parse_meso_amount(text: str) -> int | None:
    # 쉼표와 메소 표기만 제거하고, 공백은 유지
    cleaned = text.replace(",", "")
    cleaned = re.sub(r"메소|meso", "", cleaned, flags=re.IGNORECASE)

    # 앞쪽 명령어 제거
    cleaned = re.sub(r"^\s*!?분배\s*", "", cleaned)

    # 뒤쪽 인원수 제거
    # 예: "20000000000 3명" → "20000000000"
    cleaned = re.sub(r"\s+\d+\s*명\s*$", "", cleaned).strip()

    if not cleaned:
        return None

    # 조·억·만 단위 입력 처리
    unit_values = {
        "조": 1_000_000_000_000,
        "억": 100_000_000,
        "만": 10_000,
    }

    total = 0
    unit_found = False

    for unit, multiplier in unit_values.items():
        match = re.search(rf"(\d+)\s*{unit}", cleaned)

        if match:
            total += int(match.group(1)) * multiplier
            unit_found = True

    if unit_found:
        return total if total > 0 else None

    # 단위 없이 숫자만 입력한 경우
    number_match = re.fullmatch(r"\d+", cleaned)

    if number_match:
        value = int(number_match.group(0))
        return value if value > 0 else None

    return None


def parse_party_count(text: str) -> int | None:
    match = re.search(r"(\d+)\s*명", text)

    if match:
        return int(match.group(1))

    numbers = re.findall(r"\d+", text.replace(",", ""))

    if numbers:
        candidate = int(numbers[-1])

        if 2 <= candidate <= 6:
            return candidate

    return None


def get_cached_maplescouter(nickname: str):
    cached = MAPLESCOUTER_CACHE.get(nickname)

    if not cached:
        return None

    saved_time, data = cached

    if time.time() - saved_time > MAPLESCOUTER_CACHE_TTL:
        MAPLESCOUTER_CACHE.pop(nickname, None)
        return None

    return data


def set_cached_maplescouter(nickname: str, data: dict):
    MAPLESCOUTER_CACHE[nickname] = (time.time(), data)


def find_first_key(data, keys):
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] is not None:
                return data[key]

        for value in data.values():
            found = find_first_key(value, keys)
            if found is not None:
                return found

    elif isinstance(data, list):
        for item in data:
            found = find_first_key(item, keys)
            if found is not None:
                return found

    return None


def get_pet_lists(data: dict):
    """
    Maplescouter 응답에서 userPetData, userPetEquipData를 찾음.
    top-level에 없을 수 있으므로 재귀 탐색까지 수행.
    """
    user_pet_data = data.get("userPetData") if isinstance(data, dict) else None
    user_pet_equip_data = data.get("userPetEquipData") if isinstance(data, dict) else None

    if not isinstance(user_pet_data, list):
        user_pet_data = find_first_key(data, ["userPetData"])

    if not isinstance(user_pet_equip_data, list):
        user_pet_equip_data = find_first_key(data, ["userPetEquipData"])

    if not isinstance(user_pet_data, list):
        user_pet_data = None

    if not isinstance(user_pet_equip_data, list):
        user_pet_equip_data = None

    return user_pet_data, user_pet_equip_data


def extract_pet_status(data: dict):
    user_pet_data, user_pet_equip_data = get_pet_lists(data)

    pet_count = len(user_pet_data) if isinstance(user_pet_data, list) else 0
    pet_equip_count = len(user_pet_equip_data) if isinstance(user_pet_equip_data, list) else 0

    pet_attack = 0
    pet_magic = 0

    if isinstance(user_pet_equip_data, list):
        for equip in user_pet_equip_data:
            if not isinstance(equip, dict):
                continue

            options = equip.get("itemOption", [])

            if not isinstance(options, list):
                continue

            for option in options:
                if not isinstance(option, dict):
                    continue

                option_type = option.get("option_type")
                option_value = option.get("option_value")

                try:
                    option_value = int(option_value)
                except Exception:
                    continue

                if option_type == "공격력":
                    pet_attack += option_value
                elif option_type == "마력":
                    pet_magic += option_value

    # 핵심: 두 key가 실제 list로 존재하고, 둘 다 비어 있을 때만 누락으로 판정
    pet_missing = (
        isinstance(user_pet_data, list)
        and isinstance(user_pet_equip_data, list)
        and len(user_pet_data) == 0
        and len(user_pet_equip_data) == 0
    )

    return {
        "pet_missing": pet_missing,
        "pet_count": pet_count,
        "pet_equip_count": pet_equip_count,
        "pet_attack": pet_attack,
        "pet_magic": pet_magic,
    }


def needs_pet_attack_correction(nickname: str, data: dict) -> bool:
    if nickname not in PET_ATTACK_CORRECTION:
        return False

    user_pet_data, user_pet_equip_data = get_pet_lists(data)

    return (
        isinstance(user_pet_data, list)
        and isinstance(user_pet_equip_data, list)
        and len(user_pet_data) == 0
        and len(user_pet_equip_data) == 0
    )


def extract_maplescouter_values(data: dict):
    if not isinstance(data, dict):
        return None

    general_380 = (
        data.get("boss380_stat")
        or data.get("boss380_itemStat")
        or data.get("boss380_item_stat")
        or data.get("item_stat")
    )

    hexa_380 = (
        data.get("boss380_hexaStat")
        or data.get("boss380_hexa_stat")
        or data.get("hexa_stat")
    )

    combat_power = (
        data.get("combat_power")
        or data.get("combatPower")
    )

    calculated_data = data.get("calculatedData")

    if isinstance(calculated_data, dict):
        if combat_power is None:
            combat_power = (
                calculated_data.get("combatPower")
                or calculated_data.get("combat_power")
            )

    if general_380 is None:
        general_380 = find_first_key(
            data,
            ["boss380_stat", "boss380_itemStat", "boss380_item_stat", "item_stat"]
        )

    if hexa_380 is None:
        hexa_380 = find_first_key(
            data,
            ["boss380_hexaStat", "boss380_hexa_stat", "hexa_stat"]
        )

    if combat_power is None:
        combat_power = find_first_key(
            data,
            ["combatPower", "combat_power"]
        )

    if general_380 is None and hexa_380 is None:
        return None

    pet_status = extract_pet_status(data)

    return {
        "general_380": general_380,
        "hexa_380": hexa_380,
        "combat_power": combat_power,
        "pet_missing": pet_status["pet_missing"],
        "pet_count": pet_status["pet_count"],
        "pet_equip_count": pet_status["pet_equip_count"],
        "pet_attack": pet_status["pet_attack"],
        "pet_magic": pet_status["pet_magic"],
    }


def build_user_stat_for_simulator(data: dict) -> dict | None:
    if isinstance(data.get("userStat"), dict):
        return data["userStat"]

    required_keys = [
        "doping",
        "linkSkill",
        "special",
        "stat",
        "hexa",
        "seedRing",
        "entireStat",
        "power",
        "huntSkill",
    ]

    user_stat = {}

    for key in required_keys:
        value = data.get(key)

        if value is None:
            print(f"Missing simulator userStat key: {key}")
            return None

        user_stat[key] = value

    user_stat["isGMS"] = bool(data.get("isGMS", False))
    user_stat["isTMS"] = bool(data.get("isTMS", False))
    user_stat["isMSEA"] = bool(data.get("isMSEA", False))
    user_stat["isJMS"] = bool(data.get("isJMS", False))

    return user_stat


def build_simulator_payload(user_stat: dict, atk_value: int) -> dict:
    special = user_stat.get("special", {})
    doping = user_stat.get("doping", {})
    link_skill = user_stat.get("linkSkill", {})

    return {
        "mainStat": "0",
        "mainStatPer": "0",
        "mainStatAbs": "0",
        "subStat": "0",
        "subStatPer": "0",
        "subStatAbs": "0",
        "ssubStat": "0",
        "ssubStatPer": "0",
        "ssubStatAbs": "0",
        "allStatPer": "0",
        "criRate": "0",
        "buffDuration": "0",
        "coolTimeReduce": "0",
        "atk": str(atk_value),
        "atkPer": "0",
        "bossDmg": "0",
        "criDmg": "0",
        "ignoreGuard": "0",
        "genesis": bool(special.get("genesis", False)),
        "mainStat9Level": "",
        "subStat9Level": "",
        "ssubStat9Level": "",
        "finalDmg": "0.00000",
        "resetCoolDown": "0.0",
        "tms_fd": "",
        "weaponAtk": "0",
        "masteryCore1": "",
        "masteryCore2": "",
        "masteryCore3": "",
        "masteryCore4": "",
        "skillCore1": "",
        "skillCore2": "",
        "reinCore1": "",
        "reinCore2": "",
        "reinCore3": "",
        "reinCore4": "",
        "generalCore2": "",
        "generalCore3": "",
        "erda": "0",
        "solJanus": "0",
        "dopingSimul": doping,
        "linkSimul": link_skill,
        "restraintRing": str(special.get("restraintRing", "0")),
        "weaponRing": str(special.get("weaponRing", "0")),
        "ringofSum": str(special.get("ringOfSum", "0")),
        "riskTaker": str(special.get("riskTaker", "0")),
        "contiRing": str(special.get("continuosRing", "0")),
        "destiny2ndSkill": bool(special.get("destiny2ndSkill", False)),
    }


async def fetch_pet_corrected_maplescouter_result(
    nickname: str,
    raw_data: dict,
    atk_value: int,
):
    if not MAPLESCOUTER_API_KEY:
        print("MAPLESCOUTER_API_KEY is not set")
        return None

    user_stat = build_user_stat_for_simulator(raw_data)

    if not user_stat:
        print("Failed to build userStat for dmg-simulator")
        return None

    simulator = build_simulator_payload(user_stat, atk_value)

    api_url = "https://api.maplescouter.com/api/calc/dmg-simulator"

    headers = {
        "accept": "*/*",
        "accept-language": "ko,en;q=0.9,en-US;q=0.8",
        "api-key": MAPLESCOUTER_API_KEY,
        "cache-control": "public, max-age=300",
        "content-type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Origin": "https://maplescouter.com",
        "Referer": "https://maplescouter.com/",
    }

    payload = {
        "userStat": user_stat,
        "simulator": simulator,
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0),
            follow_redirects=True
        ) as client:
            response = await client.post(api_url, headers=headers, json=payload)

        print("==== MAPLESCOUTER DMG SIMULATOR ====")
        print("STATUS:", response.status_code)
        print("TEXT SAMPLE:", response.text[:1000])
        print("=====================================")

        if response.status_code not in {200, 201}:
            return None

        data = response.json()

        general_380 = data.get("boss380_stat")
        hexa_380 = data.get("boss380_hexaStat")

        if general_380 is None and hexa_380 is None:
            return None

        return {
            "general_380": general_380,
            "hexa_380": hexa_380,
            "combat_power": data.get("combatPower"),
            "pet_correction_applied": True,
            "pet_attack_correction": atk_value,
        }

    except Exception as e:
        print("Maplescouter dmg-simulator error:", repr(e))
        return None


async def fetch_maplescouter_api(nickname: str):
    cached = get_cached_maplescouter(nickname)

    if cached:
        return cached

    if not MAPLESCOUTER_API_KEY:
        print("MAPLESCOUTER_API_KEY is not set")
        return None

    api_url = "https://api.maplescouter.com/api/id"

    params = {
        "name": nickname,
        "preset": "00000",
        "region": "kms"
    }

    headers = {
        "accept": "*/*",
        "accept-language": "ko,en;q=0.9,en-US;q=0.8",
        "api-key": MAPLESCOUTER_API_KEY,
        "cache-control": "public, max-age=300",
        "content-type": "application/json",
        "priority": "u=1, i",
        "sec-ch-ua": '"Microsoft Edge";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Origin": "https://maplescouter.com",
        "Referer": "https://maplescouter.com/",
    }

    try:
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
            response = await client.get(api_url, headers=headers, params=params)

        print("==== MAPLESCOUTER API REQUEST ====")
        print("URL:", str(response.request.url))
        print("STATUS:", response.status_code)
        print("TEXT SAMPLE:", response.text[:3000])
        print("===================================")

        if response.status_code not in {200, 201}:
            return None

        data = response.json()

        parsed = extract_maplescouter_values(data)

        if not parsed:
            print("Maplescouter parse failed:", data)
            return None

        parsed["pet_correction_needed"] = needs_pet_attack_correction(nickname, data)
        parsed["pet_correction_applied"] = False
        parsed["pet_attack_correction"] = 0

        if parsed["pet_correction_needed"]:
            atk_value = PET_ATTACK_CORRECTION[nickname]
            corrected = await fetch_pet_corrected_maplescouter_result(
                nickname=nickname,
                raw_data=data,
                atk_value=atk_value,
            )

            if corrected:
                parsed.update(corrected)
            else:
                parsed["pet_attack_correction"] = atk_value

        print("==== PARSED MAPLESCOUTER VALUES ====")
        print(parsed)
        print("=====================================")

        parsed["api_url"] = str(response.request.url)
        set_cached_maplescouter(nickname, parsed)

        return parsed

    except Exception as e:
        print("Maplescouter API error:", repr(e))
        return None


async def build_maplescouter_all_response(
    character_names: list[str],
    title: str,
):
    # 동시에 너무 많이 요청하면 ReadTimeout이 잘 나므로 2개씩만 처리
    semaphore = asyncio.Semaphore(2)

    async def fetch_one(nickname: str):
        try:
            async with semaphore:
                data = await fetch_maplescouter_api(nickname)

            if not data:
                return {
                    "nickname": nickname,
                    "success": False,
                    "general_380": None,
                    "hexa_380": None,
                    "pet_correction_needed": False,
                    "pet_correction_applied": False,
                    "pet_attack_correction": 0,
                }

            return {
                "nickname": nickname,
                "success": True,
                "general_380": data.get("general_380"),
                "hexa_380": data.get("hexa_380"),
                "pet_correction_needed": data.get("pet_correction_needed", False),
                "pet_correction_applied": data.get("pet_correction_applied", False),
                "pet_attack_correction": data.get("pet_attack_correction", 0),
            }

        except Exception as e:
            print(f"Maplescouter all fetch error for {nickname}:", repr(e))

            return {
                "nickname": nickname,
                "success": False,
                "general_380": None,
                "hexa_380": None,
                "pet_correction_needed": False,
                "pet_correction_applied": False,
                "pet_attack_correction": 0,
            }

    results = await asyncio.gather(
        *[fetch_one(name) for name in character_names]
    )

    success_results = [r for r in results if r["success"]]
    failed_results = [r for r in results if not r["success"]]

    success_results.sort(
        key=lambda r: stat_to_int(r["hexa_380"]),
        reverse=True
    )

    lines = [
        title,
        "헥사환산(380) 기준 내림차순",
        "─────────────────",
    ]

    for idx, r in enumerate(success_results, start=1):
        correction_text = ""

        if r.get("pet_correction_applied"):
            correction_text = (
                f"\n   보정: 펫 누락 공+{r.get('pet_attack_correction')} 적용"
            )
        elif r.get("pet_correction_needed"):
            correction_text = (
                f"\n   보정: 펫 누락 공+{r.get('pet_attack_correction')} 적용 실패"
            )

        lines.append(
            f"{idx}. {r['nickname']}\n"
            f"   환산(380): {format_stat(r['general_380'])}\n"
            f"   헥사환산(380): {format_stat(r['hexa_380'])}"
            f"{correction_text}"
        )

    if failed_results:
        lines.append("─────────────────")
        lines.append("조회 실패:")

        for r in failed_results:
            lines.append(f"- {r['nickname']}")

    return simple_text("\n".join(lines))


async def refresh_maplescouter_all_cache(
    cache_key: str,
    character_names: list[str],
    title: str,
):
    try:
        print(f"Maplescouter all cache refresh started: {cache_key}")

        response = await build_maplescouter_all_response(
            character_names=character_names,
            title=title,
        )

        text = response["template"]["outputs"][0]["simpleText"]["text"]

        MAPLESCOUTER_ALL_CACHES[cache_key]["saved_time"] = time.time()
        MAPLESCOUTER_ALL_CACHES[cache_key]["text"] = text

        print(f"Maplescouter all cache refreshed: {cache_key}")

    except Exception as e:
        print(f"Maplescouter all cache refresh error [{cache_key}]:", repr(e))

    finally:
        MAPLESCOUTER_ALL_REFRESH_TASKS[cache_key] = None


def is_maplescouter_all_refreshing(cache_key: str) -> bool:
    task = MAPLESCOUTER_ALL_REFRESH_TASKS.get(cache_key)

    return task is not None and not task.done()


def start_maplescouter_all_refresh(
    cache_key: str,
    character_names: list[str],
    title: str,
):
    if not is_maplescouter_all_refreshing(cache_key):
        MAPLESCOUTER_ALL_REFRESH_TASKS[cache_key] = asyncio.create_task(
            refresh_maplescouter_all_cache(
                cache_key=cache_key,
                character_names=character_names,
                title=title,
            )
        )


async def make_maplescouter_all_result(
    cache_key: str,
    character_names: list[str],
    title: str,
):
    cache = MAPLESCOUTER_ALL_CACHES[cache_key]
    cached_text = cache.get("text")
    saved_time = cache.get("saved_time", 0)
    cache_age = time.time() - saved_time

    # 1. 캐시가 있고 5분 이내면 바로 출력
    if cached_text and cache_age <= MAPLESCOUTER_ALL_CACHE_TTL:
        return simple_text(cached_text)

    # 2. 캐시는 없고, 이미 조회 중이면 조회 중 안내만 출력
    if not cached_text and is_maplescouter_all_refreshing(cache_key):
        return simple_text(
            "아직 전체 환산 조회 중입니다.\n"
            "잠시 후 다시 입력해주세요."
        )

    # 3. 캐시는 없고, 조회 중도 아니면 조회 시작
    if not cached_text:
        start_maplescouter_all_refresh(
            cache_key=cache_key,
            character_names=character_names,
            title=title,
        )

        return simple_text(
            "전체 환산 조회를 시작했습니다.\n"
            "약 10~20초 후 다시 입력해주세요."
        )

    # 4. 캐시는 있지만 오래됐으면, 일단 이전 결과를 보여주고 백그라운드 갱신
    if cached_text and cache_age > MAPLESCOUTER_ALL_CACHE_TTL:
        if is_maplescouter_all_refreshing(cache_key):
            return simple_text(
                cached_text
                + "\n\n※ 이전 조회 결과입니다. 최신 값으로 갱신 중입니다."
            )

        start_maplescouter_all_refresh(
            cache_key=cache_key,
            character_names=character_names,
            title=title,
        )

        return simple_text(
            cached_text
            + "\n\n※ 이전 조회 결과입니다. 최신 값으로 갱신을 시작했습니다."
        )


async def make_maplescouter_card(nickname: str):
    if not is_valid_nickname(nickname):
        return simple_text(
            f"'{nickname}' 닉네임 형식이 올바르지 않아요.\n"
            "닉네임을 다시 확인해주세요."
        )

    encoded_name = quote(nickname)

    info_url = f"https://maplescouter.com/info?name={encoded_name}&preset=00000"
    result_url = f"https://maplescouter.com/result?name={encoded_name}&preset=00000"
    spec_order_url = f"https://maplescouter.com/spec-order?name={encoded_name}&preset=00000"

    result_data = await fetch_maplescouter_api(nickname)

    if not result_data:
        return {
            "version": "2.0",
            "template": {
                "outputs": [
                    {
                        "textCard": {
                            "title": f"{nickname} 님의 환산 정보",
                            "description": (
                                "환산 값을 자동으로 불러오지 못했어요.\n"
                                "아래 버튼을 눌러 Maplescouter에서 직접 확인해주세요."
                            ),
                            "buttons": [
                                {
                                    "action": "webLink",
                                    "label": "환산 보기",
                                    "webLinkUrl": info_url
                                },
                                {
                                    "action": "webLink",
                                    "label": "효율·보스컷 보기",
                                    "webLinkUrl": result_url
                                },
                                {
                                    "action": "webLink",
                                    "label": "스펙업 순서 보기",
                                    "webLinkUrl": spec_order_url
                                }
                            ]
                        }
                    }
                ]
            }
        }

    general_380 = format_stat(result_data.get("general_380"))
    hexa_380 = format_stat(result_data.get("hexa_380"))
    combat_power = result_data.get("combat_power")

    description_lines = [
        f"환산(380): {general_380}",
        f"헥사환산(380): {hexa_380}",
    ]

    if result_data.get("pet_correction_applied"):
        description_lines.append(
            f"보정: 펫 누락으로 공격력 +{result_data.get('pet_attack_correction')} 적용"
        )
    elif result_data.get("pet_correction_needed"):
        description_lines.append(
            f"보정: 펫 누락 의심, 공격력 +{result_data.get('pet_attack_correction')} 적용 실패"
        )

    if combat_power:
        description_lines.append(f"전투력: {format_korean_number(combat_power)}")

    description_lines.append("기준: Maplescouter 실시간 조회")

    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "textCard": {
                        "title": f"{nickname} 님의 환산 정보",
                        "description": "\n".join(description_lines),
                        "buttons": [
                            {
                                "action": "webLink",
                                "label": "환산 보기",
                                "webLinkUrl": info_url
                            },
                            {
                                "action": "webLink",
                                "label": "효율·보스컷 보기",
                                "webLinkUrl": result_url
                            },
                            {
                                "action": "webLink",
                                "label": "스펙업 순서 보기",
                                "webLinkUrl": spec_order_url
                            }
                        ]
                    }
                }
            ]
        }
    }


def make_distribution_guide():
    return simple_text(
        "보스 분배 계산기입니다.\n\n"
        "수수료는 3%로 계산해요.\n"
        "아래처럼 입력해주세요.\n\n"
        "분배 300억 6명\n"
        "분배 35000000000 2명\n\n"
        "계산 방식:\n"
        "1차 경매장 수수료 3%를 뗀 뒤,\n"
        "파티원이 받을 때 다시 3%가 빠지는 것까지 고려해\n"
        "파티장과 파티원의 최종 수령액이 같아지도록 계산합니다."
    )


def calculate_distribution(total_sale: int, party_count: int):
    if party_count < 2 or party_count > 6:
        return simple_text("인원수는 2명부터 6명까지 입력해주세요.")

    after_auction_fee = floor_to_100(total_sale * (1 - FEE_RATE))

    transfer_to_each_member = floor_to_100(
        after_auction_fee / (party_count - FEE_RATE)
    )

    leader_final = after_auction_fee - transfer_to_each_member * (party_count - 1)
    member_final = floor_to_100(transfer_to_each_member * (1 - FEE_RATE))
    error = abs(leader_final - member_final)

    text = (
        "보스 분배 계산 결과입니다.\n"
        "수수료: 3%\n\n"
        f"경매장 판매액\n"
        f"{total_sale:,} 메소\n"
        f"({format_meso(total_sale)})\n\n"
        f"1차 실제 수령액\n"
        f"{after_auction_fee:,} 메소\n"
        f"({format_meso(after_auction_fee)})\n\n"
        f"인원수\n"
        f"{party_count}명\n\n"
        f"각 파티원에게 보낼 금액\n"
        f"{transfer_to_each_member:,} 메소\n"
        f"({format_meso(transfer_to_each_member)})\n\n"
        f"파티원 실수령액\n"
        f"{member_final:,} 메소\n"
        f"({format_meso(member_final)})\n\n"
        f"파티장 최종 보유액\n"
        f"{leader_final:,} 메소\n"
        f"({format_meso(leader_final)})\n\n"
        f"오차\n"
        f"{error:,} 메소"
    )

    return simple_text(text)


def handle_distribution_command(utterance: str):
    if utterance in {"분배계산기", "분배 계산기", "보스분배", "보스 분배"}:
        return make_distribution_guide()

    if utterance.startswith("분배"):
        amount = parse_meso_amount(utterance)
        party_count = parse_party_count(utterance)

        if amount is None or party_count is None:
            return simple_text(
                "분배 계산 형식이 올바르지 않아요.\n\n"
                "예시:\n"
                "분배 300억 6명\n"
                "분배 35000000000 2명"
            )

        return calculate_distribution(amount, party_count)

    return None


async def handle_maplescouter_command(utterance: str):
    normalized = utterance.strip()
    normalized = normalized.replace("\u00a0", " ")
    normalized = normalized.replace("\u200b", "")
    normalized = normalized.replace("\ufeff", "")
    normalized = re.sub(r"\s+", " ", normalized)

    # 기존 본섭 7캐릭 전체 조회
    if re.fullmatch(r"!?환산\s*(all|전체)", normalized, re.IGNORECASE):
        return await make_maplescouter_all_result(
            cache_key="normal",
            character_names=ALL_CHARACTERS,
            title="[ 환산 전체 조회 ]",
        )

    # 챌린저스 서버 6캐릭 전체 조회
    if re.fullmatch(r"!?챌섭환산\s*(all|전체)", normalized, re.IGNORECASE):
        return await make_maplescouter_all_result(
            cache_key="challenge",
            character_names=CHALLENGE_CHARACTERS,
            title="[ 챌섭 환산 전체 조회 ]",
        )

    # 개별 환산 조회
    match = re.search(r"^!?환산\s+(.+?)\s*$", normalized)

    if match:
        nickname = match.group(1).strip()
        return await make_maplescouter_card(nickname)

    return None


async def get_ocid(character_name: str) -> tuple[str | None, str | None]:
    if not NEXON_API_KEY:
        return None, "NEXON_API_KEY가 설정되지 않았어요."

    url = f"{BASE_URL}/id"

    headers = {
        "x-nxopen-api-key": NEXON_API_KEY
    }

    params = {
        "character_name": character_name
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers=headers, params=params)

        if response.status_code == 200:
            data = response.json()
            return data.get("ocid"), None

        if response.status_code == 400:
            return None, None

        if response.status_code == 403:
            return None, "Nexon Open API 키를 확인해주세요."

        if response.status_code == 429:
            return None, "API 요청이 많아요. 잠시 후 다시 시도해주세요."

        return None, f"캐릭터 조회 중 오류가 발생했어요. ({response.status_code})"

    except httpx.TimeoutException:
        return None, "캐릭터 조회 응답이 지연되고 있어요. 잠시 후 다시 시도해주세요."

    except Exception:
        return None, "캐릭터 조회 중 알 수 없는 오류가 발생했어요."


async def get_character_basic(ocid: str, date_str: str):
    url = f"{BASE_URL}/character/basic"

    headers = {
        "x-nxopen-api-key": NEXON_API_KEY
    }

    params = {
        "ocid": ocid,
        "date": date_str
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers=headers, params=params)

        if response.status_code != 200:
            return None

        data = response.json()

        if not data.get("character_level"):
            return None

        return data

    except Exception:
        return None


def to_date_param(d):
    return d.strftime("%Y-%m-%d")


def to_date_kr(d):
    return f"{d.year}년 {d.month}월 {d.day}일"


async def handle_exp_command(utterance: str):
    match = re.search(r"^!?경험치\s+(.+?)\s*$", utterance)

    if not match:
        return None

    character_name = match.group(1).strip()

    if not is_valid_nickname(character_name):
        return simple_text(
            f"'{character_name}' 닉네임 형식이 올바르지 않아요.\n"
            "닉네임을 다시 확인해주세요."
        )

    ocid, error_message = await get_ocid(character_name)

    if error_message:
        return simple_text(error_message)

    if not ocid:
        return simple_text(
            f"'{character_name}' 닉네임을 찾지 못했어요.\n"
            "닉네임을 다시 확인해주세요."
        )

    records = []

    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst).date()

    days_to_show = 7

    for i in range(days_to_show, -1, -1):
        d = today - timedelta(days=1 + i)
        date_str = to_date_param(d)

        info = await get_character_basic(ocid, date_str)

        if not info:
            continue

        try:
            records.append({
                "date": date_str,
                "level": int(info.get("character_level")),
                "rate": float(info.get("character_exp_rate"))
            })
        except Exception:
            continue

    if len(records) < 2:
        return simple_text(
            "[오류] 데이터가 부족합니다.\n"
            f"수집된 날짜: {len(records)}일\n"
            "캐릭터가 최근 생성됐거나 API 조회 기간 이전일 수 있습니다."
        )

    current = records[-1]

    daily_gains = []
    total_gain = 0.0

    for j in range(1, len(records)):
        prev = records[j - 1]
        curr = records[j]

        if curr["level"] == prev["level"]:
            gain = curr["rate"] - prev["rate"]
        else:
            full_levels = curr["level"] - prev["level"] - 1
            gain = (100 - prev["rate"]) + (full_levels * 100) + curr["rate"]

        total_gain += gain

        daily_gains.append({
            "date": curr["date"][5:],
            "rate": curr["rate"],
            "gain": gain
        })

    days_tracked = len(daily_gains)
    daily_avg = total_gain / days_tracked if days_tracked > 0 else 0
    remaining = 100 - current["rate"]

    if daily_avg <= 0:
        level_up_str = f"계산 불가. 최근 {days_tracked}일간 경험치 변화 없음"
    else:
        days_left = math.ceil(remaining / daily_avg)
        level_up_date = today - timedelta(days=1) + timedelta(days=days_left)
        level_up_str = f"{to_date_kr(level_up_date)} (약 {days_left}일 후)"

    daily_lines = "".join(
        [
            f"\n  {g['date']}  {g['rate']:.2f}%  "
            f"({'+' if g['gain'] >= 0 else ''}{g['gain']:.2f}%)"
            for g in daily_gains
        ]
    )

    text = (
        f"[ {character_name} 경험치 리포트 ]\n"
        "─────────────────\n"
        f"현재: Lv.{current['level']}  {current['rate']:.2f}%\n"
        f"기간: {records[1]['date'][5:]} ~ {current['date'][5:]}"
        f" ({days_tracked}일)\n"
        f"기간 총 획득: +{total_gain:.2f}%\n"
        f"일평균 획득: +{daily_avg:.2f}%\n"
        f"다음 레벨까지: {remaining:.2f}% 남음\n"
        f"예상 레벨업: {level_up_str}\n"
        "─────────────────\n"
        "일별 경험치:"
        f"{daily_lines}"
    )

    return simple_text(text)


@app.post("/kakao/skill")
async def kakao_skill(request: Request):
    body = await request.json()

    utterance = body.get("userRequest", {}).get("utterance", "")
    utterance = remove_bot_mention(utterance)

    exp_response = await handle_exp_command(utterance)

    if exp_response:
        return exp_response

    distribution_response = handle_distribution_command(utterance)

    if distribution_response:
        return distribution_response

    maplescouter_response = await handle_maplescouter_command(utterance)

    if maplescouter_response:
        return maplescouter_response

    return simple_text(
        "사용 가능한 명령어입니다.\n\n"
        "1. 환산 조회\n"
        "환산 닉네임\n"
        "환산 all\n"
        "챌섭환산 all\n\n"
        "2. 보스 분배 계산기\n"
        "분배계산기\n"
        "분배 300억 6명\n\n"
        "3. 경험치 조회\n"
        "경험치 닉네임"
    )