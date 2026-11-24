import pytest

from app.services.regions import region_from_address, resolve_region


@pytest.mark.parametrize("addr,expected", [
    ("경남 창원시 성산구 공단로 1", "경상남도 창원시"),
    ("경상남도 창원시 의창구 2", "경상남도 창원시"),          # T18: 표기 차이
    ("경남 김해시 주촌면 3", "경상남도 김해시"),
    ("경상남도창원시성산구", "경상남도 창원시"),
    ("부산광역시 강서구 녹산산단", "부산광역시"),              # 광역시는 전체가 한 지역
    ("부산 사하구 4", "부산광역시"),
    ("서울 금천구 5", "서울특별시"),
    ("충남 당진시 송악읍 6", "충청남도 당진시"),
    ("경기도 광주시 오포읍", "경기도 광주시"),
    ("경북 고령군 다산면", "경상북도 고령군"),
    ("경기 시흥시 정왕동", "경기도 시흥시"),
    ("창원시 성산구", None),                                    # 시·도 없음
    ("", None),
    (None, None),
])
def test_region_from_address(addr, expected):
    assert region_from_address(addr) == expected


def test_resolve_prefers_address():
    assert resolve_region("경남 창원시 성산구", "M1EN64") == ("경상남도 창원시", "address")
    assert resolve_region(None, "M1EN64") == ("M1EN64", "route")
    assert resolve_region("알수없는주소", "M1EN64") == ("M1EN64", "route")
    assert resolve_region(None, None) == (None, None)
