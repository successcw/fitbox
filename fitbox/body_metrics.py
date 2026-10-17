#!/usr/bin/env python3
"""Body composition from weight + foot-to-foot impedance (50 kHz), male branch of the Xiaomi Mi Body
Composition Scale 2 algorithm as reverse-engineered by the community (lolouk44/xiaomi_mi_scale
src/Xiaomi_Scale_Body_Metrics.py; same formulas in openScale MiScaleLib). Pure function, no I/O.
"""

import os
import config
HEIGHT_CM = float(os.environ['FITBOX_HEIGHT_CM']) if os.environ.get('FITBOX_HEIGHT_CM') else None
AGE = int(os.environ['FITBOX_AGE']) if os.environ.get('FITBOX_AGE') else None
SEX = os.environ.get('FITBOX_SEX')


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def metrics(weight, impedance, height=HEIGHT_CM, age=AGE, sex=SEX):
    if height is None or age is None or sex not in ('male', 'female'):
        return None  # 无个人配置时只记录体重、阻抗，不猜测体成分。
    female = sex == 'female'
    lbm = (height * 9.058 / 100) * (height / 100) + weight * 0.32 + 12.226 - impedance * 0.0068 - age * 0.0542
    const = (9.25 if age <= 49 else 7.25) if female else 0.8
    if not female and weight < 61:
        coef = 0.98
    elif female and weight > 60:
        coef = 0.96 * (1.03 if height > 160 else 1)
    elif female and weight < 50:
        coef = 1.02 * (1.03 if height > 160 else 1)
    else:
        coef = 1.0
    fat = clamp((1 - (lbm - const) * coef / weight) * 100, 5, 75)
    water = (100 - fat) * 0.7
    water = clamp(water * (1.02 if water <= 50 else 0.98), 35, 75)
    bone = -((0.245691014 if female else 0.18016894) - lbm * 0.05158)
    bone = clamp(bone + (0.1 if bone > 2.2 else -0.1), 0.5, 8)
    muscle = clamp(weight - fat / 100 * weight - bone, 10, 120)
    if female:
        bmr = 864.6 + weight * 10.2036 - height * 0.39336 - age * 6.204
    else:
        bmr = 877.8 + weight * 14.916 - height * 0.726 - age * 8.976
    if height < weight * 1.6 and not female:
        sub = -((height * 0.4) - (height * (height * 0.0826)))
        visceral = (weight * 305) / (sub + 48) - 2.9 + age * 0.15
    elif not female:
        sub = 0.765 + height * -0.0015
        visceral = -((height * 0.143) - (weight * sub)) + age * 0.15 - 5.0
    else:
        sub = ((height * 1.45) + (height * 0.1158) * height) - 120
        visceral = (weight * 500 / sub - 6) + age * 0.07
    return {'fat_pct': round(fat, 1), 'fat_kg': round(weight * fat / 100, 1), 'muscle_kg': round(muscle, 1),
            'water_pct': round(water, 1), 'bone_kg': round(bone, 1), 'visceral': round(clamp(visceral, 1, 50), 1),
            'bmr': round(clamp(bmr, 500, 10000)), 'bmi': round(weight / (height / 100) ** 2, 1)}
