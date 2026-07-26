"""测试 StockFilter 行业过滤（exclude_industries）。"""

from btcore.filters import StockFilter


class StubBackend:
    """只提供 StockFilter 所需方法的 stub backend。"""

    def __init__(self, industry_map=None, index_members=None, st_map=None):
        self._industry_map = industry_map or {}
        self._index_members = index_members or {}
        self._st_map = st_map or {}

    def get_stock_industries(self, ts_codes):
        return {c: self._industry_map[c] for c in ts_codes
                if c in self._industry_map}

    def get_st_symbols(self, trade_date):
        return set()

    def get_st_map(self, from_date):
        return dict(self._st_map)

    def get_recent_listings(self, cutoff_days=60, as_of=None):
        return set()

    def get_index_members(self, index_codes, start, end):
        return dict(self._index_members)


def make_bar(symbol):
    return {"symbol": symbol, "close": 10.0, "pe_ttm": 15.0}


class TestExcludeIndustries:
    """测试 exclude_industries 过滤规则。"""

    def test_exclude_banks(self):
        """排除银行后，银行股不在结果中。"""
        backend = StubBackend({
            "600036.SH": "银行",
            "600519.SH": "食品饮料",
            "601398.SH": "银行",
        })
        rules = {
            "exclude_industries": ["银行"],
            "exclude_st": False,
            "exclude_new_stock": False,
            "exclude_loss": False,
            "exclude_boards": [],
            "min_price": 0,
        }
        f = StockFilter(backend, "20240601", rules)
        bars = {
            "600036.SH": make_bar("600036.SH"),
            "600519.SH": make_bar("600519.SH"),
            "601398.SH": make_bar("601398.SH"),
        }
        result = f.filter(bars, "20240603")
        assert "600036.SH" not in result
        assert "601398.SH" not in result
        assert "600519.SH" in result
        assert len(result) == 1

    def test_empty_exclude_list_noop(self):
        """空列表不过滤任何股票。"""
        backend = StubBackend({
            "600036.SH": "银行",
            "600519.SH": "食品饮料",
        })
        rules = {
            "exclude_industries": [],
            "exclude_st": False,
            "exclude_new_stock": False,
            "exclude_loss": False,
            "exclude_boards": [],
            "min_price": 0,
        }
        f = StockFilter(backend, "20240601", rules)
        bars = {
            "600036.SH": make_bar("600036.SH"),
            "600519.SH": make_bar("600519.SH"),
        }
        result = f.filter(bars, "20240603")
        assert len(result) == 2

    def test_industry_map_lazy_loads_once(self):
        """行业映射只在首次 filter 时加载一次。"""
        call_count = [0]

        class CountingBackend(StubBackend):
            def get_stock_industries(self, ts_codes):
                call_count[0] += 1
                return super().get_stock_industries(ts_codes)

        backend = CountingBackend({"600036.SH": "银行"})
        rules = {
            "exclude_industries": ["银行"],
            "exclude_st": False,
            "exclude_new_stock": False,
            "exclude_loss": False,
            "exclude_boards": [],
            "min_price": 0,
        }
        f = StockFilter(backend, "20240601", rules)
        bars = {"600036.SH": make_bar("600036.SH")}

        f.filter(bars, "20240603")
        f.filter(bars, "20240604")
        # 只在首次调用时加载
        assert call_count[0] == 1


def _index_rules():
    return {
        "index_universe": ["000300.SH", "000905.SH"],
        "exclude_st": False,
        "exclude_new_stock": False,
        "exclude_loss": False,
        "exclude_boards": [],
        "min_price": 0,
    }


