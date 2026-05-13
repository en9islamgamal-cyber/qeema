"""
data/curriculum.py
====================================================================
The episode curriculum for QEEMA v2.

Currently covers: Al-Fatiha + entire Juz Amma (38 episodes total).

Each episode can target a full surah OR a slice of a surah.
The pipeline doesn't care about episode "size" — it adapts the
video length to the content (1 ayah = ~50s, 10 ayahs = ~6min).

To add new episodes, just append to EPISODES dict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class EpisodeInfo:
    """Static metadata for one episode in the curriculum."""
    episode_number: int
    surah_number: int
    surah_name: str
    start_ayah: int
    end_ayah: int
    # Optional human-readable theme (for documentation only)
    theme: str = ""

    def ayah_count(self) -> int:
        return self.end_ayah - self.start_ayah + 1


# ════════════════════════════════════════════════════════════════════
# The Curriculum  —  38 episodes
# ════════════════════════════════════════════════════════════════════
# Episode 1 = Al-Fatiha (whole surah)
# Episodes 2-38 = Juz Amma in REVERSE order of mushaf
#   (i.e. shortest surahs first: An-Nas, Al-Falaq, Al-Ikhlas, ...)
#   because that's how kids typically learn.

EPISODES: Dict[int, EpisodeInfo] = {
    1:  EpisodeInfo(1,  1,   "الفاتحة", 1, 7,   "أم الكتاب"),

    # Mu'awwidhat — Protection surahs (start of Juz Amma in reverse)
    2:  EpisodeInfo(2,  114, "الناس",   1, 6,   "الاستعاذة من شر الناس"),
    3:  EpisodeInfo(3,  113, "الفلق",   1, 5,   "الاستعاذة من شر الخلق"),
    4:  EpisodeInfo(4,  112, "الإخلاص", 1, 4,   "صفات الله الواحد"),

    # Short core surahs
    5:  EpisodeInfo(5,  111, "المسد",   1, 5,   "عاقبة الكفر بالحق"),
    6:  EpisodeInfo(6,  110, "النصر",   1, 3,   "بشارة الفتح"),
    7:  EpisodeInfo(7,  109, "الكافرون", 1, 6,  "الفرق بين الإيمان والكفر"),
    8:  EpisodeInfo(8,  108, "الكوثر",   1, 3,  "نعمة الكوثر"),
    9:  EpisodeInfo(9,  107, "الماعون",  1, 7,  "تكذيب الدين"),
    10: EpisodeInfo(10, 106, "قريش",     1, 4,  "نعمة الإيلاف"),

    11: EpisodeInfo(11, 105, "الفيل",    1, 5,  "حماية الله للبيت"),
    12: EpisodeInfo(12, 104, "الهمزة",   1, 9,  "ذم الهمز واللمز"),
    13: EpisodeInfo(13, 103, "العصر",    1, 3,  "ميزان النجاح"),
    14: EpisodeInfo(14, 102, "التكاثر",  1, 8,  "الإلهاء بالدنيا"),
    15: EpisodeInfo(15, 101, "القارعة",  1, 11, "ميزان يوم القيامة"),
    16: EpisodeInfo(16, 100, "العاديات", 1, 11, "نكران الإنسان نعمة الله"),
    17: EpisodeInfo(17, 99,  "الزلزلة",  1, 8,  "أهوال يوم القيامة"),
    18: EpisodeInfo(18, 98,  "البينة",   1, 8,  "البرهان من السماء"),
    19: EpisodeInfo(19, 97,  "القدر",    1, 5,  "ليلة القدر"),
    20: EpisodeInfo(20, 96,  "العلق",    1, 19, "أول ما نزل من القرآن"),

    21: EpisodeInfo(21, 95,  "التين",    1, 8,  "تكريم الإنسان"),
    22: EpisodeInfo(22, 94,  "الشرح",    1, 8,  "تيسير الصدر"),
    23: EpisodeInfo(23, 93,  "الضحى",    1, 11, "العناية الإلهية"),
    24: EpisodeInfo(24, 92,  "الليل",    1, 21, "السعي والمآل"),
    25: EpisodeInfo(25, 91,  "الشمس",    1, 15, "تزكية النفس"),
    26: EpisodeInfo(26, 90,  "البلد",    1, 20, "اقتحام العقبة"),
    27: EpisodeInfo(27, 89,  "الفجر",    1, 30, "قصص الأمم وعبرتها"),
    28: EpisodeInfo(28, 88,  "الغاشية",  1, 26, "نعيم الجنة وجزاء النار"),
    29: EpisodeInfo(29, 87,  "الأعلى",   1, 19, "تسبيح اسم الله الأعلى"),
    30: EpisodeInfo(30, 86,  "الطارق",   1, 17, "علم الله بالنفس"),

    31: EpisodeInfo(31, 85,  "البروج",   1, 22, "صبر المؤمنين"),
    32: EpisodeInfo(32, 84,  "الانشقاق", 1, 25, "أحوال يوم القيامة"),
    33: EpisodeInfo(33, 83,  "المطففين", 1, 36, "العدل في الموازين"),
    34: EpisodeInfo(34, 82,  "الانفطار", 1, 19, "نسيان الإنسان لربه"),
    35: EpisodeInfo(35, 81,  "التكوير",  1, 29, "أهوال السماء"),
    36: EpisodeInfo(36, 80,  "عبس",      1, 42, "العتاب الإلهي"),
    37: EpisodeInfo(37, 79,  "النازعات", 1, 46, "قبض الأرواح"),
    38: EpisodeInfo(38, 78,  "النبأ",    1, 40, "النبأ العظيم"),
}


# ════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════

def get_episode_info(episode_number: int) -> Optional[EpisodeInfo]:
    """Look up an episode by its number. Returns None if not found."""
    return EPISODES.get(episode_number)


def list_all_episodes() -> Dict[int, EpisodeInfo]:
    """Return the full curriculum."""
    return dict(EPISODES)


def total_episodes() -> int:
    return len(EPISODES)


# ════════════════════════════════════════════════════════════════════
# Validation (runs at import)
# ════════════════════════════════════════════════════════════════════

def _validate_curriculum() -> None:
    """Sanity-check the curriculum at module load."""
    for ep_num, info in EPISODES.items():
        if info.episode_number != ep_num:
            raise ValueError(
                f"Curriculum key {ep_num} doesn't match "
                f"episode_number {info.episode_number}"
            )
        if info.end_ayah < info.start_ayah:
            raise ValueError(
                f"Episode {ep_num}: end_ayah ({info.end_ayah}) "
                f"< start_ayah ({info.start_ayah})"
            )
        if not (1 <= info.surah_number <= 114):
            raise ValueError(
                f"Episode {ep_num}: invalid surah_number "
                f"{info.surah_number}"
            )


_validate_curriculum()
