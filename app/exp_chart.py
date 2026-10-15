"""User-supplied Free Fire cumulative EXP chart, levels 1–65.

This is a lookup, not a live game-server response. The dashboard only computes
remaining EXP when the account has a server-returned profile and its reported
level/EXP agree with this chart.
"""
from typing import Any, Dict

# Cumulative EXP at the START of each level, as supplied by the user.
LEVEL_START_EXP = (
    0, 48, 202, 544, 1012, 1844, 2792, 3800, 4870, 6004,
    7192, 8448, 9760, 11140, 12566, 14060, 15610, 17224, 18902,
    20832, 22424, 24278, 26192, 28166, 30200, 32294, 34448,
    37804, 41274, 44870, 48582, 53394, 58566, 64096, 69994,
    76460, 83506, 91128, 99322, 108092, 120144, 133266, 147472,
    162760, 179126, 196572, 215368, 235516, 257010, 279860,
    304056, 348318, 394982, 444044, 495508, 549364, 633756,
    721744, 813336, 908522, 1041438, 1180352, 1325266, 1476184,
    1634300,
)
LEVEL_65_TO_66 = 206646  # Supplied amount needed for level 65 -> 66.
# Three provided 'needed' values disagree with the NEXT ROW's total EXP.
# Next-level cumulative thresholds take precedence for calculations.
CONFLICTING_SUPPLIED_NEEDS = {19: 1730, 20: 1792, 54: 51484}


def exp_progress(level: int, total_exp: int, verified_profile: bool) -> Dict[str, Any]:
    """Return a clearly labelled chart-derived estimate; never invent live EXP."""
    result: Dict[str, Any] = {
        "exp_chart_source": "user_supplied_levels_1_65",
        "exp_chart_row_conflict": level in CONFLICTING_SUPPLIED_NEEDS,
        "exp_chart_status": "awaiting_profile",
        "exp_level_start": None,
        "exp_next_level_total": None,
        "exp_needed_for_level": None,
        "exp_to_next_level": None,
        "exp_level_progress_percent": None,
    }
    if not verified_profile:
        return result
    if not 1 <= level <= 65:
        result["exp_chart_status"] = "outside_chart"
        return result

    start = LEVEL_START_EXP[level - 1]
    next_total = LEVEL_START_EXP[level] if level < 65 else start + LEVEL_65_TO_66
    result.update(
        exp_level_start=start,
        exp_next_level_total=next_total,
        exp_needed_for_level=next_total - start,
    )
    # A stale level, incompatible game version or unexpected server EXP must
    # be shown as a mismatch instead of '0 EXP left' or a fake progress value.
    if not start <= total_exp < next_total:
        result["exp_chart_status"] = "mismatch"
        return result

    result["exp_chart_status"] = "calculated"
    result["exp_to_next_level"] = next_total - total_exp
    result["exp_level_progress_percent"] = round(
        100 * (total_exp - start) / (next_total - start), 1
    )
    return result


assert len(LEVEL_START_EXP) == 65
assert all(b > a for a, b in zip(LEVEL_START_EXP, LEVEL_START_EXP[1:]))
