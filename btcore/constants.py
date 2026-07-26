COMMISSION_RATE = 0.00015
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.0005
TRANSFER_FEE_RATE = 0.00001

TICK_SIZE = 0.01
CAL_DAYS_ANNUAL = 244

PLATE_LIMIT_RULES: dict[str, dict] = {
    "BJ": {"rate": 0.30},
    "688": {"rate": 0.20},
    "300": {"rate": 0.10, "switch_date": "20200824", "new_rate": 0.20},
    "301": {"rate": 0.10, "switch_date": "20200824", "new_rate": 0.20},
    "default": {"rate": 0.10},
}