class TestIndexUniverse:
    """测试 index_universe 指数成分白名单（入场闸，逐日按快照取交集）。"""

    def _bars(self):
        return {s: make_bar(s) for s in
                ("000001.SZ", "600036.SH", "300750.SZ")}

    def test_membership_by_snapshot(self):
        """按当日最近一期快照过滤；成分调整即时生效。"""
        backend = StubBackend(index_members={
            "20240531": {"000001.SZ", "600036.SH"},
            # 6 月底调样: 600036 调出, 300750 调入
            "20240628": {"000001.SZ", "300750.SZ"},
        })
        f = StockFilter(backend, "20240601", _index_rules())

        r1 = f.filter(self._bars(), "20240603")
        assert set(r1) == {"000001.SZ", "600036.SH"}

        # 两期快照之间沿用较早一期
        r2 = f.filter(self._bars(), "20240627")
        assert set(r2) == {"000001.SZ", "600036.SH"}

        r3 = f.filter(self._bars(), "20240628")
        assert set(r3) == {"000001.SZ", "300750.SZ"}

    def test_before_first_snapshot_uses_first(self):
        """早于首期快照的日期回退用首期（月频粒度近似）。"""
        backend = StubBackend(index_members={"20240531": {"000001.SZ"}})
        f = StockFilter(backend, "20240506", _index_rules())
        assert set(f.filter(self._bars(), "20240506")) == {"000001.SZ"}

    def test_empty_snapshot_fail_open(self):
        """backend 无成分数据 → 告警并不生效（全量通过），不会静默清零股票池。"""
        backend = StubBackend(index_members={})
        f = StockFilter(backend, "20240601", _index_rules())
        assert set(f.filter(self._bars(), "20240603")) == set(self._bars())


class BareBackend:
    """不实现任何鸭子类型扩展方法的 backend。"""


class TestSoftFallback:
    """规则开启但 backend 缺方法：告警一次，该条规则不生效（软回退）。"""

    _BASE = {
        "exclude_st": False,
        "exclude_new_stock": False,
        "exclude_loss": False,
        "exclude_boards": [],
        "min_price": 0,
    }

    def _bars(self):
        return {s: make_bar(s) for s in ("000001.SZ", "600036.SH")}

    def test_missing_st_methods(self, caplog):
        f = StockFilter(BareBackend(), "20240601",
                        {**self._BASE, "exclude_st": True})
        assert "ST 过滤不生效" in caplog.text
        assert set(f.filter(self._bars(), "20240603")) == set(self._bars())

    def test_missing_recent_listings(self, caplog):
        f = StockFilter(BareBackend(), "20240601",
                        {**self._BASE, "exclude_new_stock": True})
        assert "次新股过滤不生效" in caplog.text
        assert set(f.filter(self._bars(), "20240603")) == set(self._bars())

    def test_missing_industries(self, caplog):
        f = StockFilter(BareBackend(), "20240601",
                        {**self._BASE, "exclude_industries": ["银行"]})
        f.filter(self._bars(), "20240603")
        assert "行业过滤不生效" in caplog.text
        assert set(f.filter(self._bars(), "20240603")) == set(self._bars())

    def test_missing_index_members(self, caplog):
        f = StockFilter(BareBackend(), "20240601",
                        {**self._BASE, "index_universe": ["000300.SH"]})
        assert "白名单规则不生效" in caplog.text
        assert set(f.filter(self._bars(), "20240603")) == set(self._bars())


class TestExcludeST:
    """ST 按当日快照判定：当日有记录才是 ST，摘帽次日自动恢复。"""

    def test_daily_snapshot_semantics(self):
        backend = StubBackend(st_map={"20240603": {"600036.SH"}})
        rules = {
            "exclude_st": True,
            "exclude_new_stock": False,
            "exclude_loss": False,
            "exclude_boards": [],
            "min_price": 0,
        }
        f = StockFilter(backend, "20240601", rules)
        bars = {s: make_bar(s) for s in ("600036.SH", "600519.SH")}
        assert set(f.filter(bars, "20240603")) == {"600519.SH"}
        # 次日快照已无 600036（摘帽）→ 恢复可买
        assert set(f.filter(bars, "20240604")) == {"600036.SH", "600519.SH"}
