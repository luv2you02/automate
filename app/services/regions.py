"""지역 판별 (PRD R3).

- 인도처 주소표에 주소가 있으면 → 시 단위 지역 키 (예: '경상남도 창원시')
  · 특별시·광역시·세종은 시·도 전체가 하나의 지역
  · '경남'/'경상남도', '서울'/'서울특별시' 같은 표기 차이는 정규화
- 없으면 → 경매 시트의 운송경로 코드 (예: 'M1EN64')
두 출처는 서로 다른 지역으로 취급한다 (region_source 로 구분).
"""
from __future__ import annotations

import re

# 정규 시·도 이름 → 표기 변형
_SIDO: dict[str, list[str]] = {
    "서울특별시": ["서울특별시", "서울시", "서울"],
    "부산광역시": ["부산광역시", "부산시", "부산"],
    "대구광역시": ["대구광역시", "대구시", "대구"],
    "인천광역시": ["인천광역시", "인천시", "인천"],
    "광주광역시": ["광주광역시", "광주시", "광주"],  # 경기도 광주시와 구분: 앞에 도가 없을 때만
    "대전광역시": ["대전광역시", "대전시", "대전"],
    "울산광역시": ["울산광역시", "울산시", "울산"],
    "세종특별자치시": ["세종특별자치시", "세종시", "세종"],
    "경기도": ["경기도", "경기"],
    "강원특별자치도": ["강원특별자치도", "강원도", "강원"],
    "충청북도": ["충청북도", "충북"],
    "충청남도": ["충청남도", "충남"],
    "전북특별자치도": ["전북특별자치도", "전라북도", "전북"],
    "전라남도": ["전라남도", "전남"],
    "경상북도": ["경상북도", "경북"],
    "경상남도": ["경상남도", "경남"],
    "제주특별자치도": ["제주특별자치도", "제주도", "제주"],
}
METRO = {"서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시", "울산광역시", "세종특별자치시"}

# 긴 표기부터 매칭해야 '경상남도'가 '경남'보다 먼저 잡힌다
_VARIANTS = sorted(((v, k) for k, vs in _SIDO.items() for v in vs), key=lambda x: -len(x[0]))


def region_from_address(address: str | None) -> str | None:
    """주소 → 지역 키. 판별 못 하면 None."""
    if not address:
        return None
    s = re.sub(r"\s+", " ", address.replace("　", " ")).strip()
    for variant, sido in _VARIANTS:
        if s.startswith(variant):
            rest = s[len(variant):].strip()
            if sido in METRO:
                return sido
            m = re.match(r"([가-힣]+?[시군])(?:\s|$|[가-힣]+[구읍면동])", rest + " ")
            if m:
                return f"{sido} {m.group(1)}"
            return None
    return None


def resolve_region(address: str | None, route: str | None) -> tuple[str | None, str | None]:
    """(region, region_source). 주소 우선, 없으면 운송경로."""
    r = region_from_address(address)
    if r:
        return r, "address"
    if route:
        return route, "route"
    return None, None
