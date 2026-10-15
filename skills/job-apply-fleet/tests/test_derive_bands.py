import derive_market_bands as dmb


def test_median_bands_need_three_verified_leads_per_level():
    leads = [{"schema_version": 2, "triage": {"level": "senior", "pay_source": "posting",
              "pay_bands": [{"location_metro": "new_york", "min": mn, "max": mx, "period": "year", "basis": "base"}]}}
             for mn, mx in [(110000, 150000), (120000, 160000), (130000, 170000)]]
    leads.append({"schema_version": 2, "triage": {"level": "mid", "pay_source": "posting",
                  "pay_bands": [{"location_metro": "new_york", "min": 1, "max": 2, "period": "year", "basis": "base"}]}})
    leads.append({"schema_version": 2, "triage": {"level": "senior", "pay_source": "none", "pay_bands": []}})
    bands, counts = dmb.derive(leads, {"senior": [1, 2], "mid": [3, 4]})
    assert bands == {"senior": [120000, 160000], "mid": [3, 4]} and counts == {"senior": 3, "mid": 1}


def test_hourly_bands_annualized_and_ote_ignored():
    mk = lambda b: {"schema_version": 2, "triage": {"level": "entry", "pay_source": "ats_api", "pay_bands": [b]}}
    leads = [mk({"location_metro": "new_york", "min": 40, "max": 50, "period": "hour", "basis": "base"})] * 3 \
        + [mk({"location_metro": "new_york", "min": 1, "max": 9, "period": "year", "basis": "ote"})]
    bands, counts = dmb.derive(leads, {"entry": [0, 0]})
    assert bands["entry"] == [83200, 104000] and counts["entry"] == 3


def test_prefers_a_home_metro_band_when_several():
    lead = {"schema_version": 2, "triage": {"level": "senior", "pay_source": "posting", "pay_bands": [
        {"location_metro": "san_francisco", "min": 200000, "max": 250000, "period": "year", "basis": "base"},
        {"location_metro": "boston", "min": 150000, "max": 180000, "period": "year", "basis": "base"}]}}
    bands, counts = dmb.derive([lead] * 3, {"senior": [1, 2]}, home=("boston",))
    assert bands["senior"] == [150000, 180000] and counts["senior"] == 3
    first, _ = dmb.derive([lead] * 3, {"senior": [1, 2]})                 # no home metro: the first band
    assert first["senior"] == [200000, 250000]
