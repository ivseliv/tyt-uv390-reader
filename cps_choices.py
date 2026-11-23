"""Generate the native editor's stock CPS selection lists at build time."""

import json

from cps_channel import ANALOG, COMMON, DIGITAL, YES_NO
from cps_dtmf import GROUP_CODE, GROUP_CODES, PTT_ID, PTT_MODES, SIDE_TONE
from cps_section_editor import FLAGS
from cps_sections import (
    BUTTONS, EMERGENCY_MODES, EMERGENCY_TYPES, GPS_FORMATS, INTRO_SCREENS, KEYPAD_TIMES,
    MAIN_MODES, MENU_BITS, MONITOR_TYPES, PERMIT_TONES, PRESSES, SIDE_MODES, TX_MODES, VFO_POWER, VFO_WIDTH,
)


def choices():
    toggles = list(YES_NO.values())
    channels = {}
    for label, _, _, _, options, _ in COMMON + DIGITAL + ANALOG:
        if isinstance(options, dict):
            channels[label] = list(options.values())
    buttons = [BUTTONS[idx] for idx in sorted(BUTTONS)]
    return {
        "channels": channels,
        "general": {
            **{label: toggles for label in FLAGS},
            "Выбор канала передачи": TX_MODES, "Формат отчёта GPS": GPS_FORMATS,
            "Автоблокировка клавиатуры": list(KEYPAD_TIMES.values()), "Монитор": MONITOR_TYPES,
            "Заставка": INTRO_SCREENS, "Сигнал разрешения передачи": PERMIT_TONES,
            "Подсветка": ["Всегда", "5 с", "10 с", "15 с"],
            "Часовой пояс": [f"UTC{offset:+d}" for offset in range(-12, 13)],
        },
        "buttons": {label: buttons for label in PRESSES},
        "menu": {label: toggles for label, _, _, _, _ in MENU_BITS},
        "emergency": {
            "Тип": EMERGENCY_TYPES, "Режим": EMERGENCY_MODES,
            **{label: toggles for label in ("Мониторинг при тревоге", "Удалённый мониторинг", "Удалённое отключение")},
        },
        "vfo": {
            "Режим": ["Аналог", "DMR"],
            "Основной режим": MAIN_MODES, "Сторона A": SIDE_MODES, "Сторона B": SIDE_MODES,
            "Шаг": [f"{hz} Гц" for hz in range(2500, 50001, 2500)],
            "Time Slot": ["1", "2"], "Color Code": [str(code) for code in range(16)],
            "Полоса": VFO_WIDTH, "Мощность": list(VFO_POWER.values()), "Только приём": toggles,
        },
        "dtmf": {"Профиль": [f"DTMF-{i}" for i in range(1, 5)]},
        "dtmf_systems": {SIDE_TONE: toggles, PTT_ID: PTT_MODES, GROUP_CODE: GROUP_CODES},
    }


if __name__ == "__main__":
    print(json.dumps(choices(), ensure_ascii=False, indent=2))
