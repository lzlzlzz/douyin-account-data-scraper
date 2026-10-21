import json

import pytest

from douyin_daily_contributions import (
    DouyinApiError,
    DailyContribution,
    _date_range,
    combine_rows,
    normalize_fan_rows,
    normalize_rows,
)


def test_normalize_publish_trends_to_daily_rows():
    payload = {
        "status_code": 0,
        "metrics": [
            {
                "english_metric_name": "publish_cnt",
                "metric_name": "投稿量",
                "metric_value": 1,
                "trends": [
                    {"date_time": "20260908", "value": 0},
                    {"date_time": "20260907", "value": 1},
                ],
            }
        ],
    }
    assert normalize_rows(payload) == [
        DailyContribution("2026-09-07", 1),
        DailyContribution("2026-09-08", 0),
    ]


def test_missing_publish_metric_is_explicit_error():
    with pytest.raises(DouyinApiError, match="publish_cnt"):
        normalize_rows({"status_code": 0, "metrics": []})


def test_normalize_fan_trends_to_daily_fields():
    payload = {
        "status_code": 0,
        "metrics": [
            {
                "english_metric_name": "total_fans_cnt",
                "metric_name": "总粉丝量",
                "trends": [{"date_time": "20260908", "value": 1006}],
            },
            {
                "english_metric_name": "net_fans_cnt",
                "metric_name": "净增粉丝",
                "trends": [{"date_time": "20260908", "value": -1}],
            },
            {
                "english_metric_name": "new_fans_cnt",
                "metric_name": "新增粉丝量",
                "trends": [{"date_time": "20260908", "value": 0}],
            },
            {
                "english_metric_name": "cancel_fans_cnt",
                "metric_name": "取关粉丝",
                "trends": [{"date_time": "20260908", "value": 1}],
            },
            {
                "english_metric_name": "home_view_fans_cnt",
                "metric_name": "回访粉丝量",
                "trends": [{"date_time": "20260908", "value": 0}],
            },
        ],
    }
    assert normalize_fan_rows(payload) == {
        "2026-09-08": {
            "total_fans": 1006,
            "net_fans": -1,
            "increase_fans": 0,
            "cancel_fans": 1,
            "return_fans": 0,
        }
    }


def test_combine_rows_aligns_contributions_and_fans_by_date():
    publish = {
        "status_code": 0,
        "metrics": [{
            "english_metric_name": "publish_cnt",
            "metric_name": "投稿量",
            "trends": [{"date_time": "20260908", "value": 1}],
        }],
    }
    fans = {
        "status_code": 0,
        "metrics": [
            {"english_metric_name": "total_fans_cnt", "trends": [{"date_time": "20260908", "value": 1006}]},
            {"english_metric_name": "net_fans_cnt", "trends": [{"date_time": "20260908", "value": -1}]},
            {"english_metric_name": "new_fans_cnt", "trends": [{"date_time": "20260908", "value": 0}]},
            {"english_metric_name": "cancel_fans_cnt", "trends": [{"date_time": "20260908", "value": 1}]},
            {"english_metric_name": "home_view_fans_cnt", "trends": [{"date_time": "20260908", "value": 0}]},
        ],
    }
    rows = combine_rows(publish, fans)
    assert rows[0].date == "2026-09-08"
    assert rows[0].contribution_count == 1
    assert rows[0].total_fans == 1006
    assert rows[0].net_fans == -1


def test_date_range_matches_dashboard_request_shape():
    assert _date_range("2026-09-01", "2026-09-08") == {
        "start_date": "20260901",
        "end_date": "20260908",
    }
    with pytest.raises(ValueError, match="同时提供"):
        _date_range("2026-09-01", None)
    with pytest.raises(ValueError, match="不能晚于"):
        _date_range("2026-09-08", "2026-09-01")
