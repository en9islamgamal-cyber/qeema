"""
data/curriculum.py — VALUE / QEEMA v22.5 — episode curriculum data (surahs, ayah ranges)
========================================================
المنهج التعليمي للقناة (38 حلقة من جزء عمّ).

[Design Decision]
- معزول في ملف منفصل لسهولة التعديل بدون لمس logic
- Frozen at module load: ما يتعدلش وقت التشغيل (immutable view)
- منهجياً مرتب من السهل (سور قصيرة) للأطول
"""
from __future__ import annotations

from typing import Final, TypedDict


class SurahInfo(TypedDict):
    """معلومات السورة في الحلقة."""
    surah: int      # رقم السورة (1-114)
    name: str       # اسم السورة بالعربية
    start: int      # رقم أول آية
    end: int        # رقم آخر آية


# Final = lint hint that this should never be reassigned.
CURRICULUM: Final[dict[int, SurahInfo]] = {
    1:  {"surah": 1,   "name": "الفاتحة",   "start": 1, "end": 7},
    2:  {"surah": 114, "name": "الناس",     "start": 1, "end": 6},
    3:  {"surah": 113, "name": "الفلق",     "start": 1, "end": 5},
    4:  {"surah": 112, "name": "الإخلاص",   "start": 1, "end": 4},
    5:  {"surah": 111, "name": "المسد",     "start": 1, "end": 5},
    6:  {"surah": 110, "name": "النصر",     "start": 1, "end": 3},
    7:  {"surah": 109, "name": "الكافرون",  "start": 1, "end": 6},
    8:  {"surah": 108, "name": "الكوثر",    "start": 1, "end": 3},
    9:  {"surah": 107, "name": "الماعون",   "start": 1, "end": 7},
    10: {"surah": 106, "name": "قريش",      "start": 1, "end": 4},
    11: {"surah": 105, "name": "الفيل",     "start": 1, "end": 5},
    12: {"surah": 104, "name": "الهمزة",    "start": 1, "end": 9},
    13: {"surah": 103, "name": "العصر",     "start": 1, "end": 3},
    14: {"surah": 102, "name": "التكاثر",   "start": 1, "end": 8},
    15: {"surah": 101, "name": "القارعة",   "start": 1, "end": 11},
    16: {"surah": 100, "name": "العاديات",  "start": 1, "end": 11},
    17: {"surah": 99,  "name": "الزلزلة",   "start": 1, "end": 8},
    18: {"surah": 98,  "name": "البينة",    "start": 1, "end": 8},
    19: {"surah": 97,  "name": "القدر",     "start": 1, "end": 5},
    20: {"surah": 96,  "name": "العلق",     "start": 1, "end": 19},
    21: {"surah": 95,  "name": "التين",     "start": 1, "end": 8},
    22: {"surah": 94,  "name": "الشرح",     "start": 1, "end": 8},
    23: {"surah": 93,  "name": "الضحى",     "start": 1, "end": 11},
    24: {"surah": 92,  "name": "الليل",     "start": 1, "end": 21},
    25: {"surah": 91,  "name": "الشمس",     "start": 1, "end": 15},
    26: {"surah": 90,  "name": "البلد",     "start": 1, "end": 20},
    27: {"surah": 89,  "name": "الفجر",     "start": 1, "end": 30},
    28: {"surah": 88,  "name": "الغاشية",   "start": 1, "end": 26},
    29: {"surah": 87,  "name": "الأعلى",    "start": 1, "end": 19},
    30: {"surah": 86,  "name": "الطارق",    "start": 1, "end": 17},
    31: {"surah": 85,  "name": "البروج",    "start": 1, "end": 22},
    32: {"surah": 84,  "name": "الانشقاق",  "start": 1, "end": 25},
    33: {"surah": 83,  "name": "المطففين",  "start": 1, "end": 36},
    34: {"surah": 82,  "name": "الانفطار",  "start": 1, "end": 19},
    35: {"surah": 81,  "name": "التكوير",   "start": 1, "end": 29},
    36: {"surah": 80,  "name": "عبس",       "start": 1, "end": 42},
    37: {"surah": 79,  "name": "النازعات",  "start": 1, "end": 46},
    38: {"surah": 78,  "name": "النبأ",     "start": 1, "end": 40},
}


def get_episode_info(episode_number: int) -> SurahInfo:
    """رجّع معلومات الحلقة. يرفع KeyError لو رقم الحلقة غير موجود."""
    if episode_number not in CURRICULUM:
        valid_range = f"1-{max(CURRICULUM.keys())}"
        raise KeyError(
            f"Episode {episode_number} not in curriculum. Valid: {valid_range}"
        )
    return CURRICULUM[episode_number]


def total_episodes() -> int:
    """العدد الكلي للحلقات في المنهج."""
    return len(CURRICULUM)
