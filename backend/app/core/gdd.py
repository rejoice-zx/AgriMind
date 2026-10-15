"""Thermal sums only: no uncalibrated crop-stage or harvest prediction."""
from datetime import date, timedelta
from app.core.weather_policy import agriculture_today, finite_number


def calculate_thermal_sum(t_max=None, t_min=None, t_base=10.0, daily_temps=None, crop="番茄"):
    base = finite_number(t_base, -100, 70)
    if base is None:
        raise ValueError("基温必须是有效有限温度")
    if daily_temps == []:
        raise ValueError("缺少历史温度序列")
    samples = daily_temps if daily_temps is not None else [
        {"date": agriculture_today().isoformat(), "t_max": t_max, "t_min": t_min}
    ]
    records, total, previous = [], 0.0, None
    for item in samples:
        raw_date = item.get("date")
        try:
            day = date.fromisoformat(raw_date)
            if day.isoformat() != raw_date or day > agriculture_today():
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("温度记录须提供有效且非未来的 YYYY-MM-DD 日期") from None
        if previous is not None and day != previous + timedelta(days=1):
            raise ValueError("温度序列日期须连续递增，不能重复或缺日")
        mx, mn = finite_number(item.get("t_max"), -100, 70), finite_number(item.get("t_min"), -100, 70)
        if mx is None or mn is None or mx < mn:
            raise ValueError("最高、最低温度须完整有效，且最高温度不低于最低温度")
        average = (mx + mn) / 2
        daily = max(0.0, average - base)
        total += daily
        records.append(dict(date=raw_date, t_max=mx, t_min=mn, t_avg=round(average, 1), gdd_day=round(daily, 1)))
        previous = day
    return dict(crop=crop, t_base=base, accumulated_gdd=round(total, 1), records=records,
                predicted_growth_stage=None, lifecycle_completion_pct=None, stage_standards=[],
                calibrated=False, notice="仅计算所提供日期范围内的热量和；尚无品种、设施、起算点校准，不能推断物候或采收日期。")
